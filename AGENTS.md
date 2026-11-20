# AGENTS.md: guía para agentes (Claude Code, Codex, Cursor u otro con terminal)

Voxel Factory convierte un **guion JSON** en un **MP4 vertical** (720×1280) con personajes voxel en 3D, voces, boca sincronizada, gestos, cámaras y subtítulos quemados. Blender corre en segundo plano, en local o en GPUs de Modal, y nunca hace falta abrirlo. Todo pasa por un solo comando: `python3 voxel.py`.

Tu trabajo es **traducir el pedido del usuario a un guion**. El motor se encarga de las voces, la animación, los subtítulos, la exportación y la verificación. No edites `studio/` para hacer un video: solo cambialo si el usuario pide un tipo de personaje o un escenario nuevo.

## Qué leer

| Archivo | Para qué |
| --- | --- |
| `AGENTS.md` (este) | El recorrido, los comandos y cómo recuperarse de errores. |
| `python3 voxel.py schema [--voices]` | Todos los campos y valores válidos, siempre al día con el código. |
| `examples/persona-y-cubo/guion.json` | Formato simple con personajes integrados, emociones, poses y gestos. |
| `examples/robot-y-gato/guion.json` | Formato simple con presets armados con piezas y animación de piezas. |
| `characters/*.json` | Presets de personajes. Copiá uno para crear otro. |

## Recorrido: «hacé un video con estos personajes diciendo esto»

```sh
python3 voxel.py doctor                                   # 1. dependencias (la primera vez)
python3 voxel.py new -o videos/mi-video/guion.json \       # 2. guion en formato simple
  --cast A=robot,B=cat \
  --line "A: Detecto un gato." --line "B[happy]: Detectás bien."
python3 voxel.py validate videos/mi-video/guion.json       # 3. valida y calcula tiempos con las voces reales
python3 voxel.py preview  videos/mi-video/guion.json       # 4. muestra de 3 s verificada
python3 voxel.py render   videos/mi-video/guion.json       # 5. toma final → videos/mi-video/tomas/toma-01/
```

Agregá `--cloud modal` a `preview` o `render` para renderizar en GPUs de Modal en vez de usar la máquina local. Si un paso termina con un código distinto de 0, corregilo y repetí ese paso antes de seguir. `--json` (en cualquier posición) devuelve un único objeto JSON con `ok`, `code` y `result`, o con `error`, `hint` y `errors[]`.

El guion puede vivir en **cualquier carpeta**, también dentro de otro proyecto. La fábrica se llama por su ruta: `python3 /ruta/a/voxel-factory/voxel.py render ./guion.json`.

### Cómo pasar del pedido al guion

1. **Personajes** (`characters`, un ID por personaje):
   - `"type": "person"`: humano voxel con 7 poses de brazos. Opciones: `cap`, `cap_text`, `cap_color`, `chain`, `skin_color`, `hair_color`, `shirt_color`, `pants_color`, `beard_color`, `eye_color`.
   - `"type": "cube"`: criatura cúbica de cara pixelada, con 16 emociones, 10 íconos y gestos automáticos. Opciones: `body_color`, `foot_color`, `cheek_color`.
   - `"preset": "<nombre>"`: un personaje de `characters/<nombre>.json` armado con piezas (`robot`, `cat`). Los campos del guion se aplican encima, por ejemplo `position`, `scale`, `voice` o colores en `materials`.
   - `"type": "custom"`: piezas definidas en el propio guion (ver «Personajes nuevos»).
   - Para dos personajes, las posiciones habituales son `[-1.64, 0, 0]` y `[1.61, 0.2, 0]`. El cubo suele ir con `scale` 0.88.
2. **Líneas** (`dialogue`), con `speaker` y `text` literal del pedido. Campos opcionales:
   - `emotion`: `neutral speaking sad crying sarcastic surprised happy wink angry worried love dizzy sleepy excited whisper smug`.
   - `pose`, para `person`: `down open explain shrug insist crossed question`.
   - `shot`: `close` (por defecto) o `wide`.
   - `listeners`: la reacción de los demás en ese plano.
   - `acting.parts`: animación de piezas de un preset, `{"cola": [[f, x°, y°, z°], …]}`.
   - `beats`: gestos manuales del cubo, `[{"at": s, "emotion": …, "icon": …, "dur": s}]`.
   - `pause`: silencio antes de la línea. `cut: true`: fuerza un corte.
   - `camera`: `{span, position, target, end_span…}`.
   - `tag`: interpretación de ElevenLabs, por ejemplo `whispers`.
   - Con `new`, las etiquetas van en la línea: `"B[happy,wide]: texto"`.
3. **Tiempos.** En formato simple no escribas `start`, `end` ni `duration`. El motor sintetiza cada voz, mide cuánto dura, recorta el silencio y encadena las líneas. `compile -o X.json` muestra el resultado. Si el usuario da tiempos exactos o un audio propio, usá el formato completo (`start`, `end`, `duration`, `shots`).
4. **Cierre.** Con dos personajes o más se agrega un plano general alejado con un leve alejamiento de cámara. `"ending": null` lo quita. `"ending": {"acting": {...}, "seconds": 2.4}` lo personaliza.

### Personajes nuevos

Copiá `characters/robot.json` y adaptalo:

- `parts`: cajas con `id`, `size`, `material` y, si hace falta, `parent`, `position` (el pivote), `offset` (la geometría respecto del pivote) y `bevel`. Los padres se declaran antes que sus hijos.
- `head` y `mouth`: nombran las piezas que siguen la cabeza y los visemas.
- `materials`: colores hex.
- `closeup`: el encuadre del primer plano.

Las piezas con pivote se pueden animar desde el guion con `acting.parts`. Verificá con `preview` (o `preview --cloud modal --range N N+1` para un solo fotograma).

## Voces

| Opción | Cómo se configura | Requisito |
| --- | --- | --- |
| macOS `say` | sin `voice` (se asigna una distinta por personaje) o `"voice": "say:Paulina"` | macOS |
| espeak-ng | `"voice": "es"` | Linux u otro sistema con espeak-ng |
| ElevenLabs | `"voice": "eleven:<voice_id>"`, más `tag` (`whispers`, `sighs`…) y `voice_settings` (`{"stability": 0.0}` para más interpretación) con el modelo `eleven_v3` | clave en `.env` |
| Audio propio | `"audio": "ruta.wav"` por línea, o `"audio"` maestro en la raíz | — |

Detalles de ElevenLabs:

- **Clave.** Va en `.env` (`ELEVENLABS_API_KEY=…` o `ELEVENLABS_ENV_FILE=/ruta`) y **nunca** en el repo.
- **Tiempos por palabra.** El motor usa los tiempos que devuelve ElevenLabs para la boca y para los gestos del cubo.
- **Correcciones automáticas.** Agrega una pausa final para que la última sílaba no se corte y recorta el silencio de los bordes.
- **Caché.** Las voces quedan en `.cache/voices`: repetir un render no gasta caracteres.

## Comandos

| Comando | Qué hace |
| --- | --- |
| `doctor` | Comprueba Python, Blender, FFmpeg con libass, ffprobe, voces, Modal, Whisper y espacio en disco. Si falta algo obligatorio, termina con el código 3. |
| `schema [--voices]` | Lista los campos, tipos, presets, emociones, íconos y voces. |
| `new -o X.json --line … [--cast A=person,B=cube] [--title] [--language]` | Crea un guion en formato simple. |
| `validate X.json [--no-tts]` | Muestra **todos** los errores con su ruta y una sugerencia. |
| `compile X.json -o Y.json` | Escribe el guion con los tiempos y los planos resueltos. |
| `preview X.json [--at s] [--length s] [--cloud modal]` | Muestra corta a 50 %, verificada. |
| `render X.json [-o Y.mp4] [--srt] [--blend f] [--range a b] [--cloud modal] [--gpu L4,A10G,T4]` | Video final verificado. Si el guion se llama `guion.json`, crea `tomas/toma-NN/`. |
| `verify Y.mp4 --spec X.json` | Decodifica el MP4 y comprueba H.264 y AAC, formato vertical, fotogramas, fps, audio en cada línea y subtítulos en sus intervalos. |
| `open <carpeta> [--take N]` | Abre la escena `.blend` de una toma en el Blender local, por la vista de cámara y sin renderizar. |
| `analyze video.mp4` | Desarma un video de referencia: cortes, un fotograma por plano, audio, transcripción con whisper-cli y un borrador de guion. |
| `cloud-stop` | Detiene cualquier render de este proyecto que siga vivo en Modal. |

### Tomas

```
videos/mi-video/
  guion.json
  preview.mp4
  tomas/toma-01/  video.mp4 · video.srt · escena.blend · guion.json (resuelto) · verificacion.json
```

### Códigos de salida

| Código | Significado | Qué hacer |
| --- | --- | --- |
| 0 | Bien | Seguir. |
| 1 | Error interno | Leer la traza. |
| 2 | Guion o argumentos inválidos | Leer `errors[]`: cada uno trae `path`, `message` y `hint`. |
| 3 | Falta una dependencia | `doctor` dice cuál y cómo instalarla. |
| 4 | Falló Blender, FFmpeg o Modal | `errors[0].message` trae la traza y `log` dice dónde está el registro completo. |
| 5 | El MP4 no pasó la verificación | `errors[]` dice qué comprobación falló. |

## Render en Modal

- **Requisito.** Tener `modal` instalado y autenticado (`uv tool install modal`, `modal setup`).
- **Cuenta.** `VOXEL_MODAL_PROFILE` en `.env` o `--modal-profile` eligen la cuenta.
- **GPU.** `--gpu L4,A10G,T4` (el valor por defecto) toma la primera libre.
- **Qué corre dónde.** Blender corre en la nube, partido en tramos de unos 90 fotogramas en paralelo. Voces, subtítulos, la unión final y la verificación siguen en local, así que el MP4 sale idéntico a uno local.
- **Comprobación sin renderizar.** `python studio/cloud_modal.py --check` (con el intérprete que tenga `modal`) informa la GPU, la versión de Blender y si OpenGL arranca por EGL.
- **Cancelar.** Cortar `voxel.py` detiene también el app en Modal. Si quedó algo vivo, `voxel.py cloud-stop` lo apaga.
- **Progreso.** Va a `.voxel/progreso/<video>.txt` en la carpeta de trabajo. `extras/voxel-progreso` es un mod de Claude Code que lo muestra en vivo sobre el prompt.

## Errores frecuentes

| Error | Qué hacer |
| --- | --- |
| `speaker` no está en `characters` | Usá los mismos IDs en ambos lados. |
| Emoción, pose o ícono inválido | La validación lista los valores válidos. |
| Preset inexistente | `schema` lista los presets. Los nuevos se crean en `characters/`. |
| La voz macOS no está instalada | `schema --voices`, o quitá `voice` para la asignación automática. |
| No hay clave de ElevenLabs | Usá voces locales o completá `.env` (ver `.env.example`). |
| La verificación de subtítulos falla | Revisá `captions.y` (lienzo 720×1280, 359 por defecto) y que el texto no esté vacío. |
| Blender falla (código 4) | Si el error viene de un `custom`, suele ser un `parent` declarado después de su hijo o una pista de `acting.parts` con valores de más o de menos. |
| El render local va lento | Usá `--cloud modal` o `preview` para iterar. |

## Entrega

Informá la ruta del MP4, su duración y el resultado de `verify`. Ofrecé `open` para mirar la escena en Blender.
