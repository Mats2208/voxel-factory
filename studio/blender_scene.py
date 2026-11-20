import argparse
import bisect
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from models import claude, color, custom, human, pivot, point_at, stage


POSES = {
    'down': (0, 0, 0, 0, 0, 0, 8),
    'open': (-34, 34, -27, -27, -11, 11, 8),
    'explain': (-32, 27, -51, -38, -24, 18, 8),
    'shrug': (-44, 43, -53, -53, -20, 20, 8),
    'insist': (-22, 34, -129, -149, -6, 0, 8),
    'crossed': (22, -22, -10, -12, 70, -70, -28),
    'question': (-8, 23, -15, -52, 0, -24, 8),
}
MASKS = dict(neutral=('normal', 'normal'), speaking=('square', 'square'), sad=('sad', 'sad'),
             crying=('normal', 'sad'), sarcastic=('skeptic', 'skeptic'),
             surprised=('wide', 'wide'), happy=('happy', 'happy'), wink=('happy', 'square'),
             angry=('angry', 'angry'), worried=('worried', 'worried'), love=('heart', 'heart'),
             dizzy=('x', 'x'), sleepy=('closed', 'closed'), excited=('star', 'star'),
             whisper=('narrow', 'narrow'), smug=('skeptic', 'happy'))
# Boca en reposo por emoción (cuando no está hablando).
REST_MOUTH = dict(happy='smile', wink='smile', love='smile', excited='grin', sad='frown', crying='frown',
                  worried='wavy', dizzy='wavy', angry='frown', surprised='o', sleepy='wavy', smug='smile')


def key(obj, path, frame):
    obj.keyframe_insert(data_path=path, frame=frame, group=obj.name)


def euler(values):
    return tuple(math.radians(x) for x in values)


def visible(obj, value):
    obj.scale = (1, 1, 1) if value else (.0001,) * 3


def easing(a, b, weight):
    weight = max(0, min(1, weight))
    return a + (b - a) * weight * weight * (3 - 2 * weight)


def track(sequence, frame, fallback):
    if not sequence:
        return fallback
    points = [item[0] for item in sequence]
    index = bisect.bisect_right(points, frame)
    if index == 0:
        return sequence[0][1:]
    if index == len(sequence):
        return sequence[-1][1:]
    left, right = sequence[index - 1], sequence[index]
    t = (frame - left[0]) / (right[0] - left[0])
    return tuple(easing(a, b, t) for a, b in zip(left[1:], right[1:]))


def interpolate_pose(params, offset):
    sequence = params.get('pose_keys', [[0, params.get('pose', 'down')]])
    numeric = [[frame, *POSES[value]] for frame, value in sequence]
    return track(numeric, offset, POSES['down'])


def animate(actors, spec, sound):
    fps = spec['fps']
    shots = spec['shots']
    starts = [item['frame'] for item in shots]
    for index in range(spec['frames']):
        frame = index + 1
        shot = shots[bisect.bisect_right(starts, index) - 1]
        offset = index - shot['frame']
        for name, model in actors.items():
            params = shot.get('acting', {}).get(name, {})
            energy, width = sound[name][index]
            emotion = params.get('emotion', 'neutral')
            shift, nod, turn, roll = track(params.get('head'), offset, (0, 0, 0, 0))
            idle = math.sin(index / fps * 2.8)
            head = model['head']
            base = model['head_base']
            head.location = (base[0] + shift, base[1], base[2] + idle * .012)
            head.rotation_euler = euler((nod + idle * .9 + energy, roll + idle * .7, turn))
            key(head, 'location', frame)
            key(head, 'rotation_euler', frame)
            if model['kind'] == 'human':
                l, r, le, re, lt, rt, pitch = interpolate_pose(params, offset)
                for i, (arm, elbow) in enumerate(zip(model['arms'], model['elbows'])):
                    arm.rotation_euler = euler((pitch + energy * 4 + idle * 1.5, -(l, r)[i], 0))
                    elbow.rotation_euler = euler(((le, re)[i] - energy * 3, -(lt, rt)[i], 0))
                    key(arm, 'rotation_euler', frame)
                    key(elbow, 'rotation_euler', frame)
                shirt = model['shirt']
                stretch = 1.60 if spec.get('closeup_shirt') and shot.get('subject') == name else 1
                shirt.scale.z = stretch
                shirt.location.z = .72 - .765 * (stretch - 1)
                key(shirt, 'scale', frame)
                key(shirt, 'location', frame)
                torso = model['torso']
                torso.rotation_euler.y = math.radians(roll * .15 + idle * .4)
                key(torso, 'rotation_euler', frame)
                phase = (index + 34) % 91
                blink = (.47, .085, .47)[phase - 1] if phase in (1, 2, 3) else 1
                if any(abs(offset - x) <= 1 for x in params.get('blinks', [])):
                    blink = .12
                gaze, gaze_z = track(params.get('gaze'), offset, (0, 0))
                for i, (eye, iris, brow) in enumerate(zip(model['eyes'], model['irises'], model['brows'])):
                    eye.scale.z = blink
                    iris.location.x = max(-.060, min(.060, gaze + turn * .0015 + .012))
                    iris.location.z = gaze_z
                    brow.location.z = .52 + (.058 if emotion == 'surprised' else 0) + energy * .026
                    brow.rotation_euler.y = (.13 if i == 0 else -.13) if emotion in ('sad', 'crying') else 0
                    key(eye, 'scale', frame)
                    key(iris, 'location', frame)
                    key(brow, 'location', frame)
                    key(brow, 'rotation_euler', frame)
            elif model['kind'] == 'claude':
                # Gestos sincronizados: la última clave de expresión vigente manda sobre la del plano.
                for key_frame, value in params.get('expr_keys', []):
                    if key_frame <= offset:
                        emotion = value
                pattern = MASKS[emotion]
                for alias, pair in model['masks'].items():
                    for side, eye in enumerate(pair):
                        visible(eye, alias == pattern[side])
                        key(eye, 'scale', frame)
                tear = model['tear']
                visible(tear, emotion == 'crying')
                tear.location.z = .995 - (offset % 28) * .0024
                key(tear, 'location', frame)
                key(tear, 'scale', frame)
                for j, leg in enumerate(model['legs']):
                    leg.rotation_euler = euler((math.sin(index * .105 + j) * 2.5, roll * -.42, 0))
                    key(leg, 'rotation_euler', frame)
                rest = REST_MOUTH.get(emotion) if energy < .17 else None
                for name, holder in model['shapes'].items():
                    visible(holder, name == rest)
                    key(holder, 'scale', frame)
                for name, holder in model['icons'].items():
                    shown, age = False, 0
                    for start_frame, icon, *length in params.get('icons', []):
                        span = length[0] if length else 30
                        if icon == name and start_frame <= offset < start_frame + span:
                            shown, age = True, offset - start_frame
                    # Aparece con un rebote (0 → 1.25 → 1) y flota.
                    pop = min(1.0, age / 4) * (1 + .25 * max(0, 1 - abs(age - 4) / 4)) if shown else .0001
                    holder.scale = (pop,) * 3
                    holder.location.z = model['icon_base'][name] + (math.sin(index * .25) * .04 if shown else 0)
                    key(holder, 'scale', frame)
                    key(holder, 'location', frame)
                for j, corner in enumerate(model['corners']):
                    smile = emotion in ('speaking', 'sarcastic') and rest is None
                    frown = False
                    visible(corner, energy < .17 and (frown or (smile and (emotion != 'sarcastic' or j == 1))))
                    corner.location.z = .814 if frown else .872
                    key(corner, 'location', frame)
                    key(corner, 'scale', frame)
            if model.get('mouth'):
                mouth = model['mouth']
                mouth.scale = (width, 1, .105 + energy * .94)
                if model['kind'] == 'claude' and energy < .17 and REST_MOUTH.get(emotion):
                    mouth.scale = (.0001,) * 3
                key(mouth, 'scale', frame)
            for part, motion in params.get('parts', {}).items():
                control = model['parts'][part]
                control.rotation_euler = euler(track(motion, offset, (0, 0, 0)))
                key(control, 'rotation_euler', frame)


LOOK_DEFAULTS = {
    'projection': 'perspective',  # 'perspective' o 'ortho' (aspecto anterior)
    'dof': True,                  # desenfoque de fondo enfocado en el objetivo del plano
    'fstop': .45,                 # menor = más desenfoque (escala de la escena: ~4 u por personaje)
    'grade': 'AgX',               # transformada de vista: AgX, Filmic, Standard...
    'contrast': 'Punchy',
    'exposure': 0.0,
    'bloom': .55,                 # 0 desactiva el brillo de LEDs y leyendas
}


def look_settings(spec):
    look = dict(LOOK_DEFAULTS)
    if spec.get('look') == 'classic' or spec.get('stage', {}).get('type') == 'terminal_classic':
        look.update(projection='ortho', dof=False, grade='Standard', contrast='None', bloom=0)
    elif isinstance(spec.get('look'), dict):
        look.update(spec['look'])
    return look


def lens_for(span, distance):
    # Sensor vertical de 36 mm: el alto visible a la distancia de enfoque coincide con `span`.
    return max(8.0, 36.0 * distance / max(.05, span))


def cameras(scene, spec, actors, look):
    perspective = look['projection'] == 'perspective'
    for shot in spec['shots']:
        frame = shot['frame'] + 1
        settings = shot.get('camera', {})
        actor = actors.get(shot.get('subject'))
        base = actor['root'].location if actor else (0, 0, 0)
        scale = actor['root'].scale.x if actor else 1
        defaults = {
            'human': ((-1.05, -7.5, 3.95), (.035, 0, 3.69), 4.20, 0),
            'claude': ((1.80, -8.6, 2.55), (-.16, 0, 1.45), 6.4, -.045),  # cuerpo entero y lugar para íconos
            'custom': ((1, -9, 3), (0, 0, 1.6), 5.6, 0),
            # Plano general alejado como el original: personajes chicos sobre el teclado.
            'wide': ((0, -31, 10.8), (0, 0, 3.3), 23.0, 0),
        }
        pos, target, span, roll = defaults[actor['kind'] if actor else 'wide']
        if actor and actor.get('closeup'):
            # Personaje armado con piezas: su preset define su propio primer plano.
            close = actor['closeup']
            pos, target, span = close.get('position', pos), close.get('target', target), close.get('span', span)
            roll = close.get('roll', roll)
        data = bpy.data.cameras.new(f'Cámara {frame:04}')
        span = settings.get('span', span * scale)
        obj = bpy.data.objects.new(data.name, data)
        bpy.context.collection.objects.link(obj)
        obj.location = settings.get('position', [p * scale + b for p, b in zip(pos, base)])
        aim = settings.get('target', [p * scale + b for p, b in zip(target, base)])
        distance = (Vector(aim) - Vector(obj.location)).length

        def optics(span_value, distance_value):
            if perspective:
                data.type = 'PERSP'
                data.sensor_fit = 'VERTICAL'
                data.sensor_height = 36
                data.lens = settings.get('lens', lens_for(span_value, distance_value))
                data.clip_end = 400
                if look['dof'] and settings.get('dof', True):
                    data.dof.use_dof = True
                    data.dof.focus_distance = distance_value
                    data.dof.aperture_fstop = settings.get('fstop', look['fstop'] * (.22 if not actor else 1))
            else:
                data.type = 'ORTHO'
                data.ortho_scale = span_value
                data.lens = 54

        optics(span, distance)
        bank = Quaternion((0, 0, 1), settings.get('roll', roll))
        point_at(obj, aim)
        obj.rotation_mode = 'QUATERNION'
        obj.rotation_quaternion = obj.rotation_euler.to_quaternion() @ bank
        if 'end_position' in settings or 'end_span' in settings:
            last = shot.get('end_frame', spec['frames'])
            lens_path = 'lens' if perspective else 'ortho_scale'
            key(obj, 'location', frame)
            key(obj, 'rotation_quaternion', frame)
            key(data, lens_path, frame)
            if perspective and data.dof.use_dof:
                key(data, 'dof.focus_distance', frame)
            obj.location = settings.get('end_position', obj.location)
            if 'end_position' in settings:
                point_at(obj, aim)
                obj.rotation_quaternion = obj.rotation_euler.to_quaternion() @ bank
            optics(settings.get('end_span', span), (Vector(aim) - Vector(obj.location)).length)
            key(obj, 'location', last)
            key(obj, 'rotation_quaternion', last)
            key(data, lens_path, last)
            if perspective and data.dof.use_dof:
                key(data, 'dof.focus_distance', last)
        scene.timeline_markers.new(shot.get('label', data.name), frame=frame).camera = obj
        if frame == 1:
            scene.camera = obj


def grade(scene, look):
    try:
        scene.view_settings.view_transform = look['grade']
        options = [i.identifier for i in scene.view_settings.bl_rna.properties['look'].enum_items]
        wanted = look['contrast']
        scene.view_settings.look = next((o for o in (wanted, f"{look['grade']} - {wanted}") if o in options), 'None')
    except TypeError as exc:
        print(f'Aviso: no se pudo aplicar el etalonaje {look["grade"]}/{look["contrast"]}: {exc}')
        scene.view_settings.view_transform = 'Standard'
    scene.view_settings.exposure = look['exposure']
    if not look['bloom']:
        return
    try:
        tree = bpy.data.node_groups.new('Posproceso', 'CompositorNodeTree')
        tree.interface.new_socket('Image', in_out='OUTPUT', socket_type='NodeSocketColor')
        layers = tree.nodes.new('CompositorNodeRLayers')
        glare = tree.nodes.new('CompositorNodeGlare')
        output = tree.nodes.new('NodeGroupOutput')
        glare.inputs['Type'].default_value = 'Bloom'
        glare.inputs['Quality'].default_value = 'High'
        glare.inputs['Threshold'].default_value = 1.4
        glare.inputs['Strength'].default_value = look['bloom']
        glare.inputs['Size'].default_value = .62
        tree.links.new(layers.outputs['Image'], glare.inputs['Image'])
        tree.links.new(glare.outputs['Image'], output.inputs[0])
        scene.compositing_node_group = tree
        scene.render.use_compositing = True
    except (AttributeError, KeyError, TypeError, RuntimeError) as exc:
        print(f'Aviso: brillo desactivado, el compositor no aceptó la configuración: {exc}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('job')
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    job = json.loads(Path(args.job).read_text())
    spec = job['spec']
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_EEVEE'
    scene.eevee.taa_render_samples = job['samples']
    scene.world = bpy.data.worlds.new('Fondo negro azulado')
    look = look_settings(spec)
    scene.world.color = color(spec.get('stage', {}).get('world', '#05040d' if look['grade'] != 'Standard' else '#151323'))[:3]
    grade(scene, look)
    scene.eevee.use_shadows = True
    scene.eevee.shadow_resolution_scale = 1.0
    scene.render.resolution_x, scene.render.resolution_y = spec['resolution']
    scene.render.resolution_percentage = job['scale']
    scene.render.fps = spec['fps']
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGB'
    scene.render.image_settings.compression = 15
    scene.render.filepath = job['frame_path']
    scene.frame_start = 1
    scene.frame_end = spec['frames']
    stage(spec.get('stage', {}))
    actors = {}
    for name, config in spec['characters'].items():
        root = pivot(name, config.get('position', (0, 0, 0)))
        root.scale = (config.get('scale', 1),) * 3
        root.rotation_euler.z = math.radians(config.get('yaw', 0))
        build = {'person': human, 'human': human, 'cube': claude, 'claude': claude, 'custom': custom}
        model = build[config['type']](root, config)
        model['head_base'] = model['head'].location.copy()
        if model['kind'] == 'claude':
            model['icon_base'] = {k: v.location.z for k, v in model['icons'].items()}
        actors[name] = model
    animate(actors, spec, job['visemes'])
    cameras(scene, spec, actors, look)
    if job.get('blend'):
        # Al abrirla en Blender se ve por la cámara del plano, con materiales (sin render final).
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            space.shading.type = 'MATERIAL'
                            space.region_3d.view_perspective = 'CAMERA'
                            space.overlay.show_overlays = False
        scene.frame_set(1)
        scene.render.filepath = '//frames/shot_'
        bpy.context.preferences.filepaths.save_version = 0
        bpy.ops.file.pack_all()
        bpy.ops.wm.save_as_mainfile(filepath=job['blend'])
    scene.render.filepath = job['frame_path']
    scene.frame_set(job['start'] + 1)
    scene.frame_start = job['start'] + 1
    scene.frame_end = job['end']
    bpy.ops.render.render(animation=True)


if __name__ == '__main__':
    main()
