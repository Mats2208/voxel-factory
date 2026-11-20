#!/usr/bin/env python3
"""Voxel Factory: convierte un guion JSON en un MP4 vertical con personajes voxel, voces,
actuación, cámaras y subtítulos, renderizado con Blender en segundo plano (local o en Modal).

Subcomandos (todos aceptan --json para salida legible por agentes):
  doctor                 comprueba Blender, FFmpeg con libass, ffprobe, voces y Whisper
  schema                 imprime los campos, poses, emociones y voces válidas
  new                    crea un guion a partir de líneas «A: texto»
  validate GUION         valida sin renderizar (errores con ruta y sugerencia)
  compile GUION -o X     escribe el guion expandido (tiempos y planos automáticos resueltos)
  preview GUION          prueba corta a media resolución, verificada
  render GUION -o MP4    render completo y verificación automática
  verify MP4 --spec G    decodifica y comprueba formato, audio y subtítulos
  analyze VIDEO          desarma un video de referencia: cortes, fotogramas, audio y transcripción
  open PROYECTO          abre la escena de una toma en el Blender local (sin renderizar)
  cloud-stop             detiene renders de este proyecto que sigan vivos en Modal

Compatibilidad: `voxel.py GUION -o MP4 [opciones]` sigue funcionando como `render`.

Códigos de salida: 0 bien, 1 error interno, 2 guion o argumentos inválidos, 3 falta una
dependencia, 4 falló Blender o FFmpeg, 5 la verificación del MP4 no pasó.
"""
import argparse
import bisect
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unicodedata
import wave
from array import array
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def load_dotenv(path):
    """Carga VARIABLE=valor de un .env local (nunca se sube a git) sin pisar el entorno."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.removeprefix('export ').partition('=')
        os.environ.setdefault(key.strip(), value.strip().strip('\'"'))


load_dotenv(ROOT / '.env')
CACHE = Path(os.environ.get('VOXEL_CACHE', ROOT / '.cache'))
RATE = 16000       # análisis de visemas
MIX_RATE = 48000   # mezcla que va al MP4
POSES = {'down', 'open', 'explain', 'shrug', 'insist', 'crossed', 'question'}
EXPRESSIONS = {'neutral', 'speaking', 'sad', 'crying', 'sarcastic', 'surprised', 'happy', 'wink',
               'angry', 'worried', 'love', 'dizzy', 'sleepy', 'excited', 'whisper', 'smug'}
ICONS = {'heart', 'question', 'exclaim', 'sweat', 'zzz', 'sparkle', 'idea', 'anger', 'music', 'skull'}
# Palabras que disparan un gesto (emoción breve) y/o un ícono sobre Claude, en el instante en que las dice.
CUES = [
    (r'error|bug|romp|crimen|jefe|slack|push|main|producci|borr|nadie|culpa|problema', 'worried', 'sweat'),
    (r'amor|quiero|gracias|favor|lindo|te\s?quiero|cari', 'love', 'heart'),
    (r'dorm|duerm|despiert|sueñ|noche|cansad', 'sleepy', 'zzz'),
    (r'idea|arregl|soluci|listo|tranqui|respir|calma', 'happy', 'idea'),
    (r'dinero|propina|dólar|pag|gratis|premio|genial|perfect', 'excited', 'sparkle'),
    (r'muert|morir|fin|despid', 'dizzy', 'skull'),
    (r'enoj|odio|basta|furios', 'angry', 'anger'),
    (r'canci|música|cant', 'happy', 'music'),
    (r'^tests?\b|test[s.]', 'happy', 'idea'),
    (r'deploy|plo\b|desplieg', 'worried', 'sweat'),
    (r'documentaci', 'smug', 'question'),
]
CHARACTER_TYPES = {'person', 'cube', 'custom'}
# Nombres anteriores, aceptados por compatibilidad.
TYPE_ALIASES = {'human': 'person', 'claude': 'cube'}
PRESETS = ROOT / 'characters'
STAGES = {'terminal', 'terminal_classic', 'custom'}
SHOT_KINDS = {'close', 'wide'}
CANVAS = (720, 1280)

EXIT_OK, EXIT_INTERNAL, EXIT_SPEC, EXIT_DEPENDENCY, EXIT_RENDER, EXIT_VERIFY = 0, 1, 2, 3, 4, 5

# Voces macOS por idioma, asignadas en orden a los personajes que no declaran `voice`.
SAY_DEFAULTS = {
    'es': ['Eddy (Spanish (Mexico))', 'Paulina', 'Reed (Spanish (Spain))', 'Mónica',
           'Rocko (Spanish (Mexico))', 'Flo (Spanish (Mexico))'],
    'en': ['Eddy (English (US))', 'Samantha', 'Reed (English (US))', 'Flo (English (US))'],
    'pt': ['Eddy (Portuguese (Brazil))', 'Luciana'],
}


class VoxelError(Exception):
    """Error accionable: `code` es el código de salida, `hint` dice cómo corregirlo."""

    def __init__(self, message, code=EXIT_SPEC, hint=None, errors=None, log=None):
        super().__init__(message)
        self.code, self.hint, self.errors, self.log = code, hint, errors or [], log


def run(*command, **options):
    return subprocess.run([str(x) for x in command], check=True, **options)


def say(message, quiet=False):
    progress(message)
    if not quiet:
        print(message, file=sys.stderr, flush=True)


PROGRESS = {'file': None}


def progress(line):
    """Agrega una línea al progreso del render en curso (renders/progreso/<video>.txt), que lee el mod."""
    if PROGRESS['file']:
        with open(PROGRESS['file'], 'a', encoding='utf-8') as handle:
            handle.write(line.rstrip() + '\n')


# ---------------------------------------------------------------- dependencias

def find_ffmpeg(required=True):
    requested = os.environ.get('FFMPEG')
    candidates = [requested] if requested else [
        shutil.which('ffmpeg'), '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg',
        '/usr/local/opt/ffmpeg-full/bin/ffmpeg',
    ]
    for binary in dict.fromkeys(candidates):
        if binary and (resolved := shutil.which(binary)):
            filters = run(resolved, '-hide_banner', '-filters', capture_output=True, text=True).stdout
            if re.search(r'^\s*[.TSC]+\s+ass\s+', filters, flags=re.MULTILINE):
                return resolved
    if not required:
        return None
    raise VoxelError('Se requiere ffmpeg con libass (filtro «ass») para quemar los subtítulos.', EXIT_DEPENDENCY,
                     'En macOS: brew install ffmpeg-full. O exportá FFMPEG=/ruta/a/ffmpeg.')


def find_ffprobe(ffmpeg=None):
    if ffmpeg:
        sibling = Path(ffmpeg).with_name('ffprobe')
        if sibling.is_file():
            return str(sibling)
    return shutil.which('ffprobe')


def find_blender():
    mac = Path('/Applications/Blender.app/Contents/MacOS/Blender')
    return os.environ.get('BLENDER') or shutil.which('blender') or (str(mac) if mac.is_file() else None)


def find_whisper():
    binary = os.environ.get('WHISPER') or shutil.which('whisper-cli') or shutil.which('whisper-cpp')
    model = os.environ.get('WHISPER_MODEL')
    if not model:
        for folder in (Path.home() / 'Library/Application Support/pywhispercpp/models',
                       Path.home() / '.cache/whisper', Path('/opt/homebrew/share/whisper-cpp')):
            found = sorted(folder.glob('ggml-*.bin')) if folder.is_dir() else []
            if found:
                model = str(next((f for f in found if 'turbo' in f.name and 'q5' not in f.name), found[0]))
                break
    return binary, model


def say_voices():
    if not shutil.which('say'):
        return []
    listing = run('say', '-v', '?', capture_output=True, text=True).stdout
    voices = []
    for line in listing.splitlines():
        match = re.match(r'^(.+?)\s+([a-z]{2,3}_[A-Z0-9]{2,3})\s+#', line)
        if match:
            voices.append((match[1].strip(), match[2]))
    return voices


def doctor(args):
    checks = []

    def add(name, ok, detail, required=True, hint=None):
        checks.append(dict(name=name, ok=bool(ok), required=required, detail=detail, hint=hint))

    add('python', sys.version_info >= (3, 10), sys.version.split()[0], hint='Usá Python 3.10 o posterior.')
    blender = find_blender()
    version = None
    if blender:
        try:
            out = run(blender, '--version', capture_output=True, text=True, timeout=60).stdout
            version = out.splitlines()[0] if out else '?'
        except (OSError, subprocess.SubprocessError) as exc:
            version = f'no responde: {exc}'
    add('blender', blender and version and not version.startswith('no responde'), f'{blender} · {version}',
        hint='Instalá Blender 4.2+ (verificado con 5.2 LTS) o exportá BLENDER=/ruta/al/ejecutable.')
    ffmpeg = find_ffmpeg(required=False)
    add('ffmpeg+libass', ffmpeg, ffmpeg or 'no encontrado', hint='brew install ffmpeg-full o FFMPEG=/ruta.')
    ffprobe = find_ffprobe(ffmpeg)
    add('ffprobe', ffprobe, ffprobe or 'no encontrado', hint='Viene con ffmpeg.')
    voices = say_voices()
    espeak = shutil.which('espeak-ng')
    by_lang = {}
    for name, locale in voices:
        by_lang.setdefault(locale[:2], []).append(name)
    add('voces (say/espeak-ng)', voices or espeak,
        f"say: {len(voices)} voces ({', '.join(f'{k}:{len(v)}' for k, v in sorted(by_lang.items()) if k in ('es', 'en', 'pt'))})"
        + (f' · espeak-ng: {espeak}' if espeak else ''),
        hint='En macOS viene `say`. En Linux: apt install espeak-ng. O dá audio propio por línea.')
    modal_python = find_modal_python()
    add('modal (opcional, render en la nube)', modal_python, modal_python or 'paquete modal no encontrado',
        required=False, hint='uv tool install modal && modal setup')
    whisper, model = find_whisper()
    add('whisper (opcional, para analyze)', whisper and model, f'{whisper or "—"} · modelo {model or "—"}',
        required=False, hint='brew install whisper-cpp y descargá un modelo ggml; WHISPER_MODEL=/ruta.')
    scratch = Path(args.scratch or tempfile.gettempdir())
    free = shutil.disk_usage(scratch).free / 2 ** 30
    add('espacio temporal', free > 2, f'{free:.1f} GB libres en {scratch}', hint='Usá --scratch en otro disco.')
    missing = [c for c in checks if c['required'] and not c['ok']]
    return dict(ok=not missing, checks=checks), (EXIT_DEPENDENCY if missing else EXIT_OK)


# ---------------------------------------------------------------- guion

def preset_names():
    return sorted(p.stem for p in PRESETS.glob('*.json')) if PRESETS.is_dir() else []


def resolve_characters(spec):
    """Normaliza tipos y expande presets: {"preset": "robot", ...} toma characters/robot.json y aplica
    encima los campos del guion (posición, escala, voz, colores…)."""
    for name, actor in (spec.get('characters') or {}).items():
        if not isinstance(actor, dict):
            continue
        preset = actor.get('preset') or (actor.get('type') if actor.get('type') in preset_names() else None)
        if preset:
            file = PRESETS / f'{preset}.json'
            if not file.is_file():
                raise VoxelError(f'characters.{name}.preset: no existe el preset «{preset}».', EXIT_SPEC,
                                 f'Presets disponibles: {preset_names()}. Se crean en characters/<nombre>.json.')
            base = json.loads(file.read_text(encoding='utf-8'))
            base.pop('description', None)
            merged = {**base, **{k: v for k, v in actor.items() if k not in ('preset', 'type')}}
            merged['type'] = base.get('type', 'custom')
            if 'materials' in actor:
                merged['materials'] = {**base.get('materials', {}), **actor['materials']}
            actor.clear()
            actor.update(merged)
        actor['type'] = TYPE_ALIASES.get(actor.get('type'), actor.get('type'))
    return spec


def load_spec(path):
    path = Path(path)
    if not path.is_file():
        raise VoxelError(f'No existe el guion {path}.', EXIT_SPEC, 'Revisá la ruta o creá uno con `voxel.py new`.')
    try:
        spec = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise VoxelError(f'JSON mal formado en {path}:{exc.lineno}:{exc.colno}: {exc.msg}.', EXIT_SPEC,
                         'Suele ser una coma de más o de menos, o comillas sin cerrar.') from exc
    if not isinstance(spec, dict):
        raise VoxelError('El guion debe ser un objeto JSON.', EXIT_SPEC)
    return spec


def voice_for(spec, name, line=None):
    actor = spec['characters'][name]
    voice = (line or {}).get('voice', actor.get('voice'))
    if voice:
        return voice
    if shutil.which('espeak-ng') or not shutil.which('say'):
        return None
    language = spec.get('language', 'es')[:2]
    available = {v for v, _ in say_voices()}
    order = list(spec['characters']).index(name)
    choices = [v for v in SAY_DEFAULTS.get(language, []) if v in available]
    return f'say:{choices[order % len(choices)]}' if choices else None


def tts(text, language, voice, speed, destination):
    espeak = shutil.which('espeak-ng')
    if voice and voice.startswith('say:'):
        if not shutil.which('say'):
            raise VoxelError('La voz say: requiere macOS.', EXIT_DEPENDENCY,
                             'Quitá `voice` para usar espeak-ng o dá un archivo `audio` por línea.')
        destination = destination.with_suffix('.aiff')
        name = voice.removeprefix('say:')
        if name not in {v for v, _ in say_voices()}:
            raise VoxelError(f'La voz macOS «{name}» no está instalada.', EXIT_SPEC,
                             'Listá las voces con `voxel.py schema` o `say -v "?"`.')
        run('say', '-v', name, '-r', speed, '-o', destination, '--', text)
    elif espeak:
        run(espeak, '-v', voice or language, '-s', speed, '-w', destination, '--', text)
    elif shutil.which('say') and not voice:
        locale = {'es': 'es_ES', 'en': 'en_US', 'pt': 'pt_BR'}.get(language, language)
        match = next((v for v, loc in say_voices() if loc == locale), None)
        if not match:
            raise VoxelError(f'No hay voz instalada para {language}.', EXIT_DEPENDENCY,
                             'Instalá una voz en Ajustes > Accesibilidad > Contenido leído.')
        destination = destination.with_suffix('.aiff')
        run('say', '-v', match, '-r', speed, '-o', destination, '--', text)
    else:
        raise VoxelError('No hay sintetizador de voz.', EXIT_DEPENDENCY,
                         'Instalá espeak-ng, usá una voz macOS con say: o indicá un archivo de audio por línea.')
    return destination


ELEVEN_ENV_FILES = [ROOT / '.env']


def eleven_key():
    """Clave de ElevenLabs: ELEVENLABS_API_KEY, o el archivo de ELEVENLABS_ENV_FILE, o los env conocidos.
    Nunca se imprime ni se copia."""
    if os.environ.get('ELEVENLABS_API_KEY'):
        return os.environ['ELEVENLABS_API_KEY']
    files = [Path(os.environ['ELEVENLABS_ENV_FILE'])] if os.environ.get('ELEVENLABS_ENV_FILE') else ELEVEN_ENV_FILES
    for file in files:
        if file.is_file():
            match = re.search(r"ELEVENLABS_API_KEY=['\"]?([^'\"\s]+)", file.read_text(errors='ignore'))
            if match:
                return match[1]
    raise VoxelError('No encontré la clave de ElevenLabs.', EXIT_DEPENDENCY,
                     'Poné ELEVENLABS_API_KEY=… en el .env de la fábrica (ver .env.example) o exportala. '
                     'Sin clave, usá voces locales (say/espeak-ng).')


def eleven_tts(text, voice_id, model, settings, destination):
    """Sintetiza con ElevenLabs y guarda el MP3 y la alineación por carácter (para boca y gestos)."""
    import base64
    import urllib.error
    import urllib.request
    body = {'text': text, 'model_id': model}
    if settings:
        body['voice_settings'] = settings
    request = urllib.request.Request(
        f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps?output_format=mp3_44100_128',
        data=json.dumps(body).encode(), method='POST',
        headers={'xi-api-key': eleven_key(), 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors='replace')[:300]
        hints = {401: ('La clave de ElevenLabs no es válida.', EXIT_DEPENDENCY),
                 402: ('La cuenta de ElevenLabs no tiene créditos.', EXIT_DEPENDENCY),
                 429: ('ElevenLabs limitó las solicitudes o no quedan caracteres.', EXIT_DEPENDENCY)}
        message, code = hints.get(exc.code, (f'ElevenLabs rechazó la voz {voice_id} con el modelo {model}.', EXIT_SPEC))
        raise VoxelError(f'{message} (HTTP {exc.code}: {detail})', code,
                         'Revisá voice (eleven:<voice_id>), eleven_model y la cuenta; `voxel.py schema --voices` lista voces.')
    except urllib.error.URLError as exc:
        raise VoxelError(f'No pude conectar con ElevenLabs: {exc.reason}', EXIT_DEPENDENCY, 'Revisá la conexión.')
    destination = destination.with_suffix('.mp3')
    destination.write_bytes(base64.b64decode(data['audio_base64']))
    alignment = data.get('normalized_alignment') or data.get('alignment') or {}
    destination.with_suffix('.align.json').write_text(json.dumps(alignment), encoding='utf-8')
    return destination


def clean_text(text):
    """Texto visible: sin etiquetas de interpretación como [whispers] o [gasps]."""
    return re.sub(r'\s*\[[^\]]*\]\s*', ' ', text).strip()


def spoken_text(line, actor):
    """Texto enviado al sintetizador: etiquetas de interpretación de ElevenLabs v3 como [whispers]."""
    tag = line.get('tag', actor.get('tag'))
    text = line['text']
    if tag and f'[{tag}]' not in text:
        text = f'[{tag}] {text}'
    # eleven_v3 corta en seco la última sílaba si la frase termina justo ahí: una pausa final la deja
    # terminar (medido: −25 dB → −39 dB en los últimos 50 ms).
    if not text.rstrip().endswith(']'):
        text = f'{text} [short pause]'
    return text


def word_times(clip):
    """Palabras con [inicio, fin, palabra] relativos al clip, desde la alineación de ElevenLabs."""
    align_file = Path(clip).with_suffix('.align.json')
    if not align_file.is_file():
        return []
    data = json.loads(align_file.read_text(encoding='utf-8'))
    chars = data.get('characters', [])
    starts = data.get('character_start_times_seconds', [])
    ends = data.get('character_end_times_seconds', [])
    words, current, begin, finish, bracket = [], '', None, None, False
    for ch, a, b in zip(chars, starts, ends):
        if ch == '[':
            bracket = True
        if bracket:
            bracket = ch != ']'
            continue
        if ch.isspace():
            if current:
                words.append([round(begin, 3), round(finish, 3), current])
            current, begin = '', None
            continue
        current += ch
        begin = a if begin is None else begin
        finish = b
    if current:
        words.append([round(begin, 3), round(finish, 3), current])
    return words


def voice_clip(spec, index, line, directory):
    """Devuelve el archivo de voz de una línea. Las voces sintetizadas se guardan en .cache/voices."""
    if line.get('audio'):
        source = (directory / line['audio']).resolve()
        if not source.is_file():
            raise VoxelError(f'dialogue[{index}].audio: no existe {source}.', EXIT_SPEC,
                             'Las rutas de audio se resuelven desde la carpeta del JSON.')
        return source
    actor = spec['characters'][line['speaker']]
    voice = voice_for(spec, line['speaker'], line)
    rate = line.get('rate', actor.get('rate', 174))
    language = spec.get('language', 'es')
    folder = CACHE / 'voices'
    folder.mkdir(parents=True, exist_ok=True)
    if voice and voice.startswith('eleven:'):
        model = line.get('eleven_model', actor.get('eleven_model', spec.get('eleven_model', 'eleven_v3')))
        settings = line.get('voice_settings', actor.get('voice_settings'))
        text = spoken_text(line, actor)
        digest = hashlib.sha1(json.dumps([text, voice, model, settings]).encode()).hexdigest()[:16]
        cached = folder / f'eleven-{digest}.mp3'
        if cached.is_file() and cached.stat().st_size > 1000:
            return cached
        return eleven_tts(text, voice.removeprefix('eleven:'), model, settings, cached)
    digest = hashlib.sha1(json.dumps([line['text'], voice, rate, language]).encode()).hexdigest()[:16]
    for suffix in ('.aiff', '.wav'):
        if (cached := folder / f'{digest}{suffix}').is_file() and cached.stat().st_size > 1000:
            return cached
    return tts(line['text'], language, voice, rate, folder / f'{digest}.wav')


def media_length(path):
    ffprobe = find_ffprobe(find_ffmpeg(required=False)) or 'ffprobe'
    out = run(ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', path,
              capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def estimate_length(text):
    return max(.8, len(text) / 14.5 + .35)


def default_pose(text, index):
    if '?' in text:
        return 'question'
    if '!' in text:
        return 'insist'
    return ('explain', 'open', 'shrug')[index % 3]


def auto_beats(line, fps):
    """Gestos de Claude sincronizados con las palabras: [[fotograma_absoluto, emoción], ...] e íconos."""
    base = line.get('emotion', 'neutral')
    words = line.get('tokens') or []
    if not words:
        parts = line['text'].split()
        step = (line['end'] - line['start']) / max(1, len(parts))
        words = [[line['start'] + k * step, line['start'] + (k + 1) * step, w] for k, w in enumerate(parts)]
    keys, icons, last_icon = [[round(line['start'] * fps) - 4, base]], [], -999
    for a, b, word in words:
        lowered = word.lower()
        for pattern, emotion, icon in CUES:
            if re.search(pattern, lowered):
                frame = round(a * fps)
                if base == 'neutral' or emotion in ('worried', 'dizzy', 'sleepy'):
                    keys.append([frame, emotion])
                    keys.append([round(b * fps) + 18, base])
                if frame - last_icon > 25:
                    icons.append([frame, icon, 34])
                    last_icon = frame
                break
    end_frame = round(line['end'] * fps)
    text = line['text'].rstrip()
    if text.endswith('?') and end_frame - last_icon > 25:
        icons.append([end_frame - 10, 'question', 30])
    elif text.endswith('!') and end_frame - last_icon > 25:
        icons.append([end_frame - 10, 'exclaim', 30])
        keys.append([end_frame - 10, 'excited' if base in ('neutral', 'happy') else base])
    elif text.endswith('...') and end_frame - last_icon > 25:
        icons.append([end_frame - 8, 'sweat', 30])
    return sorted(keys), icons


def attach_beats(spec):
    """Reparte los gestos de cada línea de Claude en el plano que los contiene (fotogramas relativos)."""
    fps = spec['fps']
    shots = spec.get('shots', [])
    starts = [shot['frame'] for shot in shots]
    for line in spec.get('dialogue', []):
        actor = spec['characters'].get(line.get('speaker'), {})
        if actor.get('type') != 'cube' or 'start' not in line or line.get('beats') is False:
            continue
        keys, icons = auto_beats(line, fps)
        for beat in line.get('beats') or []:
            frame = round((line['start'] + beat.get('at', 0)) * fps)
            if beat.get('emotion'):
                keys.append([frame, beat['emotion']])
            if beat.get('icon'):
                icons.append([frame, beat['icon'], round(beat.get('dur', 1.1) * fps)])
        home = max(0, bisect.bisect_right(starts, max(0, round(line['start'] * fps))) - 1)
        for frame, emotion in sorted(keys):
            index = max(0, bisect.bisect_right(starts, max(0, frame)) - 1)
            if index != home:
                continue  # un gesto no se cuela en el plano siguiente (pisaría su actuación)
            acting = shots[index].setdefault('acting', {}).setdefault(line['speaker'], {})
            rel = max(0, frame - shots[index]['frame'])
            track = [k for k in acting.get('expr_keys', []) if k[0] != rel] + [[rel, emotion]]
            acting['expr_keys'] = sorted(track)
        for frame, icon, span in icons:
            index = max(0, bisect.bisect_right(starts, max(0, frame)) - 1)
            if index != home:
                continue
            acting = shots[index].setdefault('acting', {}).setdefault(line['speaker'], {})
            acting.setdefault('icons', []).append([max(0, frame - shots[index]['frame']), icon, span])


def expand(spec, directory, synthesize=True, notes=None):
    """Resuelve el formato simple: tiempos automáticos por línea a partir de la voz real,
    duración total y planos automáticos. Un guion ya completo pasa sin cambios."""
    notes = notes if notes is not None else []
    dialogue = spec.get('dialogue', [])
    characters = spec.get('characters', {})
    if not isinstance(dialogue, list) or not isinstance(characters, dict):
        return spec
    gap = spec.get('gap', .3)
    cursor = spec.get('lead_in', .25)
    timed = False
    for i, line in enumerate(dialogue):
        if not isinstance(line, dict) or line.get('speaker') not in characters or not str(line.get('text', '')).strip():
            continue
        if 'start' in line and 'end' in line:
            cursor = line['end'] + gap
            continue
        timed = True
        length = None
        clip = None
        if synthesize or line.get('audio'):
            try:
                clip = voice_clip(spec, i, line, directory)
                length = media_length(clip) + .12
            except (VoxelError, OSError, subprocess.CalledProcessError, ValueError) as exc:
                notes.append(f'dialogue[{i}]: duración estimada sin voz ({exc}).')
        if length is None:
            length = estimate_length(line['text'])
        words = word_times(clip) if clip else []
        offset = 0.0
        if words:
            # Recorte al habla real: ElevenLabs deja silencio antes y después (medido: hasta 2,5 s de aire).
            offset = max(0.0, words[0][0] - .06)
            length = min(length, words[-1][1] + .3) - offset
            line['clip_offset'] = round(offset, 3)
            line['clip_length'] = round(length, 3)
        line.setdefault('start', round(cursor + line.pop('pause', 0), 3))
        line['end'] = round(line['start'] + length, 3)
        cursor = line['end'] + gap
        if words and 'tokens' not in line:
            # Boca y gestos sincronizados con cada palabra real de la voz.
            line['tokens'] = [[round(line['start'] + a - offset, 3), round(line['start'] + b - offset, 3), w]
                              for a, b, w in words]
    fps = spec.setdefault('fps', 30)
    if 'frames' not in spec and 'duration' not in spec and dialogue:
        ends = [line.get('end', 0) for line in dialogue if isinstance(line, dict)]
        ending = spec.get('ending', 'wide' if len(characters) > 1 else None)
        outro = (ending.get('seconds', 2.4) if isinstance(ending, dict) else 2.4) if ending else .6
        spec['duration'] = round(max(ends, default=1) + spec.get('outro', outro), 3)
        timed = True
    if 'shots' not in spec and dialogue:
        shots = []
        for i, line in enumerate(dialogue):
            if not isinstance(line, dict) or 'start' not in line:
                continue
            kind = line.get('shot', 'close')
            subject = None if kind == 'wide' else line.get('speaker')
            frame = 0 if not shots else max(shots[-1]['frame'] + 1, round(line['start'] * fps) - 4)
            acting = {}
            speaker = line.get('speaker')
            if speaker in characters:
                kind_of = characters[speaker].get('type')
                params = dict(line.get('acting', {}))
                if line.get('emotion'):
                    params['emotion'] = line['emotion']
                if kind_of == 'person':
                    params.setdefault('pose', line.get('pose', default_pose(line.get('text', ''), i)))
                    span = max(12, round((line['end'] - line['start']) * fps))
                    # Tres cuartos hacia el otro personaje, con leve inclinación: nunca de frente plano.
                    others = [c.get('position', [0, 0, 0])[0] for k, c in characters.items() if k != speaker]
                    mine = characters[speaker].get('position', [0, 0, 0])[0]
                    side = 1 if not others or sum(others) / len(others) >= mine else -1
                    turn = side * 15
                    params.setdefault('head', [[0, 0, 2, turn, side * 4], [span // 2, 0, 5, turn - side * 4, side * 2],
                                               [span, 0, 1, turn + side * 2, side * 5]])
                elif line.get('pose'):
                    params['pose'] = line['pose']
                acting[speaker] = params
            for other, extra in line.get('listeners', {}).items():
                acting[other] = extra
            if shots and shots[-1].get('subject') == subject and not line.get('cut'):
                shots[-1]['acting'].update(acting)
                continue
            shot = {'frame': frame, 'subject': subject, 'acting': acting}
            if line.get('camera'):
                shot['camera'] = line['camera']
            shots.append(shot)
        ending = spec.get('ending', 'wide' if len(characters) > 1 else None)
        if ending and shots:
            # Cierre: plano general alejado con un leve alejamiento de cámara; `ending` como objeto
            # define la actuación ({"acting": {...}}) y la cámara ({"camera": {...}}).
            config = ending if isinstance(ending, dict) else {}
            last_end = max(line['end'] for line in dialogue if isinstance(line, dict) and 'end' in line)
            frame = round((last_end + config.get('delay', .15)) * fps)
            total = spec.get('frames') or round(spec.get('duration', 0) * fps)
            if shots[-1].get('subject') is None:
                # La última línea ya es un plano general: se alarga con el alejamiento del cierre, sin corte.
                shots[-1].setdefault('camera', config.get('camera', {'end_span': 25.5}))
            elif shots[-1]['frame'] < frame < total - 6:
                acting = {k: {'emotion': 'happy'} if v.get('type') == 'cube' else
                          ({'pose': 'down'} if v.get('type') == 'person' else {})
                          for k, v in characters.items()}
                acting.update(config.get('acting', {}))
                shots.append({'frame': frame, 'subject': None, 'label': 'Cierre · plano general',
                              'camera': config.get('camera', {'end_span': 25.5}), 'acting': acting})
        spec['shots'] = shots
        attach_beats(spec)
        timed = True
    if timed:
        notes.append('Se resolvieron tiempos o planos automáticos; `voxel.py compile` muestra el resultado.')
    return spec


def check(spec, directory=None):
    """Valida el guion completo y devuelve (errores, avisos) con ruta, mensaje y sugerencia."""
    errors, warnings = [], []

    def err(path, message, hint=None):
        errors.append(dict(path=path, message=message, hint=hint))

    def warn(path, message, hint=None):
        warnings.append(dict(path=path, message=message, hint=hint))

    fps = spec.get('fps', 30)
    if not isinstance(fps, int) or fps < 1:
        err('fps', 'Debe ser un entero positivo.', 'Usá 30.')
        fps = 30
    if 'frames' in spec:
        frames = spec['frames']
    elif isinstance(spec.get('duration'), (int, float)):
        frames = round(spec['duration'] * fps)
    else:
        frames = None
        err('duration', 'Falta `duration` (segundos) o `frames`.', 'Agregá "duration": 8.0 o quitá los tiempos de '
            'las líneas para que se calculen solos.')
    if frames is not None and (not isinstance(frames, int) or frames < 1):
        err('frames', 'Debe ser un entero positivo.')
        frames = None
    spec.update(fps=fps, resolution=spec.get('resolution', list(CANVAS)))
    if frames:
        spec['frames'] = frames
    res = spec['resolution']
    if not (isinstance(res, list) and len(res) == 2 and all(isinstance(v, int) and v > 0 and v % 2 == 0 for v in res)):
        err('resolution', 'Debe ser [ancho, alto] con enteros pares positivos.', 'Usá [720, 1280] o [1080, 1920].')
    elif res[0] >= res[1]:
        warn('resolution', 'El formato no es vertical.', 'Los subtítulos y cámaras están pensados para 9:16.')
    actors = spec.get('characters')
    if not isinstance(actors, dict) or not actors:
        err('characters', 'Definí al menos un personaje en un mapa {"A": {...}}.',
            'Ejemplo: {"A": {"type": "person"}, "B": {"preset": "robot"}}.')
        actors = {}
    for name, actor in actors.items():
        where = f'characters.{name}'
        if not isinstance(actor, dict):
            err(where, 'Debe ser un objeto.')
            continue
        if actor.get('type') not in CHARACTER_TYPES:
            err(f'{where}.type', f'Tipo «{actor.get("type")}» desconocido.', f'Usá uno de {sorted(CHARACTER_TYPES)}.')
            continue
        if not isinstance(actor.get('scale', 1), (int, float)) or actor.get('scale', 1) <= 0:
            err(f'{where}.scale', 'Debe ser un número positivo.')
        position = actor.get('position', [0, 0, 0])
        if not (isinstance(position, list) and len(position) == 3):
            err(f'{where}.position', 'Debe ser [x, y, z].', 'x separa personajes; usá ±1.6 para dos.')
        voice = actor.get('voice')
        if voice and voice.startswith('say:') and shutil.which('say') and voice[4:] not in {v for v, _ in say_voices()}:
            err(f'{where}.voice', f'La voz macOS «{voice[4:]}» no está instalada.', 'Mirá `voxel.py schema --voices`.')
        if actor['type'] == 'custom':
            parts = set()
            materials = actor.get('materials', {})
            for j, part in enumerate(actor.get('parts', [])):
                if part.get('id') in parts:
                    err(f'{where}.parts[{j}].id', f'Pieza duplicada «{part.get("id")}».')
                if 'parent' in part and part['parent'] not in parts:
                    err(f'{where}.parts[{j}].parent', f'Padre «{part["parent"]}» sin definir antes.',
                        'Declarar los padres antes que sus hijos.')
                if part.get('material') not in materials:
                    err(f'{where}.parts[{j}].material', f'Material «{part.get("material")}» no está en `materials`.')
                parts.add(part.get('id'))
            if not actor.get('parts'):
                err(f'{where}.parts', 'Un personaje custom necesita piezas.')
            for k in ('head', 'mouth'):
                if k in actor and actor[k] not in parts:
                    err(f'{where}.{k}', f'Control «{actor[k]}» no es una pieza.')
    stage = spec.get('stage', {})
    if stage.get('type', 'terminal') not in STAGES:
        err('stage.type', f'Escenario «{stage.get("type")}» desconocido.', f'Usá uno de {sorted(STAGES)}.')
    length = frames / fps if frames else None
    dialogue = spec.get('dialogue', [])
    if not isinstance(dialogue, list):
        err('dialogue', 'Debe ser una lista.')
        dialogue = []
    last_by_speaker = {}
    for i, line in enumerate(dialogue):
        where = f'dialogue[{i}]'
        if not isinstance(line, dict):
            err(where, 'Debe ser un objeto con speaker y text.')
            continue
        if line.get('speaker') not in actors:
            err(f'{where}.speaker', f'Personaje «{line.get("speaker")}» no está en `characters`.',
                f'Usá uno de {sorted(actors)}.')
        if 'audio' not in spec and not str(line.get('text', '')).strip() and not line.get('audio'):
            err(f'{where}.text', 'Línea sin texto ni audio.')
        start, end = line.get('start'), line.get('end')
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            err(where, 'Faltan `start` y `end` en segundos.', 'Quitá ambos de todas las líneas para tiempos automáticos.')
            continue
        if not start < end:
            err(where, f'start {start} debe ser menor que end {end}.')
        if length and not 0 <= start < end <= length + 1e-6:
            err(where, f'El intervalo {start}–{end} s sale de la duración {length:.2f} s.',
                'Aumentá `duration` o mové la línea.')
        for key in ('emotion',):
            if key in line and line[key] not in EXPRESSIONS:
                err(f'{where}.emotion', f'Emoción «{line[key]}» inválida.', f'Usá una de {sorted(EXPRESSIONS)}.')
        for j, beat in enumerate(line.get('beats') or []):
            if beat.get('emotion') and beat['emotion'] not in EXPRESSIONS:
                err(f'{where}.beats[{j}].emotion', f'Emoción «{beat["emotion"]}» inválida.', f'Usá una de {sorted(EXPRESSIONS)}.')
            if beat.get('icon') and beat['icon'] not in ICONS:
                err(f'{where}.beats[{j}].icon', f'Ícono «{beat["icon"]}» inválido.', f'Usá uno de {sorted(ICONS)}.')
        if 'pose' in line and line['pose'] not in POSES:
            err(f'{where}.pose', f'Pose «{line["pose"]}» inválida.', f'Usá una de {sorted(POSES)}.')
        speaker = line.get('speaker')
        if speaker in last_by_speaker and start < last_by_speaker[speaker]:
            warn(where, f'Se superpone con la línea anterior de {speaker}.', 'Las bocas se mezclan; separá los tiempos.')
        last_by_speaker[speaker] = end
        text = str(line.get('text', ''))
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start and len(text) / (end - start) > 26:
            warn(where, f'{len(text) / (end - start):.0f} caracteres por segundo: el subtítulo se lee mal.',
                 'Alargá el intervalo o partí la línea.')
        if len(text) > 120:
            warn(f'{where}.text', 'Más de 120 caracteres: el subtítulo ocupará más de tres renglones.', 'Partí la línea.')
        if directory and line.get('audio') and not (directory / line['audio']).is_file():
            err(f'{where}.audio', f'No existe {directory / line["audio"]}.')
    if directory and spec.get('audio') and not (directory / spec['audio']).is_file():
        err('audio', f'No existe la pista maestra {directory / spec["audio"]}.')
    shots = spec.get('shots')
    if not isinstance(shots, list) or not shots:
        err('shots', 'Faltan planos.', 'Quitá `shots` para que se generen desde el diálogo, o agregá {"frame": 0}.')
        shots = []
    starts = [shot.get('frame') for shot in shots if isinstance(shot, dict)]
    if shots and (any(type(v) is not int for v in starts) or starts[0] != 0 or sorted(set(starts)) != starts):
        err('shots', f'Los `frame` deben ser enteros únicos crecientes que empiecen en 0 (hay {starts}).')
    elif frames and starts and starts[-1] >= frames:
        err(f'shots[{len(shots) - 1}].frame', f'{starts[-1]} está fuera de los {frames} fotogramas.')
    for index, shot in enumerate(shots):
        where = f'shots[{index}]'
        if not isinstance(shot, dict):
            err(where, 'Debe ser un objeto.')
            continue
        if index + 1 < len(starts):
            shot['end_frame'] = starts[index + 1]
        elif frames:
            shot['end_frame'] = frames
        if shot.get('subject') is not None and shot['subject'] not in actors:
            err(f'{where}.subject', f'«{shot["subject"]}» no es un personaje.', 'Usá un ID de `characters` o null (plano general).')
        camera = shot.get('camera', {})
        for key in ('position', 'target', 'end_position'):
            if key in camera and not (isinstance(camera[key], list) and len(camera[key]) == 3):
                err(f'{where}.camera.{key}', 'Debe ser [x, y, z].')
        for name, acting in shot.get('acting', {}).items():
            at = f'{where}.acting.{name}'
            if name not in actors:
                err(at, f'«{name}» no es un personaje.')
                continue
            if acting.get('emotion', 'neutral') not in EXPRESSIONS:
                err(f'{at}.emotion', f'Emoción «{acting.get("emotion")}» inválida.', f'Usá una de {sorted(EXPRESSIONS)}.')
            if acting.get('pose', 'down') not in POSES:
                err(f'{at}.pose', f'Pose «{acting.get("pose")}» inválida.', f'Usá una de {sorted(POSES)}.')
            for j, item in enumerate(acting.get('expr_keys', [])):
                if not (isinstance(item, list) and len(item) == 2 and item[1] in EXPRESSIONS):
                    err(f'{at}.expr_keys[{j}]', f'Debe ser [fotograma, emoción] con emoción en {sorted(EXPRESSIONS)}.')
            for j, item in enumerate(acting.get('icons', [])):
                if not (isinstance(item, list) and len(item) in (2, 3) and item[1] in ICONS):
                    err(f'{at}.icons[{j}]', f'Debe ser [fotograma, ícono, duración?] con ícono en {sorted(ICONS)}.')
            for j, item in enumerate(acting.get('pose_keys', [])):
                if not (isinstance(item, list) and len(item) == 2 and item[1] in POSES):
                    err(f'{at}.pose_keys[{j}]', f'Debe ser [fotograma, pose] con pose en {sorted(POSES)}.')
            expected = {'head': 5, 'gaze': 3}
            tracks = {k: acting.get(k, []) for k in ('head', 'gaze', 'pose_keys')}
            custom_parts = {part.get('id') for part in actors[name].get('parts', [])}
            for part, motion in acting.get('parts', {}).items():
                if part not in custom_parts:
                    err(f'{at}.parts.{part}', 'Pieza inexistente en ese personaje.')
                tracks[f'parts.{part}'] = motion
            for key, sequence in tracks.items():
                if not isinstance(sequence, list) or any(not isinstance(row, list) or not row for row in sequence):
                    err(f'{at}.{key}', 'Debe ser una lista de claves [f, ...].')
                    continue
                times = [row[0] for row in sequence]
                if times != sorted(set(times)) or any(not isinstance(t, (int, float)) or t < 0 for t in times):
                    err(f'{at}.{key}', 'Claves duplicadas, negativas o desordenadas.', 'f es relativo al inicio del plano.')
                if key in expected and any(len(row) != expected[key] for row in sequence):
                    err(f'{at}.{key}', f'Cada clave lleva {expected[key]} valores.',
                        'head: [f, desplazamiento_x, inclinación, giro, balanceo]; gaze: [f, x, z].')
    subtitles_list = spec.get('subtitles')
    if subtitles_list is not None:
        for i, item in enumerate(subtitles_list):
            if not isinstance(item, dict) or not {'start', 'end', 'text'} <= item.keys():
                err(f'subtitles[{i}]', 'Debe tener start, end y text.')
    return errors, warnings


def validate(spec, directory=None):
    """Compatibilidad: valida y devuelve el guion o lanza VoxelError con todos los errores."""
    errors, warnings = check(spec, directory)
    if errors:
        first = errors[0]
        raise VoxelError(f'{len(errors)} error(es) en el guion. Primero: {first["path"]}: {first["message"]}',
                         EXIT_SPEC, first.get('hint'), errors)
    spec['_warnings'] = warnings
    return spec


def prepare(path, synthesize=True):
    source = Path(path).resolve()
    spec = load_spec(source)
    notes = []
    resolve_characters(spec)
    expand(spec, source.parent, synthesize, notes)
    spec = validate(spec, source.parent)
    return source, spec, notes


def tempo(value):
    factors = []
    while value > 2:
        factors.append(2)
        value /= 2
    while value < .5:
        factors.append(.5)
        value /= .5
    return ','.join(f'atempo={v:.7f}' for v in [*factors, value])


def pcm(path):
    with wave.open(str(path), 'rb') as stream:
        return array('h', stream.readframes(stream.getnframes()))


def audio_track(ffmpeg, spec, directory, scratch):
    duration = spec['frames'] / spec['fps']
    analysis = scratch / 'analysis.wav'
    mix = scratch / 'mix.wav'
    if 'audio' in spec:
        master = (directory / spec['audio']).resolve()
        run(ffmpeg, '-v', 'error', '-i', master, '-af', 'apad', '-t', duration,
            '-ar', RATE, '-ac', 1, analysis)
        return master, pcm(analysis), {}
    inputs, delays, tracks = [], [], {}
    for i, line in enumerate(spec.get('dialogue', [])):
        if not line.get('audio') and not line.get('text', '').strip():
            continue
        source = voice_clip(spec, i, line, directory)
        offset = line.get('clip_offset', 0)
        length = line.get('clip_length') or (media_length(source) - offset)
        if length <= 0:
            raise VoxelError(f'La pista de diálogo está vacía: {source}', EXIT_SPEC, 'Revisá el texto o el audio.')
        span = line['end'] - line['start']
        factor = length / span
        # «shrink» (por defecto) solo acelera la voz si no entra en el intervalo; nunca la estira.
        if line.get('fit', spec.get('fit_voice', 'shrink')) == 'shrink':
            factor = max(1.0, factor)
        clip = scratch / f'aligned-{i:04}.wav'
        # Fundido corto de entrada y salida: sin clics en los bordes de cada clip de voz.
        fade_out = max(0.0, min(length / factor, span) - .04)
        seek = ['-ss', f'{offset:.3f}'] if offset else []
        run(ffmpeg, '-v', 'error', *seek, '-i', source, '-af',
            f'{tempo(factor)},afade=t=in:d=0.01,afade=t=out:st={fade_out:.3f}:d=0.04,apad',
            '-t', span, '-ac', 1, '-ar', MIX_RATE, clip)
        low = scratch / f'aligned-{i:04}-16k.wav'
        run(ffmpeg, '-v', 'error', '-i', clip, '-ar', RATE, low)
        tracks[i] = pcm(low)
        inputs.append(clip)
        delays.append(round(line['start'] * 1000))
    if inputs:
        graph = ';'.join(f'[{k}]adelay={d}:all=1[a{k}]' for k, d in enumerate(delays))
        graph += ';' + ''.join(f'[a{k}]' for k in range(len(inputs)))
        graph += f'amix=inputs={len(inputs)}:normalize=0:dropout_transition=0,alimiter=limit=0.95,apad,atrim=0:{duration:.6f}[out]'
        run(ffmpeg, '-v', 'error', *[x for clip in inputs for x in ('-i', clip)], '-filter_complex', graph,
            '-map', '[out]', '-ac', 1, '-ar', MIX_RATE, '-y', mix)
    else:
        run(ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', f'anullsrc=r={MIX_RATE}:cl=mono', '-t', duration, '-y', mix)
    run(ffmpeg, '-v', 'error', '-i', mix, '-ar', RATE, '-ac', 1, '-y', analysis)
    return mix, pcm(analysis), tracks


def phonetic_tokens(text):
    basic = unicodedata.normalize('NFD', text.lower())
    basic = ''.join(letter for letter in basic if not unicodedata.combining(letter))
    return re.findall(r'ch|ll|rr|qu|[a-z0-9]', basic)


def visemes(spec, samples, clips, measured):
    fps = spec['fps']
    result = {name: [[0.0, .68] for _ in range(spec['frames'])] for name in spec['characters']}
    for line_id, line in enumerate(spec.get('dialogue', [])):
        tape = result[line['speaker']]
        start, end = line['start'], line['end']
        individual = clips.get(line_id)
        chunk = individual if individual is not None else samples[round(start * RATE):round(end * RATE)]
        frames = range(math.ceil(start * fps - 1e-7), min(len(tape), math.ceil(end * fps - 1e-7)))
        tokens = line.get('tokens')
        token_times = [item[0] for item in tokens] if tokens else []
        letters = phonetic_tokens(clean_text(line.get('text', ''))) or ['a']
        readings = []
        for frame in frames:
            offset = max(0, (frame + .5) / fps - start)
            middle = int(offset * RATE)
            window = chunk[max(0, middle - 250):middle + 250]
            readings.append(math.sqrt(sum(x * x for x in window) / max(1, len(window))))
        if not readings:
            continue
        ceiling = max(300., sorted(readings)[int((len(readings) - 1) * .87)])
        for frame, rms in zip(frames, readings):
            timestamp = (frame + .5) / fps
            offset = timestamp - start
            if tokens:
                i = max(0, bisect.bisect_right(token_times, timestamp) - 1)
                a, b, token = tokens[i]
                local = phonetic_tokens(token) or [' ']
                idx = int(max(0, (timestamp - a) / max(.02, b - a)) * len(local))
                letter = local[min(len(local) - 1, idx)]
            else:
                letter = letters[min(len(letters) - 1, int(max(0, offset) * len(letters) / (end - start)))]
            volume = max(0, min(1, (rms / ceiling - .11) / .89))
            opening, width = (.63, .93)
            if letter in ('p', 'b', 'm'):
                opening, width = (.07, .95)
            elif letter in ('o', 'u'):
                opening, width = (.98, .62)
            elif letter in ('e', 'i', 's'):
                opening, width = (.53, 1.10)
            elif letter == 'a':
                opening, width = (1.10, 1.07)
            value = min(1, volume ** .59 * opening)
            if value >= tape[frame][0]:
                tape[frame] = [round(value, 3), round(width if volume > .06 else .68, 2)]
    if measured:
        shots = spec['shots']
        starts = [shot['frame'] for shot in shots]
        for key, (opening, width) in measured.items():
            frame = int(key)
            if not 0 <= frame < spec['frames']:
                continue
            shot = shots[bisect.bisect_right(starts, frame) - 1]
            if (actor := shot.get('subject')) is None:
                continue
            old_open, old_width = result[actor][frame]
            result[actor][frame] = [.74 * opening + .26 * old_open, .75 * width + .25 * old_width]
    return result


def ass_time(seconds):
    ticks = max(0, round(seconds * 100))
    return f'{ticks // 360000}:{ticks // 6000 % 60:02}:{ticks // 100 % 60:02}.{ticks % 100:02}'


def subtitles(spec, destination, start, end):
    timecode = start / spec['fps']
    limit = end / spec['fps']
    config = spec.get('captions', {})
    out = [
        '[Script Info]', 'ScriptType: v4.00+', 'PlayResX: 720', 'PlayResY: 1280', 'WrapStyle: 2',
        '[V4+ Styles]',
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
        f'Style: Dialog,Arial,{config.get("size", 31)},&H00FFFFFF,&H00FFFFFF,&H82000000,&H80000000,-1,0,0,0,100,100,0,0,1,1.1,0.55,5,44,44,0,1',
        '[Events]', 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text',
    ]
    segments = spec.get('subtitles')
    if segments is None:
        segments = [dict(start=d['start'], end=d['end'], text=clean_text(d.get('text', '')))
                    for d in spec.get('dialogue', [])]
    for segment in segments:
        left, right = max(segment['start'], timecode), min(segment['end'], limit)
        if left >= right or not segment['text'].strip():
            continue
        lines = segment['text'].splitlines()
        if len(lines) == 1:
            lines = textwrap.wrap(lines[0], width=config.get('wrap', 43))
        content = r'\N'.join(line.replace('\\', '＼').replace('{', '｛').replace('}', '｝') for line in lines)
        y = segment.get('y', config.get('y', 359))
        fade = segment.get('fade_ms', 0)
        position = r'\pos(360,' + str(y) + ')'
        fade_in = max(0, fade - round((left - segment['start']) * 1000))
        fade_out = max(0, fade - round((segment['end'] - right) * 1000))
        transition = fr'\fad({fade_in},{fade_out})' if fade_in or fade_out else ''
        tags = '{' + position + transition + '}'
        out.append(f'Dialogue: 0,{ass_time(left - timecode)},{ass_time(right - timecode)},Dialog,,0,0,0,,{tags}{content}')
    destination.write_text('\n'.join(out) + '\n', encoding='utf-8')


def subtitle_segments(spec):
    segments = spec.get('subtitles')
    if segments is None:
        segments = [dict(start=d['start'], end=d['end'], text=clean_text(d.get('text', '')))
                    for d in spec.get('dialogue', [])]
    y = spec.get('captions', {}).get('y', 359)
    return [dict(s, y=s.get('y', y)) for s in segments if str(s.get('text', '')).strip()]


# ---------------------------------------------------------------- render

def blender_job(blender, job_file, log_path, frames_total, quiet):
    command = [blender, '-b', '--factory-startup', '--python-exit-code', '1', '--python',
               str(ROOT / 'studio/blender_scene.py'), '--', str(job_file)]
    started = time.time()
    done = 0
    with open(log_path, 'w', encoding='utf-8') as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   errors='replace')
        for line in process.stdout:
            log.write(line)
            if line.startswith('Saved:') and '.png' in line:
                done += 1
                log.flush()
                if done == 1 or done % 30 == 0 or done == frames_total:
                    elapsed = time.time() - started
                    rest = elapsed / done * (frames_total - done)
                    say(f'  render {done}/{frames_total} fotogramas · {elapsed:.0f} s · faltan ~{rest:.0f} s', quiet)
        code = process.wait()
    if code:
        tail = log_path.read_text(encoding='utf-8', errors='replace').splitlines()[-25:]
        trace = [l for l in tail if l.strip()]
        hint = 'Mirá el log. Si es un error de Python en studio/, corregí el guion o el modelo y repetí con preview.'
        if any('MemoryError' in l or 'out of memory' in l.lower() for l in trace):
            hint = 'Sin memoria: bajá --scale o --samples, o renderizá por tramos con --range.'
        raise VoxelError('Blender falló al construir o renderizar la escena.', EXIT_RENDER, hint,
                         log=str(log_path), errors=[dict(path='blender', message='\n'.join(trace[-12:]))])
    return time.time() - started


def find_modal_python():
    """Intérprete con el paquete `modal`: VOXEL_MODAL_PYTHON, el propio, o el de la CLI instalada con uv/pipx."""
    candidates = [os.environ.get('VOXEL_MODAL_PYTHON'), sys.executable]
    cli = shutil.which('modal')
    if cli:
        first = Path(cli).read_text(errors='ignore').splitlines()[0] if Path(cli).is_file() else ''
        if first.startswith('#!'):
            candidates.append(first[2:].strip())
    for python in filter(None, candidates):
        if shutil.which(python) or Path(python).is_file():
            probe = subprocess.run([python, '-c', 'import modal'], capture_output=True)
            if probe.returncode == 0:
                return python
    return None


def cloud_job(job_file, scratch, blend, frames_total, args, quiet):
    python = find_modal_python()
    if not python:
        raise VoxelError('No encontré el paquete modal.', EXIT_DEPENDENCY,
                         'Instalá la CLI (uv tool install modal) y autenticá con `modal setup`, '
                         'o exportá VOXEL_MODAL_PYTHON=/ruta/al/python.')
    command = [python, str(ROOT / 'studio/cloud_modal.py'), str(job_file), str(scratch)]
    if args.chunks:
        command += ['--chunks', str(args.chunks)]
    if blend:
        command += ['--blend', str(blend)]
    env = dict(os.environ)
    if args.gpu:
        env['VOXEL_MODAL_GPU'] = args.gpu
    if args.modal_profile:
        env['MODAL_PROFILE'] = args.modal_profile
    say(f'  nube: Modal · GPU {env.get("VOXEL_MODAL_GPU", "L4,A10G,T4")} · cuenta {env.get("MODAL_PROFILE", "activa")}', quiet)
    started = time.time()
    for attempt in range(1, 4):
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                                   errors='replace')
        lines = []
        try:
            for line in process.stdout:
                lines.append(line.rstrip())
                if line.strip() and not line.lstrip().startswith(('│', '╭', '╰', '├', '└', '🔨', '┃', '┏', '┗', '┡')):
                    progress('  ' + line.rstrip())
                    if not quiet:
                        print('  ' + line.rstrip(), file=sys.stderr, flush=True)
        except BaseException:
            # Cortaron voxel.py: el hijo recibe SIGTERM, detiene el app en Modal y libera las GPUs.
            process.terminate()
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
            raise
        code = process.wait()
        # La conexión con Modal a veces no se abre a tiempo (red lenta): reintentar antes de fallar.
        connecting = any('create_channel' in l or 'TimeoutError' in l for l in lines) and not any(
            'fotogramas recibidos' in l for l in lines)
        if code and connecting and attempt < 3:
            say(f'  nube: la conexión con Modal no respondió; reintento {attempt + 1}/3…', quiet)
            time.sleep(5 * attempt)
            continue
        break
    if code:
        tail = [l for l in lines if l.strip()][-15:]
        hint = 'Mirá la traza. Si es de autenticación, corré `modal setup` o elegí otra cuenta con --modal-profile.'
        if any('credit' in l.lower() or 'billing' in l.lower() or 'spend' in l.lower() for l in tail):
            hint = 'La cuenta de Modal no tiene créditos: usá otra con --modal-profile o VOXEL_MODAL_PROFILE.'
        raise VoxelError('El render en Modal falló.', EXIT_RENDER, hint, errors=[dict(path='modal', message='\n'.join(tail))])
    return time.time() - started


def take_folder(source):
    """videos/<proyecto>/guion.json → videos/<proyecto>/tomas/toma-NN (la siguiente libre), o None."""
    if source.name != 'guion.json':
        return None
    root = source.parent / 'tomas'
    numbers = [int(m[1]) for d in root.glob('toma-*') if (m := re.match(r'toma-(\d+)$', d.name))] if root.is_dir() else []
    return root / f'toma-{max(numbers, default=0) + 1:02}'


def render(args, quiet=False):
    ffmpeg = find_ffmpeg()
    blender = find_blender()
    if not getattr(args, 'cloud', None):
        args.cloud = os.environ.get('VOXEL_CLOUD') or None
    if not blender and args.cloud != 'modal':
        raise VoxelError('No encontré Blender.', EXIT_DEPENDENCY,
                         'Instalá Blender en /Applications o exportá BLENDER=/ruta/al/ejecutable.')
    if not find_ffprobe(ffmpeg):
        raise VoxelError('No encontré ffprobe.', EXIT_DEPENDENCY, 'Viene con ffmpeg.')
    source, spec, notes = prepare(args.project)
    for note in notes:
        say(f'nota: {note}', quiet)
    for w in spec.pop('_warnings', []):
        say(f'aviso: {w["path"]}: {w["message"]}', quiet)
    start, end = args.range or (0, spec['frames'])
    if not 0 <= start < end <= spec['frames']:
        raise VoxelError(f'Rango {start}–{end} fuera de los {spec["frames"]} fotogramas.', EXIT_SPEC,
                         'El fin es exclusivo y los fotogramas empiezan en 0.')
    if not 1 <= args.scale <= 100 or args.samples < 1:
        raise VoxelError('--scale va de 1 a 100 y --samples debe ser positivo.', EXIT_SPEC)
    take = None
    if not args.output:
        take = take_folder(source)
        if take is None:
            args.output = ROOT / 'renders' / f'{source.stem}.mp4'
        else:
            # Proyecto con tomas: video, subtítulos, escena de Blender y guion resuelto juntos por versión.
            take.mkdir(parents=True)
            args.output = take / 'video.mp4'
            args.blend = args.blend or take / 'escena.blend'
            args.srt = True
    output = Path(args.output).resolve()
    blend = Path(args.blend).resolve() if args.blend else None
    output.parent.mkdir(parents=True, exist_ok=True)
    if blend:
        blend.parent.mkdir(parents=True, exist_ok=True)
    if args.scratch and not Path(args.scratch).is_dir():
        raise VoxelError(f'--scratch {args.scratch} no existe.', EXIT_SPEC)
    log_path = output.with_suffix('.log')
    label = f'{source.parent.name}-{take.name}' if take else output.stem
    PROGRESS['file'] = Path(os.environ.get('VOXEL_PROGRESS_DIR') or Path.cwd() / '.voxel' / 'progreso') / f'{label}.txt'
    PROGRESS['file'].parent.mkdir(parents=True, exist_ok=True)
    PROGRESS['file'].write_text('', encoding='utf-8')
    say(f'render {source.name}: fotogramas {start}–{end} de {spec["frames"]}, {args.scale}% · {args.samples} muestras', quiet)
    with tempfile.TemporaryDirectory(prefix='voxel-', dir=args.scratch) as name:
        scratch = Path(name)
        try:
            audio, samples, clips = audio_track(ffmpeg, spec, source.parent, scratch)
        except subprocess.CalledProcessError as exc:
            raise VoxelError(f'FFmpeg no pudo preparar el audio: {exc}', EXIT_RENDER,
                             'Revisá que los archivos de audio se puedan abrir.') from exc
        subtitles(spec, scratch / 'dialogue.ass', start, end)
        measured_file = spec.get('visual_visemes')
        measured = json.loads((source.parent / measured_file).read_text()) if measured_file else {}
        job = dict(spec=spec, visemes=visemes(spec, samples, clips, measured), start=start, end=end,
                   frame_path=str(scratch / 'shot_'), blend=str(blend) if blend else None,
                   scale=args.scale, samples=args.samples)
        (scratch / 'job.json').write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        if args.cloud == 'modal':
            seconds = cloud_job(scratch / 'job.json', scratch, blend, end - start, args, quiet)
        else:
            seconds = blender_job(blender, scratch / 'job.json', log_path, end - start, quiet)
        duration = (end - start) / spec['fps']
        master = 'audio' in spec and start == 0
        filter_graph = 'pad=ceil(iw/2)*2:ceil(ih/2)*2,ass=dialogue.ass'
        encoding = ['-c:a', 'copy'] if master and audio.suffix.lower() in ('.mp4', '.m4a', '.aac') else [
            '-af', 'apad', '-c:a', 'aac', '-b:a', '192k']
        seek = ['-ss', f'{start / spec["fps"]:.9f}'] if start else []
        try:
            run(ffmpeg, '-nostdin', '-v', 'error', '-framerate', spec['fps'], '-start_number', start + 1,
                '-i', 'shot_%04d.png', *seek, '-i', audio,
                '-map', '0:v:0', '-map', '1:a:0', '-vf', filter_graph, '-t', f'{duration:.9f}',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', 16, '-pix_fmt', 'yuv420p',
                *encoding, '-movflags', '+faststart', '-y', 'final.mp4', cwd=scratch,
                capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise VoxelError('FFmpeg falló al unir imagen, audio y subtítulos.', EXIT_RENDER,
                             'Comprobá `voxel.py doctor` (libass) y el espacio en disco.',
                             errors=[dict(path='ffmpeg', message=(exc.stderr or '')[-1500:])]) from exc
        with tempfile.NamedTemporaryFile(prefix='.voxel-', dir=output.parent, delete=False) as pending:
            pending_path = Path(pending.name)
        try:
            shutil.copyfile(scratch / 'final.mp4', pending_path)
            os.chmod(pending_path, 0o644)
            pending_path.replace(output)
        finally:
            pending_path.unlink(missing_ok=True)
        if args.srt:
            write_srt(spec, output.with_suffix('.srt'), start, end)
    if args.cloud != 'modal':
        log_path.unlink(missing_ok=True)
    result = dict(output=str(output), frames=[start, end], seconds=round(duration, 3),
                  render_seconds=round(seconds, 1), scale=args.scale, samples=args.samples,
                  where=args.cloud or 'local')
    if take:
        result['take'] = str(take)
        snapshot = {k: v for k, v in spec.items() if not k.startswith('_')}
        for shot in snapshot.get('shots', []):
            shot.pop('end_frame', None)
        (take / 'guion.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if not args.no_verify:
        report = verify_file(output, spec, (start, end), quiet=quiet)
        result['verify'] = report
        if take:
            (take / 'verificacion.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if not report['ok']:
            failed = [c for c in report['checks'] if not c['ok']]
            raise VoxelError(f'El MP4 se generó pero no pasó la verificación: {failed[0]["name"]}: {failed[0]["detail"]}',
                             EXIT_VERIFY, failed[0].get('hint'),
                             errors=[dict(path=c['name'], message=c['detail']) for c in failed])
    return result


def write_srt(spec, destination, start=0, end=None):
    fps = spec['fps']
    offset = start / fps
    limit = (end or spec['frames']) / fps

    def stamp(t):
        ms = max(0, round(t * 1000))
        return f'{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}'

    rows = []
    for segment in subtitle_segments(spec):
        a, b = max(segment['start'], offset), min(segment['end'], limit)
        if a < b:
            rows.append(f'{len(rows) + 1}\n{stamp(a - offset)} --> {stamp(b - offset)}\n{segment["text"]}\n')
    destination.write_text('\n'.join(rows), encoding='utf-8')


def preview_range(spec, at=None, length=3.0):
    fps, frames = spec['fps'], spec['frames']
    span = max(1, round(length * fps))
    if at is not None:
        start = round(at * fps)
    elif len(spec['shots']) > 1:
        start = spec['shots'][1]['frame'] - span // 2
    else:
        start = 0
    start = max(0, min(start, frames - span))
    return start, min(frames, start + span)


# ---------------------------------------------------------------- verificación

def probe(path):
    ffprobe = find_ffprobe(find_ffmpeg(required=False)) or 'ffprobe'
    out = run(ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path,
              capture_output=True, text=True).stdout
    return json.loads(out)


def grab(ffmpeg, path, t, crop=None, gray=False):
    """Decodifica un fotograma en `t` segundos y devuelve (ancho, alto, bytes RGB o gris)."""
    vf = []
    if crop:
        vf.append('crop={}:{}:{}:{}'.format(*crop))
    vf.append('format=gray' if gray else 'format=rgb24')
    raw = run(ffmpeg, '-v', 'error', '-ss', f'{t:.4f}', '-i', path, '-frames:v', 1, '-vf', ','.join(vf),
              '-f', 'rawvideo', '-', capture_output=True).stdout
    return raw


def caption_score(raw, width, max_run):
    """Cuenta trazos finos casi blancos con caída de brillo a ambos lados en cada renglón: el
    patrón del texto quemado. Fondos blancos amplios (camiseta) y texto desenfocado no suman."""
    pixels = len(raw) // 3
    height = pixels // width
    lum = [0] * pixels
    white = bytearray(pixels)
    for i in range(pixels):
        r, g, b = raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]
        lum[i] = (r * 2 + g * 5 + b) >> 3
        if min(r, g, b) >= 205 and max(r, g, b) - min(r, g, b) <= 45:
            white[i] = 1
    score = 0
    for y in range(height):
        row = y * width
        x = 1
        while x < width - 1:
            if white[row + x]:
                begin = x
                while x < width - 1 and white[row + x]:
                    x += 1
                if x - begin <= max_run:
                    peak = max(lum[row + begin:row + x])
                    if peak - lum[row + max(0, begin - 2)] >= 55 and peak - lum[row + min(width - 1, x + 1)] >= 55:
                        score += 1
            x += 1
    return score


def verify_file(path, spec=None, frame_range=None, quiet=False):
    path = Path(path)
    checks = []

    def add(name, ok, detail, hint=None):
        checks.append(dict(name=name, ok=bool(ok), detail=detail, hint=hint))

    if not path.is_file() or path.stat().st_size == 0:
        add('archivo', False, f'No existe o está vacío: {path}', 'Volvé a renderizar.')
        return dict(ok=False, checks=checks, file=str(path))
    ffmpeg = find_ffmpeg()
    info = probe(path)
    video = next((s for s in info['streams'] if s['codec_type'] == 'video'), None)
    audio = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
    add('pista de video', video and video.get('codec_name') == 'h264',
        f'{video.get("codec_name")} {video.get("width")}×{video.get("height")} {video.get("pix_fmt")}' if video else 'sin video')
    if not video:
        return dict(ok=False, checks=checks, file=str(path))
    width, height = int(video['width']), int(video['height'])
    add('vertical', height > width, f'{width}×{height}', 'Usá resolution [720, 1280].')
    add('pista de audio', audio and audio.get('codec_name') == 'aac',
        f'{audio.get("codec_name")} {audio.get("sample_rate")} Hz' if audio else 'sin audio')
    decode = subprocess.run([ffmpeg, '-v', 'error', '-i', str(path), '-map', '0', '-f', 'null', '-'],
                            capture_output=True, text=True)
    add('decodificación completa', decode.returncode == 0 and not decode.stderr.strip(),
        'sin errores' if decode.returncode == 0 and not decode.stderr.strip() else decode.stderr.strip()[-400:],
        'El archivo está dañado: volvé a renderizar.')
    counted = run(find_ffprobe(ffmpeg), '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
                  'stream=nb_read_frames,r_frame_rate', '-of', 'json', path, capture_output=True, text=True).stdout
    stream = json.loads(counted)['streams'][0]
    frames = int(stream['nb_read_frames'])
    num, den = (int(x) for x in stream['r_frame_rate'].split('/'))
    fps = num / den
    duration = float(info['format']['duration'])
    result = dict(file=str(path), width=width, height=height, fps=round(fps, 3), frames=frames,
                  duration=round(duration, 3), size_mb=round(path.stat().st_size / 2 ** 20, 2))
    if spec:
        start, end = frame_range or (0, spec['frames'])
        add('fotogramas', frames == end - start, f'{frames} de {end - start} esperados')
        add('cadencia', abs(fps - spec['fps']) < .01, f'{fps:.3f} fps')
        offset = start / spec['fps']
        limit = end / spec['fps']
        # Audio: cada línea debe sonar por encima del silencio.
        if audio:
            pcm_raw = run(ffmpeg, '-v', 'error', '-i', path, '-vn', '-ac', 1, '-ar', 8000, '-f', 's16le', '-',
                          capture_output=True).stdout
            samples = array('h', pcm_raw[:len(pcm_raw) // 2 * 2])
            quiet_lines = []
            for i, line in enumerate(spec.get('dialogue', [])):
                a, b = max(line['start'], offset), min(line['end'], limit)
                if b - a < .1:
                    continue
                chunk = samples[int((a - offset) * 8000):int((b - offset) * 8000)]
                rms = math.sqrt(sum(x * x for x in chunk) / max(1, len(chunk)))
                db = 20 * math.log10(max(rms, 1) / 32768)
                if db < -45:
                    quiet_lines.append(f'línea {i} ({db:.0f} dBFS)')
            add('audio en cada línea', not quiet_lines, 'todas suenan' if not quiet_lines else ', '.join(quiet_lines),
                'Revisá la voz o el archivo de audio de esas líneas.')
        # Subtítulos: muestreo al principio, en el medio y al final de cada intervalo.
        scale = height / CANVAS[1]
        band = round(150 * scale)
        stroke = max(3, round(7 * scale))
        threshold = max(15, round(50 * scale))
        missing, seen, timeline = [], 0, []
        baseline_t = []
        segments = subtitle_segments(spec)
        for i, segment in enumerate(segments):
            a, b = max(segment['start'], offset), min(segment['end'], limit)
            if b - a < .15:
                continue
            seen += 1
            top = max(0, min(height - band, round(segment['y'] * scale) - band // 2))
            # Con fundido (fade_ms) el texto es semitransparente en los bordes: medir después de la entrada
            # y antes de la salida, sin cruzar el centro en subtítulos cortos.
            edge = min((b - a) / 2 - .02, .1 + segment.get('fade_ms', 0) / 1000)
            probes = sorted({round(a + edge, 3), round((a + b) / 2, 3), round(b - edge, 3)})
            scores = [caption_score(grab(ffmpeg, path, t - offset, (width, band, 0, top)), width, stroke) for t in probes]
            ok = all(score >= threshold for score in scores)
            timeline.append(dict(index=i, start=segment['start'], end=segment['end'], scores=scores, ok=ok))
            if not ok:
                missing.append(f'subtítulo {i} «{segment["text"][:30]}» (puntajes {scores}, mínimo {threshold})')
        # Control negativo: en huecos sin subtítulo la franja no debería tener texto.
        gaps = []
        ordered = sorted((s['start'], s['end']) for s in segments)
        for (a1, b1), (a2, b2) in zip(ordered, ordered[1:]):
            if a2 - b1 >= .25 and offset <= (b1 + a2) / 2 <= limit:
                gaps.append((b1 + a2) / 2)
        false_hits = []
        y_default = spec.get('captions', {}).get('y', 359)
        for t in gaps[:4]:
            top = max(0, min(height - band, round(y_default * scale) - band // 2))
            score = caption_score(grab(ffmpeg, path, t - offset, (width, band, 0, top)), width, stroke)
            if score >= threshold:
                false_hits.append(f'{t:.2f}s ({score})')
        add('subtítulos en sus intervalos', seen and not missing,
            f'{seen} subtítulos presentes en inicio, medio y final' if seen and not missing else
            ('ningún subtítulo cae en el rango' if not seen else '; '.join(missing)),
            'Revisá `captions.y`, el texto o que ffmpeg tenga libass.')
        if gaps:
            add('sin subtítulos en los huecos', not false_hits,
                f'{len(gaps[:4])} huecos limpios' if not false_hits else 'texto en ' + ', '.join(false_hits),
                'Un subtítulo dura más de lo previsto o el fondo tiene texto blanco en esa franja.')
        result['subtitles'] = timeline
    ok = all(c['ok'] for c in checks)
    result.update(ok=ok, checks=checks)
    for c in checks:
        say(f'  {"✓" if c["ok"] else "✗"} {c["name"]}: {c["detail"]}', quiet)
    return result


# ---------------------------------------------------------------- análisis de referencia

def analyze(args, quiet=False):
    video = Path(args.video).resolve()
    if not video.is_file():
        raise VoxelError(f'No existe {video}.', EXIT_SPEC)
    ffmpeg = find_ffmpeg()
    out = Path(args.out or ROOT / 'analysis' / re.sub(r'[^\w-]+', '-', video.stem)[:40].strip('-')).resolve()
    (out / 'frames').mkdir(parents=True, exist_ok=True)
    info = probe(video)
    stream = next(s for s in info['streams'] if s['codec_type'] == 'video')
    num, den = (int(x) for x in stream['r_frame_rate'].split('/'))
    fps = num / den
    duration = float(info['format']['duration'])
    say(f'analizando {video.name}: {stream["width"]}×{stream["height"]}, {fps:.2f} fps, {duration:.2f} s', quiet)
    detect = subprocess.run([ffmpeg, '-v', 'info', '-i', str(video), '-vf',
                             f"select='gt(scene,{args.threshold})',showinfo", '-an', '-f', 'null', '-'],
                            capture_output=True, text=True)
    cuts = sorted({round(float(m) * fps) for m in re.findall(r'pts_time:([\d.]+)', detect.stderr)})
    bounds = [0, *cuts, round(duration * fps)]
    shots = []
    for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
        if b <= a:
            continue
        middle = (a + b) / 2 / fps
        frame_file = out / 'frames' / f'plano-{i:02}-f{a:05}.png'
        run(ffmpeg, '-v', 'error', '-ss', f'{middle:.3f}', '-i', video, '-frames:v', 1, '-y', frame_file)
        shots.append(dict(index=i, frame=a, end_frame=b, start=round(a / fps, 3), end=round(b / fps, 3),
                          still=str(frame_file)))
    run(ffmpeg, '-v', 'error', '-i', video, '-vf',
        f"fps={min(2, 40 / max(duration, 1)):.4f},scale=180:-2,tile=8x5", '-frames:v', 1, '-y', out / 'contacto.png')
    has_audio = any(s['codec_type'] == 'audio' for s in info['streams'])
    transcript = []
    if has_audio:
        run(ffmpeg, '-v', 'error', '-i', video, '-vn', '-c:a', 'copy', '-y', out / 'audio.m4a')
        run(ffmpeg, '-v', 'error', '-i', video, '-vn', '-ac', 1, '-ar', 16000, '-y', out / 'audio16k.wav')
        whisper, model = find_whisper()
        if whisper and model and not args.no_transcribe:
            say('transcribiendo con whisper…', quiet)
            subprocess.run([whisper, '-m', model, '-f', str(out / 'audio16k.wav'), '-l', args.language, '-oj',
                            '-ml', '42', '-sow', '-np', '-of', str(out / 'transcripcion')],
                           capture_output=True, text=True)
            data_file = out / 'transcripcion.json'
            if data_file.is_file():
                data = json.loads(data_file.read_text(encoding='utf-8', errors='replace'))
                for seg in data.get('transcription', []):
                    o = seg.get('offsets', {})
                    transcript.append(dict(start=o.get('from', 0) / 1000, end=o.get('to', 0) / 1000,
                                           text=seg.get('text', '').strip()))
        elif not args.no_transcribe:
            say('aviso: sin whisper; transcribí a mano o instalá whisper-cpp.', quiet)
    # Borrador de guion: diálogo con tiempos de la transcripción y planos en los cortes detectados.
    draft = dict(title=f'Recreación de {video.stem[:40]}', fps=round(fps), frames=round(duration * fps),
                 resolution=[int(stream['width']), int(stream['height'])], language=args.language,
                 audio=os.path.relpath(out / 'audio.m4a', ROOT / 'examples') if has_audio else None,
                 characters={'A': {'type': 'person', 'position': [-1.64, 0, 0]},
                             'B': {'type': 'cube', 'position': [1.61, .2, 0], 'scale': .88}},
                 stage={'type': 'terminal'},
                 dialogue=[dict(speaker='?', start=round(t['start'], 2), end=round(t['end'], 2), text=t['text'])
                           for t in transcript if t['text']],
                 shots=[dict(frame=s['frame'], subject='?', acting={}) for s in shots])
    if not has_audio:
        draft.pop('audio')
    (out / 'borrador.json').write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding='utf-8')
    report = dict(video=str(video), out=str(out), fps=fps, duration=duration,
                  resolution=[int(stream['width']), int(stream['height'])], cuts=cuts, shots=shots,
                  transcript=transcript, contact_sheet=str(out / 'contacto.png'),
                  draft=str(out / 'borrador.json'),
                  next_steps=['Mirá contacto.png y cada frames/plano-*.png: quién habla, emoción, pose y encuadre.',
                              'Completá speaker y subject (los «?») en borrador.json; ajustá textos con lo que se ve '
                              'en los subtítulos del original.',
                              'Copiá el borrador a examples/, corré validate y luego preview.'])
    (out / 'analisis.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    say(f'{len(shots)} planos, {len(transcript)} segmentos de voz → {out}', quiet)
    return report


# ---------------------------------------------------------------- creación y referencia

def new_script(args):
    cast = {}
    for item in (args.cast or 'A=person,B=cube').split(','):
        name, _, kind = item.partition('=')
        kind = TYPE_ALIASES.get(kind.strip(), kind.strip())
        if kind in preset_names():
            cast[name.strip()] = {'preset': kind}
        elif kind in CHARACTER_TYPES - {'custom'}:
            cast[name.strip()] = {'type': kind}
        else:
            raise VoxelError(f'--cast: tipo «{kind}» inválido para {name}.', EXIT_SPEC,
                             f'Usá person, cube o un preset: {preset_names()}.')
    positions = {1: [[0, 0, 0]], 2: [[-1.64, 0, 0], [1.61, .2, 0]], 3: [[-3.2, 0, 0], [0, .2, 0], [3.2, 0, 0]]}
    slots = positions.get(len(cast)) or [[(i - (len(cast) - 1) / 2) * 3.2, 0, 0] for i in range(len(cast))]
    for (name, actor), pos in zip(cast.items(), slots):
        actor['position'] = pos
        if actor.get('type') == 'cube':
            actor['scale'] = .88
    dialogue = []
    for raw in args.line:
        match = re.match(r'^\s*([^\s:\[]+)\s*(?:\[([^\]]*)\])?\s*:\s*(.+)$', raw)
        if not match or match[1] not in cast:
            raise VoxelError(f'--line «{raw}» no tiene la forma «ID: texto» con un ID de {sorted(cast)}.', EXIT_SPEC,
                             'Ejemplo: --line "A[question]: ¿Probamos?" --line "B[happy]: Sí."')
        line = {'speaker': match[1], 'text': match[3].strip()}
        for tag in filter(None, (t.strip() for t in (match[2] or '').split(','))):
            if tag in EXPRESSIONS:
                line['emotion'] = tag
            elif tag in POSES:
                line['pose'] = tag
            elif tag in SHOT_KINDS:
                line['shot'] = tag
            else:
                raise VoxelError(f'Etiqueta «{tag}» desconocida en «{raw}».', EXIT_SPEC,
                                 f'Emociones {sorted(EXPRESSIONS)}, poses {sorted(POSES)}, planos {sorted(SHOT_KINDS)}.')
        dialogue.append(line)
    spec = dict(title=args.title or 'Video nuevo', language=args.language, fps=30, resolution=list(CANVAS),
                characters=cast, stage={'type': 'terminal'}, dialogue=dialogue)
    output = Path(args.output).resolve()
    if output.exists() and not args.force:
        raise VoxelError(f'{output} ya existe.', EXIT_SPEC, 'Elegí otra ruta o usá --force.')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return dict(output=str(output), lines=len(dialogue), characters=list(cast))


def schema(args):
    data = {
        'formato_simple': {
            'descripcion': 'Lo mínimo: characters + dialogue con speaker y text. Los tiempos salen de la voz '
                           'sintetizada, la duración se calcula sola y los planos se generan por línea.',
            'ejemplo': 'examples/persona-y-cubo/guion.json',
        },
        'raiz': {
            'title': 'texto', 'language': 'es | en | pt (voz y sintetizador)', 'fps': 'entero, 30 por defecto',
            'resolution': '[ancho, alto] pares; [720, 1280] por defecto',
            'duration | frames': 'opcional en formato simple',
            'gap': 'segundos entre líneas automáticas (0.3)', 'lead_in': 'silencio inicial (0.25)',
            'outro': 'segundos después de la última línea (1.2 con dos personajes)',
            'ending': '"wide" agrega un plano general de cierre (por defecto con 2+ personajes); null lo evita',
            'audio': 'pista maestra opcional; si está, no se sintetizan voces',
            'look': 'objeto de aspecto (ver look) o "classic" para el render ortográfico anterior',
            'captions': '{"y": 359, "size": 31, "wrap": 43} posición en lienzo 720×1280',
            'subtitles': 'lista opcional {start, end, text, y?, fade_ms?}; por defecto salen del diálogo',
        },
        'characters.ID': {
            'type': sorted(CHARACTER_TYPES) + ['(alias human, claude)'], 'position': '[x, y, z]; dos personajes: [-1.64,0,0] y [1.61,0.2,0]',
            'preset': f'personaje armado en characters/<nombre>.json: {preset_names()}',
            'scale': 'cube suele ir a 0.88', 'yaw': 'grados',
            'voice': '"say:Paulina" (macOS), id de espeak-ng o "eleven:<voice_id>" (ElevenLabs, requiere clave)',
            'tag': 'ElevenLabs v3: interpretación, p. ej. "whispers", "sighs", "excited"',
            'closeup': 'custom: {"position", "target", "span"} del primer plano, relativo al personaje',
            'rate': 'palabras por minuto, 174 por defecto',
            'opciones person': ['skin_color', 'hair_color', 'shirt_color', 'pants_color', 'beard_color', 'eye_color',
                                'cap (true/false)', 'cap_color', 'cap_text', 'chain (true/false)'],
            'opciones cube': ['body_color', 'foot_color', 'cheek_color'],
        },
        'dialogue[]': {
            'speaker': 'ID de characters', 'text': 'lo que dice y el subtítulo',
            'start, end': 'segundos absolutos; omitir en ambos para tiempos automáticos',
            'emotion': sorted(EXPRESSIONS), 'pose': sorted(POSES), 'shot': sorted(SHOT_KINDS),
            'pause': 'segundos extra antes de la línea (solo automáticos)',
            'listeners': '{"B": {"emotion": "sad"}} actuación de los demás en ese plano',
            'cut': 'true fuerza un corte aunque hable el mismo personaje',
            'audio': 'archivo de voz propio', 'voice, rate': 'reemplazan los del personaje',
            'fit': '"shrink" (acelera si no entra) o "stretch" (ajusta siempre al intervalo)',
        },
        'shots[] (formato completo)': {
            'frame': 'inicio desde 0, creciente', 'subject': 'ID o null para plano general', 'label': 'nombre',
            'camera': '{position, target, span, roll, end_position, end_span, lens, fstop, dof}',
            'acting.ID': '{emotion, pose, pose_keys [[f,pose]], head [[f,dx,inclinación,giro,balanceo]], '
                         'gaze [[f,x,z]], blinks [f], parts {pieza: [[f,x,y,z]]}}',
        },
        'look': {
            'projection': 'perspective | ortho', 'dof': 'true', 'fstop': '0.45 (menor = más desenfoque)',
            'grade': 'AgX | Filmic | Standard', 'contrast': 'Punchy, Medium High Contrast…', 'exposure': '0',
            'bloom': '0.55; 0 lo apaga',
        },
        'stage': {
            'type': sorted(STAGES), 'terminal': '{seed, accent, keys[], legend, text, lights: cinematic|classic}',
            'custom': '{background, floor, materials{}, props[{id, position, size, material, bevel}]}',
        },
        'poses_humano': sorted(POSES),
        'iconos_cube': sorted(ICONS),
        'emociones': {'cube': 'cambian ojos, boca e íconos pixelados', 'person': 'surprised, sad y crying mueven las cejas',
                      'valores': sorted(EXPRESSIONS)},
    }
    if args.voices:
        data['voces_say'] = [dict(name=n, locale=l) for n, l in say_voices()]
        data['voces_por_defecto'] = SAY_DEFAULTS
    return data


def open_take(args):
    """Abre la escena .blend de una toma en el Blender local, para mirarla sin renderizar."""
    target = Path(args.target).resolve()
    if target.is_file() and target.suffix == '.blend':
        blend = target
    else:
        project = target.parent if target.name == 'guion.json' else target
        takes = sorted((project / 'tomas').glob('toma-*')) if (project / 'tomas').is_dir() else []
        if args.take:
            takes = [t for t in takes if t.name == f'toma-{args.take:02}']
        if not takes:
            raise VoxelError(f'No hay tomas en {project}.', EXIT_SPEC, 'Renderizá con `voxel.py render <proyecto>/guion.json`.')
        blend = takes[-1] / 'escena.blend'
    if not blend.is_file():
        raise VoxelError(f'La toma no tiene escena: {blend}', EXIT_SPEC, 'Se guarda al renderizar una toma.')
    blender = find_blender()
    if sys.platform == 'darwin':
        app = Path(blender).parents[2] if blender and '.app/' in blender else None
        command = ['open', '-a', str(app), str(blend)] if app else ['open', str(blend)]
    elif blender:
        command = [blender, str(blend)]
    else:
        raise VoxelError('No encontré Blender.', EXIT_DEPENDENCY)
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    video = blend.with_name('video.mp4')
    say(f'abriendo {blend} en Blender (vista de cámara, sin render)', args.json)
    return dict(blend=str(blend), video=str(video) if video.is_file() else None)


def cloud_stop(args):
    """Detiene todos los apps de voxel-factory que sigan vivos en Modal (libera GPUs huérfanas)."""
    python = find_modal_python()
    cli = shutil.which('modal')
    if not cli:
        raise VoxelError('No encontré la CLI de modal.', EXIT_DEPENDENCY, 'uv tool install modal')
    env = dict(os.environ)
    if args.modal_profile:
        env['MODAL_PROFILE'] = args.modal_profile
    listing = run(cli, 'app', 'list', '--json', capture_output=True, text=True, env=env).stdout
    alive = [a for a in json.loads(listing or '[]')
             if a.get('description') == 'voxel-factory' and a.get('state') not in ('stopped', 'deployed')]
    stopped = []
    for app in alive:
        run(cli, 'app', 'stop', '-y', app['app_id'], capture_output=True, text=True, env=env)
        stopped.append(dict(app_id=app['app_id'], created_at=app.get('created_at'), tasks=app.get('tasks')))
        say(f'  detenido {app["app_id"]} ({app.get("tasks")} contenedores)', args.json)
    if not stopped:
        say('  no había apps de voxel-factory corriendo en Modal', args.json)
    return dict(stopped=stopped, python=python)


# ---------------------------------------------------------------- CLI

COMMANDS = {'doctor', 'schema', 'new', 'validate', 'compile', 'preview', 'render', 'verify', 'analyze', 'cloud-stop', 'open'}


def render_options(parser, preview=False):
    parser.add_argument('project', type=Path, help='Guion JSON.')
    parser.add_argument('-o', '--output', type=Path,
                        help='MP4 de salida. Sin -o: videos/<proyecto>/guion.json crea la siguiente toma '
                             '(tomas/toma-NN/ con video, srt, escena.blend y guion); otros guiones van a renders/.'
                        + (' En preview: <proyecto>/preview.mp4 o renders/<guion>-preview.mp4.' if preview else ''))
    parser.add_argument('--blend', type=Path, help='Guarda también la escena editable de Blender.')
    parser.add_argument('--range', nargs=2, type=int, metavar=('INICIO', 'FIN'), help='Fotogramas desde 0, fin exclusivo.')
    parser.add_argument('--scale', type=int, default=50 if preview else 100, help='Porcentaje de resolución.')
    parser.add_argument('--samples', type=int, default=8 if preview else 24, help='Muestras de EEVEE.')
    parser.add_argument('--scratch', type=Path, help='Carpeta existente para los fotogramas temporales.')
    parser.add_argument('--srt', action='store_true', help='Escribe además un .srt junto al MP4.')
    parser.add_argument('--no-verify', action='store_true', help='No verificar el MP4 al terminar.')
    parser.add_argument('--cloud', choices=['modal'], help='Renderiza los fotogramas en la nube (Modal, GPU). '
                        'También con VOXEL_CLOUD=modal.')
    parser.add_argument('--gpu', help='GPU de Modal o lista de preferencia: «L4,A10G,T4» (por defecto) toma la primera libre; «L4» fija una.')
    parser.add_argument('--chunks', type=int, default=0, help='Tramos paralelos en la nube (0 = automático).')
    parser.add_argument('--modal-profile', default=os.environ.get('VOXEL_MODAL_PROFILE'),
                        help='Cuenta de Modal (perfil de ~/.modal.toml). Por defecto la activa.')
    if preview:
        parser.add_argument('--at', type=float, help='Segundo de inicio de la muestra.')
        parser.add_argument('--length', type=float, default=3.0, help='Duración de la muestra en segundos.')


def build_parser():
    parser = argparse.ArgumentParser(prog='voxel.py', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--json', action='store_true', help='Imprime un único objeto JSON en stdout.')
    sub = parser.add_subparsers(dest='command')
    p = sub.add_parser('doctor', help='Comprueba dependencias.')
    p.add_argument('--scratch', type=Path)
    p = sub.add_parser('schema', help='Campos y valores válidos del guion.')
    p.add_argument('--voices', action='store_true', help='Incluye las voces macOS instaladas.')
    p = sub.add_parser('new', help='Crea un guion en formato simple.')
    p.add_argument('-o', '--output', type=Path, required=True)
    p.add_argument('--line', action='append', required=True, help='«ID[etiquetas]: texto», repetible.')
    p.add_argument('--cast', help='A=person,B=cube (por defecto); tipos person, cube o un preset de characters/.')
    p.add_argument('--title')
    p.add_argument('--language', default='es')
    p.add_argument('--force', action='store_true')
    p = sub.add_parser('validate', help='Valida el guion sin renderizar.')
    p.add_argument('project', type=Path)
    p.add_argument('--no-tts', action='store_true', help='Estima los tiempos automáticos sin sintetizar voces.')
    p = sub.add_parser('compile', help='Escribe el guion con tiempos y planos resueltos.')
    p.add_argument('project', type=Path)
    p.add_argument('-o', '--output', type=Path, required=True)
    render_options(sub.add_parser('preview', help='Muestra corta verificada.'), preview=True)
    render_options(sub.add_parser('render', help='Render completo verificado.'))
    p = sub.add_parser('verify', help='Verifica un MP4.')
    p.add_argument('video', type=Path)
    p.add_argument('--spec', type=Path, help='Guion usado, para comprobar duración, audio y subtítulos.')
    p.add_argument('--range', nargs=2, type=int, metavar=('INICIO', 'FIN'))
    p = sub.add_parser('open', help='Abre la escena de una toma en el Blender local (sin renderizar).')
    p.add_argument('target', type=Path, help='videos/<proyecto>, su guion.json o un .blend')
    p.add_argument('--take', type=int, help='Número de toma (por defecto la última).')
    p = sub.add_parser('cloud-stop', help='Detiene los renders de este proyecto que sigan vivos en Modal.')
    p.add_argument('--modal-profile', default=os.environ.get('VOXEL_MODAL_PROFILE'))
    p = sub.add_parser('analyze', help='Desarma un video de referencia.')
    p.add_argument('video', type=Path)
    p.add_argument('--out', type=Path, help='Carpeta de salida (analysis/<nombre> por defecto).')
    p.add_argument('--language', default='es')
    p.add_argument('--threshold', type=float, default=.3, help='Sensibilidad de detección de cortes.')
    p.add_argument('--no-transcribe', action='store_true')
    return parser


def dispatch(args):
    quiet = args.json
    if args.command == 'doctor':
        result, code = doctor(args)
        for c in result['checks']:
            say(f'{"✓" if c["ok"] else ("✗" if c["required"] else "–")} {c["name"]}: {c["detail"]}'
                + ('' if c['ok'] else f'  → {c["hint"]}'), quiet)
        return result, code
    if args.command == 'schema':
        return schema(args), EXIT_OK
    if args.command == 'new':
        result = new_script(args)
        say(f'guion creado: {result["output"]}', quiet)
        return result, EXIT_OK
    if args.command in ('validate', 'compile'):
        source, spec, notes = prepare(args.project, synthesize=not getattr(args, 'no_tts', False))
        warnings = spec.pop('_warnings', [])
        summary = dict(file=str(source), frames=spec['frames'], fps=spec['fps'],
                       seconds=round(spec['frames'] / spec['fps'], 3), lines=len(spec.get('dialogue', [])),
                       shots=len(spec['shots']), characters=list(spec['characters']), warnings=warnings, notes=notes)
        if args.command == 'compile':
            for shot in spec['shots']:
                shot.pop('end_frame', None)
            Path(args.output).write_text(json.dumps(spec, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            summary['output'] = str(Path(args.output).resolve())
        say(f'✓ guion válido: {summary["seconds"]} s, {summary["frames"]} fotogramas, {summary["lines"]} líneas, '
            f'{summary["shots"]} planos', quiet)
        for w in warnings:
            say(f'  aviso {w["path"]}: {w["message"]}' + (f' → {w["hint"]}' if w.get('hint') else ''), quiet)
        for n in notes:
            say(f'  nota: {n}', quiet)
        return summary, EXIT_OK
    if args.command in ('preview', 'render'):
        if args.command == 'preview':
            source, spec, _ = prepare(args.project)
            if not args.range:
                args.range = list(preview_range(spec, args.at, args.length))
            if not args.output:
                args.output = (source.parent / 'preview.mp4' if source.name == 'guion.json'
                               else ROOT / 'renders' / f'{source.stem}-preview.mp4')
        result = render(args, quiet)
        say(f'✓ {result["output"]}', quiet)
        return result, EXIT_OK
    if args.command == 'verify':
        spec = None
        if args.spec:
            _, spec, _ = prepare(args.spec)
        result = verify_file(args.video, spec, args.range, quiet)
        return result, (EXIT_OK if result['ok'] else EXIT_VERIFY)
    if args.command == 'analyze':
        return analyze(args, quiet), EXIT_OK
    if args.command == 'open':
        return open_take(args), EXIT_OK
    if args.command == 'cloud-stop':
        return cloud_stop(args), EXIT_OK
    raise VoxelError('Falta el subcomando.', EXIT_SPEC, 'Corré `voxel.py --help`.')


def _interrupt(signum, frame):
    raise KeyboardInterrupt(f'señal {signum}')


def main(argv=None):
    import signal
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _interrupt)
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = '--json' in argv  # se acepta en cualquier posición
    argv = [a for a in argv if a != '--json']
    positional = [a for a in argv if not a.startswith('-')]
    if positional and positional[0] not in COMMANDS and positional[0].endswith('.json'):
        argv.insert(argv.index(positional[0]), 'render')  # forma anterior: voxel.py guion.json -o salida.mp4
    parser = build_parser()
    args = parser.parse_args(argv)
    args.json = as_json
    if not args.command:
        parser.print_help(sys.stderr)
        return EXIT_SPEC
    try:
        result, code = dispatch(args)
    except VoxelError as exc:
        progress(f'Error: {exc}')
        progress(f'exit={exc.code}')
        payload = dict(ok=False, command=args.command, code=exc.code, error=str(exc), hint=exc.hint,
                       errors=exc.errors, log=exc.log)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f'Error: {exc}', file=sys.stderr)
            for e in exc.errors[:20]:
                print(f'  - {e["path"]}: {e["message"]}' + (f' → {e["hint"]}' if e.get('hint') else ''), file=sys.stderr)
            if exc.hint:
                print(f'Sugerencia: {exc.hint}', file=sys.stderr)
            if exc.log:
                print(f'Log: {exc.log}', file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        progress('Error: render cancelado')
        progress('exit=130')
        return 130
    except Exception as exc:  # noqa: BLE001 — último recurso con contexto para el agente
        import traceback
        trace = traceback.format_exc()
        if args.json:
            print(json.dumps(dict(ok=False, command=args.command, code=EXIT_INTERNAL, error=repr(exc),
                                  trace=trace[-2000:]), ensure_ascii=False, indent=2))
        else:
            print(trace, file=sys.stderr)
        return EXIT_INTERNAL
    progress(f'exit={code}')
    if args.json:
        print(json.dumps(dict(ok=code == EXIT_OK, command=args.command, code=code, result=result),
                         ensure_ascii=False, indent=2))
    elif args.command == 'schema':
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command in ('render', 'preview'):
        print(result['output'])
    return code


if __name__ == '__main__':
    sys.exit(main())
