"""Render de fotogramas en Modal (GPU en la nube) para Voxel Factory.

voxel.py prepara todo en local (voces, visemas, subtítulos y job.json) y llama a este script
con el intérprete que tenga el paquete `modal`. Acá solo se ejecuta Blender: el rango de
fotogramas se parte en tramos que se renderizan en paralelo, cada uno en su propio contenedor
con GPU, y los PNG vuelven a la carpeta temporal local. La unión con audio y subtítulos sigue
siendo local, así que la salida es idéntica a la de un render local.

Uso directo (normalmente lo invoca voxel.py con --cloud modal):
    python cloud_modal.py JOB.json SALIDA_DIR [--chunks N] [--blend RUTA] [--gpu L4] [--check]

Variables: VOXEL_MODAL_GPU (L4 por defecto), MODAL_PROFILE (cuenta de Modal a usar).
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent.parent
BLENDER_VERSION = '5.2.4'
BLENDER_URL = (f'https://download.blender.org/release/Blender{BLENDER_VERSION.rsplit(".", 1)[0]}/'
               f'blender-{BLENDER_VERSION}-linux-x64.tar.xz')
# Lista de preferencia separada por comas: Modal toma la primera que tenga libre, así un tramo no
# espera en cola por un modelo agotado. Una sola GPU («L4») fija el modelo.
GPU_LIST = [g.strip() for g in os.environ.get('VOXEL_MODAL_GPU', 'L4,A10G,T4').split(',') if g.strip()]
GPU = GPU_LIST[0] if len(GPU_LIST) == 1 else GPU_LIST
# Las fuentes de macOS que usa el escenario viajan con la imagen para que el render sea igual al local.
FONTS = ['/System/Library/Fonts/Menlo.ttc', '/System/Library/Fonts/SFNSMono.ttf',
         '/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf']

image = (
    modal.Image.debian_slim(python_version='3.11')
    .apt_install('wget', 'xz-utils', 'libxi6', 'libxxf86vm1', 'libxfixes3', 'libxrender1', 'libgl1', 'libegl1',
                 'libxkbcommon0', 'libxkbcommon-x11-0', 'libsm6', 'libice6', 'libx11-6', 'libxext6',
                 'libwayland-client0', 'libwayland-egl1', 'libwayland-cursor0', 'libdbus-1-3', 'libvulkan1',
                 'fonts-dejavu-core')
    .run_commands(f'wget -q {BLENDER_URL} -O /tmp/blender.tar.xz && mkdir -p /opt/blender && '
                  'tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 && rm /tmp/blender.tar.xz')
    .env({'VOXEL_FONTS': '/fonts', 'NVIDIA_DRIVER_CAPABILITIES': 'all'})
)
for path in FONTS:
    if Path(path).is_file():
        image = image.add_local_file(path, f'/fonts/{Path(path).name}', copy=True)
image = image.add_local_dir(ROOT / 'studio', '/voxel/studio', ignore=['__pycache__', '*.pyc', 'cloud_modal.py'])

app = modal.App('voxel-factory', image=image)


@app.function(gpu=GPU, cpu=4, memory=8192, timeout=3600, max_containers=10)
def render_chunk(job: dict, start: int, end: int, save_blend: bool, live=None) -> bytes:
    work = Path(f'/tmp/voxel-{start}-{end}')
    # Un contenedor reutilizado conserva /tmp: cada tramo usa una carpeta propia y vacía.
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    job = dict(job, start=start, end=end, frame_path=str(work / 'shot_'),
               blend=str(work / 'escena.blend') if save_blend else None)
    job_file = work / 'job.json'
    job_file.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
    began = time.time()
    if live is not None:
        live[str(start)] = 0
    process = subprocess.Popen(['/opt/blender/blender', '-b', '--factory-startup', '--python-exit-code', '1',
                                '--python', '/voxel/studio/blender_scene.py', '--', str(job_file)],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
    tail, count = [], 0
    for line in process.stdout:
        tail = (tail + [line.rstrip()])[-40:]
        if line.startswith('Saved:') and '.png' in line:
            count += 1
            # Avance en vivo: el cliente local suma estos contadores cada 2 s.
            if live is not None and (count % 3 == 0 or count == end - start):
                live[str(start)] = count
    if process.wait():
        raise RuntimeError(f'Blender falló en el tramo {start}–{end}:\n' + '\n'.join(tail[-30:]))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for item in sorted(work.iterdir()):
            if item.suffix in ('.png', '.blend'):
                archive.add(item, arcname=item.name)
        device = subprocess.run(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
                                capture_output=True, text=True).stdout.strip() or '?'
        log = (f'tramo {start}–{end}: {end - start} fotogramas en {time.time() - began:.0f} s '
               f'({device})\n').encode()
        info = tarfile.TarInfo('tramo.log')
        info.size = len(log)
        archive.addfile(info, io.BytesIO(log))
    return buffer.getvalue()


@app.function(gpu=GPU, timeout=600)
def check() -> dict:
    """Comprueba la GPU y que Blender arranque, sin renderizar nada."""
    gpu = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,driver_version', '--format=csv,noheader'],
                         capture_output=True, text=True).stdout.strip()
    version = subprocess.run(['/opt/blender/blender', '-b', '--factory-startup', '--version'],
                             capture_output=True, text=True).stdout.splitlines()[:1]
    probe = subprocess.run(['/opt/blender/blender', '-b', '--factory-startup', '--python-expr',
                            'import bpy, gpu; gpu.init(); '
                            'print("VOXEL_GPU", gpu.platform.vendor_get(), gpu.platform.renderer_get(), '
                            'gpu.platform.backend_type_get())'],
                           capture_output=True, text=True, errors='replace')
    line = next((l for l in (probe.stdout + probe.stderr).splitlines() if 'VOXEL_GPU' in l), None)
    libs = subprocess.run('ls /usr/lib/x86_64-linux-gnu/ /usr/lib64 2>/dev/null | grep -iE "nvidia|EGL|vulkan" | sort -u',
                          shell=True, capture_output=True, text=True).stdout.split()
    icds = subprocess.run('ls /usr/share/vulkan/icd.d /etc/vulkan/icd.d /usr/share/glvnd/egl_vendor.d 2>/dev/null',
                          shell=True, capture_output=True, text=True).stdout.split()
    return dict(gpu=gpu, blender=version[0] if version else None, eevee_context=line,
                fonts=sorted(os.listdir('/fonts')) if os.path.isdir('/fonts') else [], graphics_libs=libs, icds=icds,
                error=None if line else '\n'.join((probe.stdout + probe.stderr).splitlines()[-15:]))


def split(start, end, chunks):
    size = max(1, -(-(end - start) // chunks))
    return [(a, min(end, a + size)) for a in range(start, end, size)]


def _stop_on_signal(signum, frame):
    # Si matan al cliente local (pkill, Ctrl+C, cierre de la terminal), salir por KeyboardInterrupt
    # hace que `app.run()` detenga el app en Modal y libere las GPUs; sin esto quedaba huérfano.
    raise KeyboardInterrupt(f'señal {signum}')


def main():
    import signal
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _stop_on_signal)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('job', nargs='?', type=Path)
    parser.add_argument('out', nargs='?', type=Path)
    parser.add_argument('--chunks', type=int, default=0, help='Tramos en paralelo (0 = uno cada ~90 fotogramas).')
    parser.add_argument('--blend', type=Path, help='Guarda la escena editable (la arma el primer tramo).')
    parser.add_argument('--check', action='store_true', help='Solo comprueba GPU y Blender en la nube.')
    args = parser.parse_args()
    with modal.enable_output(), app.run():
        if args.check:
            print(json.dumps(check.remote(), ensure_ascii=False))
            return
        job = json.loads(args.job.read_text(encoding='utf-8'))
        start, end = job['start'], job['end']
        chunks = args.chunks or min(10, max(1, round((end - start) / 90)))
        pieces = split(start, end, chunks)
        print(f'modal: {end - start} fotogramas en {len(pieces)} tramos sobre {"/".join(GPU_LIST)}', file=sys.stderr, flush=True)
        began = time.time()
        import threading
        with modal.Dict.ephemeral() as live:
            calls = [(job, a, b, bool(args.blend) and i == 0, live) for i, (a, b) in enumerate(pieces)]
            stop = threading.Event()

            def poll():
                last = -1
                while not stop.wait(2):
                    try:
                        rendered = sum(int(live.get(str(a), 0)) for a, _ in pieces)
                    except Exception:  # noqa: BLE001 — un sondeo fallido no corta el render
                        continue
                    if rendered != last:
                        last = rendered
                        print(f'en vivo: {rendered}/{end - start} fotogramas renderizados · '
                              f'{time.time() - began:.0f} s', flush=True)

            watcher = threading.Thread(target=poll, daemon=True)
            watcher.start()
            try:
                received = collect(render_chunk.starmap(calls, order_outputs=False), args, start, end, began)
            finally:
                stop.set()
        if len(received) != end - start:
            raise SystemExit(f'Faltan fotogramas: llegaron {len(received)} de {end - start}.')


def collect(results, args, start, end, began):
    received = set()
    for payload in results:
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            for member in archive.getmembers():
                data = archive.extractfile(member).read()
                if member.name == 'escena.blend' and args.blend:
                    args.blend.write_bytes(data)
                elif member.name == 'tramo.log':
                    print(data.decode().strip(), flush=True)
                elif member.name.endswith('.png'):
                    (args.out / member.name).write_bytes(data)
                    received.add(member.name)
        print(f'recibidos: {len(received)}/{end - start} fotogramas · {time.time() - began:.0f} s', flush=True)
    return received


if __name__ == '__main__':
    main()
