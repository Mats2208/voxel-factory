---
name: voxel-factory
description: Hacer videos verticales (9:16) con personajes voxel en 3D que hablan, con voces, boca sincronizada, gestos, íconos, cámaras y subtítulos, usando la CLI de Voxel Factory (Blender headless, local o en GPUs de Modal). Usala cuando pidan "hacé un video con estos personajes diciendo esto", "un sketch/diálogo animado", "un short/reel con muñequitos 3D", "un video voxel", "dos personajes que discuten", "recreá este video con personajes voxel", "creá un personaje nuevo para los videos", o cuando haya que renderizar, verificar o iterar tomas de Voxel Factory. No la uses para editar video filmado ni para animaciones 2D/motion graphics.
---

# Voxel Factory: de un pedido a un video verificado

Voxel Factory convierte un guion JSON en un MP4 vertical. Vos escribís el guion; la fábrica sintetiza las voces, calcula los tiempos, anima, encuadra, renderiza, subtitula y **verifica** el resultado. Esta skill te dice cómo hacerlo bien a la primera.

## 0. Ubicar la fábrica

Usá la primera ruta que exista:

1. La variable `VOXEL_FACTORY`.
2. Dos carpetas arriba de este `SKILL.md`, siguiendo el symlink (`skills/voxel-factory/` vive dentro del repo).
3. `~/PROYECTOS/voxel-factory`.
4. Si no existe en ninguna: `git clone https://github.com/Mats2208/voxel-factory`.

```sh
F="${VOXEL_FACTORY:-$(cd "$(dirname "$(readlink -f ~/.claude/skills/voxel-factory/SKILL.md)")/../.." && pwd)}"
python3 "$F/voxel.py" doctor        # la primera vez: dice qué falta y cómo instalarlo
```

`AGENTS.md`, en la raíz de la fábrica, es la referencia completa. `python3 "$F/voxel.py" schema` imprime los campos válidos y siempre está al día con el código. **Leé `schema` antes de inventar un campo.**

## 1. El recorrido

```sh
python3 "$F/voxel.py" new -o videos/<proyecto>/guion.json --cast A=person,B=cube --line "A: …" --line "B[happy]: …"
python3 "$F/voxel.py" validate videos/<proyecto>/guion.json     # voces reales, tiempos y planos
python3 "$F/voxel.py" preview  videos/<proyecto>/guion.json     # 3 s a 50 %, verificada
python3 "$F/voxel.py" render   videos/<proyecto>/guion.json [--cloud modal]
python3 "$F/voxel.py" open     videos/<proyecto>                # escena en Blender, sin renderizar
```

- El guion puede vivir en **cualquier carpeta**, también dentro de otro proyecto. Si se llama `guion.json`, cada `render` crea `tomas/toma-NN/` con `video.mp4`, `video.srt`, `escena.blend`, el guion resuelto y `verificacion.json`.
- Agregá `--json` a cualquier comando para recibir un objeto JSON y leer `code`. Los códigos son: **0** bien, **2** guion inválido (leé `errors[].path` y `hint`), **3** falta una dependencia, **4** falló Blender, FFmpeg o Modal, **5** el MP4 no pasó la verificación.
- **No des un video por terminado sin `verify` en verde.** `render` lo corre solo; si da código 5, leé qué comprobación falló.

## 2. Del pedido al guion

Usá el formato simple: personajes y líneas. **No escribas `start`, `end` ni `duration`**: la fábrica mide cada voz real, recorta el silencio y encadena las líneas. Revisá el resultado con `compile -o X.json`.

### Personajes

| Elegí | Cuando | Se controla con |
| --- | --- | --- |
| `"type": "person"` | Una persona | `pose` en cada línea (`down open explain shrug insist crossed question`). Opciones: `cap`, `cap_text`, `cap_color`, `chain` y colores de piel, pelo, camiseta, pantalón y ojos. |
| `"type": "cube"` | Una criatura expresiva, la mejor para hacer reír | `emotion`, más gestos e íconos automáticos sincronizados con las palabras |
| `"preset": "robot"` / `"cat"` | Otros personajes armados con piezas | `acting.parts` para mover piezas, por ejemplo la cola o un brazo |
| `"type": "custom"` | Un personaje que no existe | Ver la sección 4 |

Posiciones de dos personajes: `[-1.64, 0, 0]` y `[1.61, 0.2, 0]`. El cubo va con `"scale": 0.88`.

### Líneas que funcionan en cámara

- **Texto literal del pedido.** No lo parafrasees.
- **Frases cortas: 3 a 10 palabras por línea.** Una línea larga se convierte en un subtítulo de tres renglones y en un plano que no termina nunca. Si es larga, partila en dos líneas del mismo personaje con `"cut": true`.
- **Comedia: preparación, giro y remate.** El remate funciona mejor en un plano general: `"shot": "wide"` en la última línea, con `listeners` para la reacción del otro personaje. Ejemplo: `"listeners": {"B": {"emotion": "dizzy", "icons": [[10, "skull", 45]]}}`.
- **Emociones del cubo:** `neutral speaking sad crying sarcastic surprised happy wink angry worried love dizzy sleepy excited whisper smug`. Cambiá la emoción en cada línea: un cubo siempre `neutral` aburre.
- **Íconos del cubo:** `heart question exclaim sweat zzz sparkle idea anger music skull`. Algunas palabras los disparan solas: «error/rompiste/jefe» dan sudor, «duermo» da zzz, «arreglamos» da la idea. Un «?» final muestra `question` y un «!» muestra `exclaim`. Para gestos manuales: `"beats": [{"at": 0.4, "emotion": "love", "icon": "heart"}]`, con `at` en segundos desde el inicio de la línea.
- **Etiquetas en `new`:** `"B[happy,wide]: texto"`, que pueden ser una emoción, una pose o un plano.
- **Cierre:** con dos personajes o más se agrega solo un plano general alejado con un leve alejamiento de cámara. `"ending": null` lo quita; `"ending": {"acting": {...}, "seconds": 2.4}` lo personaliza. Si la última línea ya es `wide`, el cierre la alarga sin agregar un corte.

## 3. Voces

| Opción | Cómo | Cuándo |
| --- | --- | --- |
| Sistema (por defecto) | No pongas `voice`: macOS asigna una voz distinta a cada personaje; Linux usa espeak-ng | Pruebas, sin claves |
| macOS fija | `"voice": "say:Paulina"` (`schema --voices` lista las voces instaladas) | |
| ElevenLabs | `"voice": "eleven:<voice_id>"`, más `"tag": "whispers"` (o `sighs`, `excited`…) y `"voice_settings": {"stability": 0.0}` para más actuación | Videos finales |

- **Clave de ElevenLabs.** Va en `$F/.env` (`ELEVENLABS_API_KEY=…` o `ELEVENLABS_ENV_FILE=/ruta`). **Nunca** en el guion ni en el repo, y nunca la imprimas.
- **Lo que ya está resuelto.** El modelo `eleven_v3` cortaba la última sílaba; la fábrica agrega una pausa final y recorta el silencio de los bordes. No lo arregles a mano.
- **Sincronía.** ElevenLabs devuelve el tiempo de cada palabra: la boca y los gestos del cubo caen exactos. Con voces del sistema los gestos se reparten en proporción a la línea.
- **Caché.** Las voces quedan en `.cache/voices`: repetir un render no gasta caracteres. Cambiar el texto, la voz o la etiqueta sí regenera esa línea.
- **Para elegir una voz,** generá muestras de la misma frase con 2 o 3 candidatas y que el usuario elija de oído. Vos no podés escuchar el audio: no afirmes cómo suena.

## 4. Personajes nuevos

Copiá `$F/characters/robot.json` a `characters/<nombre>.json` y adaptalo:

- `parts`: cajas con `id`, `size` `[x, y, z]`, `material` y, si hace falta, `parent`, `position` (el pivote, relativo al padre), `offset` (la geometría respecto del pivote) y `bevel`. **Los padres se declaran antes que sus hijos.** La cara mira hacia **−y** y **z** apunta hacia arriba.
- `head` y `mouth`: las piezas que siguen a la cabeza y a la boca.
- `closeup`: el encuadre del primer plano, `{"position", "target", "span"}` (`span` es el alto visible).
- Las piezas con pivote propio se animan desde el guion: `"acting": {"parts": {"cola": [[f, x°, y°, z°], …]}}`, con `f` en fotogramas desde el inicio del plano.

Validá y mirá **un solo fotograma** antes de renderizar todo: `preview --range N N+1` (agregá `--cloud modal` para no usar la máquina local). Después revisalo con tu herramienta de lectura de imágenes.

## 5. Revisar como un director

`verify` comprueba la técnica, no el gusto. Antes de entregar, mirá el video:

```sh
ffmpeg -v error -i video.mp4 -vf "select='not(mod(n\,60))',scale=180:-2,tile=8x2" -frames:v 1 -y hoja.png
```

Leé `hoja.png` y comprobá estas cosas:

- **Encuadre:** que nadie quede cortado.
- **Expresiones:** que cada una cuadre con lo que se dice.
- **Íconos:** que no tapen la cara.
- **Remate:** que el plano final cierre el chiste.

Si algo falla, ajustá el guion y hacé otra toma; `render` crea `toma-02` sin pisar la anterior.

## 6. Render en Modal (opcional)

- **Requisito:** `uv tool install modal && modal setup`. La cuenta se elige con `VOXEL_MODAL_PROFILE` en `.env` o con `--modal-profile`.
- **Qué hace:** Blender se reparte en tramos de unos 90 fotogramas; `--gpu L4,A10G,T4` toma la primera GPU libre. Lo demás pasa en local y el MP4 sale idéntico.
- **Límite de GPUs:** una cuenta básica permite **10 a la vez**. Dos renders grandes simultáneos se reparten esas 10.
- **Apps huérfanas:** **nunca mates el proceso de Modal a mano y lo olvides.** Cortar `voxel.py` apaga el app, pero después de cualquier corte corré `voxel.py cloud-stop` y comprobá con `MODAL_PROFILE=… modal container list` que quedó en cero contenedores. Un app huérfano sigue gastando créditos y ocupa las GPUs.
- **Progreso:** se escribe en `.voxel/progreso/<video>.txt` (carpeta de trabajo). El mod `extras/voxel-progreso` lo muestra en vivo en Claude Code.

## 7. Errores frecuentes

| Síntoma | Causa y arreglo |
| --- | --- |
| `validate` da código 2 | Leé todos los `errors[]`: cada uno trae la ruta y una sugerencia. Corregilos juntos. |
| Preset inexistente | `schema` lista los presets; los nuevos van en `characters/`. |
| Blender da código 4 en un `custom` | Un `parent` declarado después de su hijo, o una pista de `acting.parts` con valores de más o de menos. |
| Falla la verificación de subtítulos | `captions.y` fuera de lugar (lienzo 720×1280, 359 por defecto) o un texto vacío. |
| El video dura de más | Líneas largas: partilas. Bajá `gap` (0,3 s por defecto) o el `outro`. |
| El render local calienta la máquina | Usá `--cloud modal`, o `preview` para iterar. |
| ElevenLabs da 401/402/429 | Clave inválida, sin créditos o con límite. Volvé a voces del sistema o avisale al usuario. |

## 8. Entregar

Informá la ruta del MP4 y de la carpeta de la toma, la duración y el resultado de `verify`. Ofrecé `voxel.py open <proyecto>` para ver la escena en Blender. Si usaste servicios pagos, decí cuánto: caracteres de ElevenLabs y minutos de GPU de Modal.
