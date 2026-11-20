# voxel-progreso (mod de Claude Code)

Muestra sobre el prompt de Claude Code el avance en vivo de cada render de Voxel Factory: fotogramas, tramos, GPU, tiempo, cuánto falta y el resultado de la verificación. Avisa cuando un video termina o falla.

Lee los archivos que escribe `voxel.py` en `<carpeta de trabajo>/.voxel/progreso/` (o en `VOXEL_PROGRESS_DIR`), así que conviene abrir Claude Code en la carpeta desde la que renderizás.

```sh
claude --plugin-dir /ruta/a/voxel-factory/extras/voxel-progreso
```
