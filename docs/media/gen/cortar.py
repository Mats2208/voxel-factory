"""Quita el fondo verde de una grilla 3x3 generada y la corta en 9 recortes transparentes.

Uso: python3 cortar.py grilla.png salida_prefijo nombre1,nombre2,...,nombre9
Escribe salida_prefijo-<nombre>.webp (recorte ajustado al contenido, lado máximo 480 px).
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image

CHROMA = Path.home() / '.codex/skills/.system/imagegen/scripts/remove_chroma_key.py'


def main():
    source, prefix, names = Path(sys.argv[1]), sys.argv[2], sys.argv[3].split(',')
    alpha = source.with_name(source.stem + '-alpha.png')
    subprocess.run([sys.executable, str(CHROMA), '--input', str(source), '--out', str(alpha),
                    '--auto-key', 'border', '--soft-matte', '--despill', '--force'], check=True)
    image = Image.open(alpha).convert('RGBA')
    width, height = image.size
    for index, name in enumerate(names):
        row, col = divmod(index, 3)
        cell = image.crop((col * width // 3, row * height // 3, (col + 1) * width // 3, (row + 1) * height // 3))
        box = cell.getchannel('A').point(lambda a: 255 if a > 24 else 0).getbbox()
        if box:
            cell = cell.crop(box)
        cell.thumbnail((480, 480), Image.LANCZOS)
        out = Path(f'{prefix}-{name}.webp')
        cell.save(out, 'WEBP', quality=88, method=6)
        print(out, cell.size)


if __name__ == '__main__':
    main()
