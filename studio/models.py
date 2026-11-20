import math
import os
import random
from pathlib import Path

import bpy
from mathutils import Vector


def color(hex_code):
    channels = [int(hex_code.lstrip('#')[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple(v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels) + (1,)


def material(name, hex_code, roughness=.65, metal=0, emission=0, textured=False, spread=(.81, 1.08)):
    """Principled material. `textured` admite True o 'voxel' (ruido por celdas), 'streaks'
    (vetas horizontales tipo pincelada, como el cuerpo de Claude) y 'fine' (grano sutil)."""
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    result.diffuse_color = rgb = color(hex_code)
    nodes = result.node_tree.nodes
    links = result.node_tree.links
    surface = nodes.get('Principled BSDF')
    surface.inputs['Base Color'].default_value = rgb
    surface.inputs['Roughness'].default_value = roughness
    surface.inputs['Metallic'].default_value = metal
    surface.inputs['Emission Color'].default_value = rgb
    surface.inputs['Emission Strength'].default_value = emission
    if textured:
        style = 'voxel' if textured is True else textured
        cells = {'voxel': (24, 30, 24), 'streaks': (3.2, 3.2, 21), 'fine': (34, 34, 34)}[style]
        coordinates = nodes.new('ShaderNodeTexCoord')
        scale = nodes.new('ShaderNodeVectorMath')
        scale.operation = 'MULTIPLY'
        scale.inputs[1].default_value = cells
        grid = nodes.new('ShaderNodeVectorMath')
        grid.operation = 'FLOOR'
        noise = nodes.new('ShaderNodeTexWhiteNoise')
        ramp = nodes.new('ShaderNodeValToRGB')
        low, high = spread
        ramp.color_ramp.elements[0].color = tuple(v * low for v in rgb[:3]) + (1,)
        ramp.color_ramp.elements[1].color = tuple(min(1, v * high) for v in rgb[:3]) + (1,)
        links.new(coordinates.outputs['Object'], scale.inputs[0])
        links.new(scale.outputs[0], grid.inputs[0])
        links.new(grid.outputs[0], noise.inputs['Vector'])
        if style == 'streaks':
            # Pinceladas: celdas alargadas en X con pocas vetas claras y oscuras sobre un tono
            # medio, más una variación suave de baja frecuencia.
            ramp.color_ramp.interpolation = 'CONSTANT'
            elements = ramp.color_ramp.elements
            elements[0].position, elements[1].position = 0, .84
            middle = elements.new(.07)
            middle.color = rgb
            elements[0].color = tuple(v * .88 for v in rgb[:3]) + (1,)
            elements[1].color = tuple(min(1, v * 1.13) for v in rgb[:3]) + (1,)
            cloud = nodes.new('ShaderNodeTexNoise')
            cloud.inputs['Scale'].default_value = 1.6
            cloud.inputs['Detail'].default_value = 2
            links.new(coordinates.outputs['Object'], cloud.inputs['Vector'])
            shade = nodes.new('ShaderNodeMix')
            shade.data_type = 'RGBA'
            shade.blend_type = 'MULTIPLY'
            shade.inputs['Factor'].default_value = .22
            links.new(ramp.outputs[0], shade.inputs['A'])
            links.new(cloud.outputs['Color'], shade.inputs['B'])
            links.new(noise.outputs['Value'], ramp.inputs[0])
            boost = nodes.new('ShaderNodeHueSaturation')
            boost.inputs['Value'].default_value = 1.3
            boost.inputs['Saturation'].default_value = 1.15
            links.new(shade.outputs['Result'], boost.inputs['Color'])
            links.new(boost.outputs[0], surface.inputs['Base Color'])
            return result
        links.new(noise.outputs['Value'], ramp.inputs[0])
        links.new(ramp.outputs[0], surface.inputs['Base Color'])
    return result


def unlit(name, hue, strength):
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    nodes = result.node_tree.nodes
    glow = nodes.new('ShaderNodeEmission')
    glow.inputs['Color'].default_value = color(hue)
    glow.inputs['Strength'].default_value = strength
    result.node_tree.links.new(glow.outputs[0], nodes.get('Material Output').inputs[0])
    return result


def haze(hue, density):
    """Velo translúcido emisivo: aplasta el contraste de lo que queda detrás, como bruma."""
    result = bpy.data.materials.new('Bruma')
    result.use_nodes = True
    nodes = result.node_tree.nodes
    for node in list(nodes):
        if node.type != 'OUTPUT_MATERIAL':
            nodes.remove(node)
    glow = nodes.new('ShaderNodeEmission')
    glow.inputs['Color'].default_value = color(hue)
    glow.inputs['Strength'].default_value = .3
    clear = nodes.new('ShaderNodeBsdfTransparent')
    mix = nodes.new('ShaderNodeMixShader')
    mix.inputs['Fac'].default_value = density
    links = result.node_tree.links
    links.new(clear.outputs[0], mix.inputs[1])
    links.new(glow.outputs[0], mix.inputs[2])
    links.new(mix.outputs[0], next(n for n in nodes if n.type == 'OUTPUT_MATERIAL').inputs[0])
    try:
        result.surface_render_method = 'BLENDED'
    except (AttributeError, TypeError):
        result.blend_method = 'BLEND'
    return result


def pivot(name, location=(0, 0, 0), parent=None):
    obj = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(obj)
    obj.parent = parent
    obj.location = location
    return obj


def mesh(name, vertices, faces, surface, parent, location=(0, 0, 0), bevel=0):
    data = bpy.data.meshes.new(name)
    data.from_pydata(vertices, [], faces)
    data.update()
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.parent = parent
    obj.location = location
    if surface:
        data.materials.append(surface)
    if bevel:
        modifier = obj.modifiers.new('Facetas de borde', 'BEVEL')
        modifier.width = bevel
        modifier.segments = 2
        normals = obj.modifiers.new('Normales ponderadas', 'WEIGHTED_NORMAL')
        normals.keep_sharp = True
    return obj


def box(name, location, dimensions, surface, parent=None, bevel=0):
    x, y, z = (d / 2 for d in dimensions)
    vertices = [(-x, -y, -z), (x, -y, -z), (x, y, -z), (-x, y, -z),
                (-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z)]
    faces = [(3, 2, 1, 0), (0, 1, 5, 4), (1, 2, 6, 5),
             (2, 3, 7, 6), (3, 0, 4, 7), (4, 5, 6, 7)]
    return mesh(name, vertices, faces, surface, parent, location, bevel)


def tube(name, points, radius, surface, parent):
    data = bpy.data.curves.new(name, 'CURVE')
    data.dimensions = '3D'
    data.bevel_depth = radius
    data.bevel_resolution = 1
    spline = data.splines.new('POLY')
    spline.points.add(len(points) - 1)
    for point, xyz in zip(spline.points, points):
        point.co = (*xyz, 1)
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.parent = parent
    data.materials.append(surface)
    return obj


def font(path):
    # En la nube (Linux) las fuentes de macOS viajan a VOXEL_FONTS con el mismo nombre de archivo.
    candidates = [Path(path)]
    if os.environ.get('VOXEL_FONTS'):
        candidates.append(Path(os.environ['VOXEL_FONTS']) / Path(path).name)
    for candidate in candidates:
        if candidate.is_file():
            return bpy.data.fonts.load(str(candidate), check_existing=True)
    return bpy.data.fonts.get('Bfont')


def label(name, words, size, location, surface, parent=None, typeface=None, align='LEFT'):
    data = bpy.data.curves.new(name, 'FONT')
    data.body, data.size, data.align_x = words, size, align
    if typeface:
        data.font = typeface
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.parent = parent
    obj.location = location
    obj.rotation_euler.x = math.pi / 2
    data.materials.append(surface)
    return obj


def cap(head, config):
    red = material('Fucsia de gorra', config.get('cap_color', '#c41d4e'), .92, textured=True, spread=(.86, 1.05))
    shadow = material('Visera inferior', '#8a0d4c', .92)
    cream = material('Letras de gorra', '#fff0bc', emission=.16)
    rings = [(0, .74, .59), (.11, .73, .59), (.24, .59, .49),
             (.35, .39, .35), (.41, .13, .13)]
    count = 16
    vertices = [(a * math.cos(i * math.tau / count), b * math.sin(i * math.tau / count), z)
                for z, a, b in rings for i in range(count)]
    faces = [(j * count + i, j * count + (i + 1) % count,
              (j + 1) * count + (i + 1) % count, (j + 1) * count + i)
             for j in range(len(rings) - 1) for i in range(count)]
    faces.append(tuple(range((len(rings) - 1) * count, len(rings) * count)))
    mesh('Copa facetada', vertices, faces, red, head, (0, 0, .80))
    box('Visera', (0, -.76, .83), (1.48, .73, .095), red, head, .045)
    box('Canto violeta', (0, -.78, .785), (1.45, .68, .047), shadow, head, .019)
    box('Botón superior', (0, 0, 1.22), (.17, .14, .08), red, head, .025)
    text = config.get('cap_text', '')
    if text:
        logo = label('Texto sobre gorra', text, .24, (0, -.655, .955),
                     cream, head, font('/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf'), 'CENTER')
        logo.rotation_euler.x = math.radians(63)
        logo.visible_shadow = False  # flota sobre la copa: su sombra se leía como un logo duplicado


def human(root, config):
    # Colores planos y limpios: el grano en la piel se lee como suciedad (comparado con el original).
    skin = material('Piel', config.get('skin_color', '#d38a4c'), .55)
    jaw = material('Barba incipiente', config.get('beard_color', '#b0703a'), .62)
    hair = material('Pelo y cejas', config.get('hair_color', '#593412'))
    cloth = material('Camiseta', config.get('shirt_color', '#f3e4d8'), .8)
    jeans = material('Pantalón', config.get('pants_color', '#25306e'), .78, textured='fine', spread=(.86, 1.06))
    gold = material('Oro', '#cb9216', .24, .76)
    inks = material('Boca humana', '#5e1a0c', .5)
    lid = material('Párpado', '#3b2412')
    eye_white = material('Esclerótica', '#fff0e8')
    iris = material('Iris azul', config.get('eye_color', '#25375d'))
    shoe = material('Calzado', '#eee2d3')
    # Silueta del original: torso más angosto y piernas ~0,35 más largas (todo lo de arriba sube igual).
    torso = pivot('Control de torso', (0, 0, 1.91), root)
    shirt = box('Camiseta', (0, 0, .72), (1.34, .64, 1.53), cloth, torso, .09)
    box('Cintura', (0, 0, .03), (1.10, .58, .34), jeans, torso, .04)
    for sign in (-1, 1):
        x = sign * .27
        box('Pierna', (x, .02, 1.40), (.50, .50, 1.20), jeans, root, .03)
        box('Bota azul', (x, .015, .53), (.48, .49, .62), jeans, root, .025)
        box('Zapatilla', (x, -.12, .135), (.52, .78, .24), shoe, root, .06)
    box('Cuello', (0, -.025, 1.77), (.44, .44, .64), skin, torso, .035)
    box('Escote', (0, -.323, 1.40), (.46, .02, .16), skin, torso, .01)
    chain = [(math.cos(a) * .28, -.361 - math.sin(a) * .035, 1.51 - math.sin(a) * .28)
             for a in (i * math.pi / 36 for i in range(37))]
    if config.get('chain', True):
        tube('Cadena dorada', chain, .029, gold, torso)
    arms = []
    elbows = []
    for side in (-1, 1):
        arm = pivot('Hombro', (side * .69, 0, 1.29), torso)
        box('Manga', (side * .16, 0, -.16), (.46, .66, .43), cloth, arm, .065)
        box('Brazo', (side * .16, 0, -.53), (.36, .49, .52), skin, arm, .038)
        elbow = pivot('Codo', (side * .16, 0, -.75), arm)
        box('Antebrazo', (0, 0, -.23), (.35, .45, .49), skin, elbow, .037)
        box('Mano', (0, -.015, -.58), (.38, .48, .26), skin, elbow, .045)
        arms.append(arm)
        elbows.append(elbow)
    head = pivot('Control de cabeza', (.065, 0, 4.00), root)
    box('Cabeza', (0, 0, .21), (1.35, 1.04, 1.25), skin, head, .073)
    box('Mandíbula', (0, -.36, -.17), (1.33, .43, .50), jaw, head, .059)
    box('Pelo posterior', (0, .37, .48), (1.38, .46, .73), hair, head, .060)
    for side in (-1, 1):
        box('Oreja', (side * .685, -.10, .06), (.17, .34, .46), skin, head, .03)
        box('Pelo lateral', (side * .655, -.05, .50), (.10, .62, .40), hair, head, .02)
        box('Patilla', (side * .645, -.36, .33), (.09, .12, .22), hair, head, .01)
    eyes, irises, brows = [], [], []
    for side in (-1, 1):
        eye = pivot('Control de ojo', (side * .31, -.532, .33), head)
        box('Blanco del ojo', (0, 0, 0), (.29, .036, .19), eye_white, eye)
        irises.append(box('Iris', (.025, -.025, -.008), (.133, .021, .167), iris, eye))
        brows.append(box('Ceja gruesa', (side * .31, -.559, .52), (.33, .087, .086), hair, head, .012))
        eyes.append(eye)
    box('Nariz cúbica', (0, -.608, .164), (.183, .23, .27), skin, head, .034)
    box('Labio superior', (0, -.586, .012), (.47, .069, .079), jaw, head, .011)
    box('Mentón', (0, -.575, -.33), (.52, .06, .13), jaw, head, .012)
    mouth = box('Control visema humano', (0, -.588, -.086), (.37, .031, .18), inks, head)
    if config.get('cap', True):
        cap(head, config)
    return dict(root=root, head=head, torso=torso, shirt=shirt, arms=arms, elbows=elbows, eyes=eyes,
                brows=brows, irises=irises, mouth=mouth, kind='human')


def claude(root, config):
    orange = material('Naranja del cuerpo', config.get('body_color', '#e2611a'), .62, textured='streaks')
    foot = material('Cantos de las patas', config.get('foot_color', '#a8451a'), .7)
    ink = unlit('Píxeles de la cara', '#170906', .90)
    cheeks = material('Rubor', config.get('cheek_color', '#ef5a4c'), emission=.12)
    blue = material('Lágrima', '#4c9fe8', emission=.14)
    head = pivot('Control de cubo', (0, 0, .79), root)
    box('Cuerpo cúbico', (0, 0, .91), (2.83, .95, 1.82), orange, head, .008)
    for side in (-1, 1):
        box('Aleta', (side * 1.46, -.030, .53), (.15, .34, .37), orange, head, .026)
    legs = []
    for x in (-1.21, -.84, .84, 1.21):
        leg = pivot('Control de pata', (x, -.29 if abs(x) < 1 else -.16, .02), head)
        box('Pata rectangular', (0, 0, -.385), (.259, .33, .77), orange, leg)
        box('Extremo de pata', (0, -.003, -.697), (.261, .331, .13), foot, leg)
        legs.append(leg)
    patterns = {'normal': ('111', '111', '110'), 'square': ('11', '11'),
                'happy': ('010', '101'), 'sad': ('111',), 'wink': ('111',),
                'skeptic': ('010', '111', '110'),
                # Ojos nuevos: se reflejan solos en el ojo derecho (x * sign).
                'angry': ('100', '110', '111'), 'worried': ('001', '011', '111'),
                'heart': ('01010', '11111', '01110', '00100'), 'x': ('101', '010', '101'),
                'closed': ('000', '111'), 'star': ('010', '111', '010'), 'narrow': ('111', '111'),
                'wide': ('111', '111', '111', '111')}
    masks = {}
    for name, pattern in patterns.items():
        pair = []
        for sign in (-1, 1):
            eye = pivot('Ojo ' + name, (sign * 1.01, -.484, 1.28), head)
            for iy, line in enumerate(pattern):
                for ix, pixel in enumerate(line):
                    if pixel == '1':
                        x = (ix - (len(line) - 1) / 2) * .15 * sign
                        box('Píxel', (x, 0, (.9 - iy) * .15), (.151, .005, .151), ink, eye)
            pair.append(eye)
        masks[name] = pair
    for sign in (-1, 1):
        box('Mejilla rosada', (sign * 1.04, -.480, .99), (.29, .009, .083), cheeks, head)
    tear = box('Lágrima pixelada', (1.05, -.490, .993), (.057, .014, .16), blue, head)
    mouth = box('Control visema Claude', (0, -.484, .84), (.30, .017, .21), ink, head)
    corners = [box('Comisura', (sign * .175, -.485, .86), (.072, .014, .075), ink, head)
               for sign in (-1, 1)]
    # Bocas pixeladas en reposo (cuando no habla): sonrisa, puchero, ondulada y mueca grande.
    shapes = {}
    for name, rows in MOUTHS.items():
        holder = pivot('Boca ' + name, (0, -.486, .86), head)
        for iy, row in enumerate(rows):
            for ix, pixel in enumerate(row):
                if pixel == '1':
                    box('Píxel de boca', ((ix - (len(row) - 1) / 2) * .085, 0, -iy * .085), (.086, .005, .086), ink, holder)
        shapes[name] = holder
    # Íconos pixelados que aparecen sobre o junto a la cabeza, sincronizados con lo que dice.
    icons = {}
    for name, (hue, rows, anchor) in ICONS.items():
        glow = unlit('Ícono ' + name, hue, 1.25)
        holder = pivot('Ícono ' + name, anchor, head)
        for iy, row in enumerate(rows):
            for ix, pixel in enumerate(row):
                if pixel == '1':
                    box('Píxel de ícono', ((ix - (len(row) - 1) / 2) * .11, 0, (len(rows) - 1 - iy) * .11),
                        (.112, .06, .112), glow, holder)
        icons[name] = holder
    return dict(root=root, head=head, legs=legs, masks=masks, tear=tear, mouth=mouth,
                corners=corners, shapes=shapes, icons=icons, kind='claude')


MOUTHS = {
    'smile': ('10001', '01110'),
    'frown': ('01110', '10001'),
    'wavy': ('01010', '10101'),
    'grin': ('1111111', '1000001', '0111110'),
    'o': ('010', '101', '010'),
}

# nombre: (color, filas de píxeles, ancla relativa a la cabeza)
ICONS = {
    'heart': ('#ff3b6b', ('0110110', '1111111', '1111111', '0111110', '0011100', '0001000'), (.95, -.40, 2.05)),
    'question': ('#f2f3ff', ('01110', '10001', '00010', '00100', '00000', '00100'), (.95, -.40, 2.05)),
    'exclaim': ('#ffd23f', ('11', '11', '11', '11', '00', '11'), (.95, -.40, 2.05)),
    'sweat': ('#3d9bff', ('010', '010', '111', '111', '010'), (1.62, -.30, 1.50)),
    'zzz': ('#e8e9ff', ('1111', '0010', '0100', '1111'), (.95, -.40, 2.05)),
    'sparkle': ('#ffe066', ('00100', '00100', '11011', '00100', '00100'), (.95, -.40, 2.05)),
    'idea': ('#ffe066', ('01110', '11111', '11111', '01110', '01010', '01110'), (.95, -.40, 2.05)),
    'anger': ('#ff3b3b', ('1101', '1001', '0000', '1001', '1011'), (-1.05, -.40, 2.05)),
    'music': ('#c9b8ff', ('0011', '0011', '0010', '1110', '1110'), (.95, -.40, 2.05)),
    'skull': ('#f2f3ff', ('01110', '11111', '10101', '11111', '01010'), (.95, -.40, 2.05)),
}


def custom(root, config):
    assembly = pivot('Control de conjunto', parent=root)
    parts = {}
    library = {name: material(name, value) for name, value in config['materials'].items()}
    for part in config['parts']:
        parent = parts.get(part.get('parent'), assembly)
        control = pivot(part['id'], part.get('position', (0, 0, 0)), parent)
        box(part['id'], part.get('offset', (0, 0, 0)), part['size'], library[part['material']],
            control, part.get('bevel', 0))
        parts[part['id']] = control
    face = config.get('mouth')
    mouth = parts[face] if face else None
    return dict(root=root, head=parts.get(config.get('head'), assembly), parts=parts, mouth=mouth, kind='custom',
                closeup=config.get('closeup'))


def point_at(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat('-Z', 'Y').to_euler()


def lamp(name, loc, target, hue, energy, width, kind='AREA'):
    data = bpy.data.lights.new(name, kind)
    data.energy = energy
    data.color = color(hue)[:3]
    if kind == 'AREA':
        data.shape = 'DISK'
        data.size = width
    else:
        data.shadow_soft_size = width
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = loc
    point_at(obj, target)


def orb(name, location, radius, surface, parent=None):
    """Esfera baja (icosfera) para luces de estado con brillo."""
    import bmesh
    data = bpy.data.meshes.new(name)
    shape = bmesh.new()
    bmesh.ops.create_icosphere(shape, subdivisions=2, radius=radius)
    shape.to_mesh(data)
    shape.free()
    for polygon in data.polygons:
        polygon.use_smooth = True
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.parent = parent
    obj.location = location
    data.materials.append(surface)
    return obj


TERMINAL_ROWS = [
    'Reading 38 merge requests', 'working · 4:12', 'Approved with comments', 'git push origin main',
    'Release notes', 'feature request #4471', 'Merged PRs', 'All checks passed', 'deploy: production',
    'running tests', 'Review requested', 'npm run build', 'Tests: 212 passed', 'Diff +312 -58',
    'Added / Fixed', 'Grouped by label', 'Tagged v4 as feature request', 'open PRs: 7',
    'retry 2/3 · ok', 'lint: 0 errors',
]


def terminal_panel(rng, origin, width, height, fonts, glass, frame, inks, led, depth_rank):
    inks = inks[depth_rank]
    """Panel de terminal flotante: vidrio oscuro, borde tenue, líneas de log y un LED rojo."""
    x, y, z = origin
    box('Vidrio de panel', (x + width / 2, y + .03, z + height / 2), (width, .03, height), glass[depth_rank])
    for dz in (0, height):
        box('Borde de panel', (x + width / 2, y, z + dz), (width, .012, .012), frame)
    for dx in (0, width):
        box('Borde de panel', (x + dx, y, z + height / 2), (.012, .012, height), frame)
    size = (.15, .15, .17)[depth_rank]
    lines = max(2, int((height - .25) / (size * 1.55)))
    words = '\n'.join(rng.choice(TERMINAL_ROWS) for _ in range(lines))
    ink = inks[0] if rng.random() < .62 else inks[1]
    label('Registro de terminal', words, size, (x + .12, y - .01, z + height - .12 - size), ink,
          typeface=fonts[0] if rng.random() < .7 else fonts[1])
    if rng.random() < .7:
        orb('LED de estado', (x + width - .16, y - .04, z + .16 + rng.random() * (height - .3)), .05, led)


def stage(config):
    style = config.get('type', 'terminal')
    if style == 'custom':
        backdrop = material('Fondo', config.get('background', '#090d1d'), emission=.22)
        ground = material('Suelo', config.get('floor', '#181b2d'), .29, .38)
        box('Suelo', (0, 0, -.25), (160, 160, .30), ground)
        box('Fondo', (0, 10, 12), (200, .2, 80), backdrop)
        library = {name: material(name, value) for name, value in config.get('materials', {}).items()}
        for part in config.get('props', []):
            box(part['id'], part['position'], part['size'], library[part['material']], bevel=part.get('bevel', 0))
    elif style == 'terminal_classic':
        _classic_terminal()
    else:
        rng = random.Random(config.get('seed', 9))
        accent = config.get('accent', '#5b47ff')
        key_colors = config.get('keys', ['#0d0a3c', '#120e4c', '#171259', '#09072c'])
        # Bruma «horneada»: cuanto más lejos la capa, más azulado y claro su vidrio, sin costo de render.
        glass = [unlit('Vidrio cercano', '#06060f', 1), unlit('Vidrio medio', '#0c0d2e', 1), unlit('Vidrio lejano', '#121444', 1)]
        frame = unlit('Borde azul de panel', '#1b1f55', .9)
        frame_far = unlit('Borde lejano', '#1a1d5c', .5)
        text = config.get('text', '#d9dbef')
        inks = [[unlit('Texto terminal claro', text, 1.15), unlit('Texto terminal tenue', '#8e92b8', .8)],
                [unlit('Texto medio', text, .62), unlit('Texto medio tenue', '#7c80b0', .42)],
                [unlit('Texto lejano', '#a7acdf', .34), unlit('Texto lejano tenue', '#6d72a8', .24)]]
        led = unlit('LED rojo', '#ff2614', 9)
        fonts = [font('/System/Library/Fonts/Menlo.ttc'), font('/System/Library/Fonts/SFNSMono.ttf')]
        if fonts[1] is None:
            fonts[1] = fonts[0]
        box('Fondo profundo', (0, 22, 12), (160, .2, 70), unlit('Azul profundo', config.get('haze_color', '#0b0c30'), 1))
        if config.get('haze', 0):  # velo translúcido real: ~4× más lento; la bruma por defecto va en los materiales
            # Detrás de la capa cercana: los paneles próximos quedan nítidos y negros, los lejanos se velan.
            tint = config.get('haze_color', '#141650')
            box('Bruma azul', (0, 3.5, 12), (120, .01, 40), haze(tint, config.get('haze', .45)))
            box('Bruma azul lejana', (0, 7.2, 14), (160, .01, 50), haze(tint, config.get('haze', .45) * .8))
        # Tres capas de paneles a distintas profundidades: el desenfoque de cámara las separa.
        layers = [(2.3, 1.0, 1.3, 0), (4.6, 1.25, 1.9, 1), (8.5, 1.6, 2.6, 2)]
        for depth, scale_w, spread, rank in layers:
            x = -11 * spread
            while x < 11 * spread:
                width = rng.uniform(2.1, 3.3) * scale_w
                z = rng.uniform(-.3, 1.2) + rank * .6
                while z < 13 + rank * 3:
                    height = rng.uniform(1.4, 2.5) * scale_w
                    if rng.random() < .82:
                        terminal_panel(rng, (x, depth + rng.uniform(-.25, .25), z), width, height,
                                       fonts, glass, frame, inks, led, rank)
                    z += height + rng.uniform(.15, .7)
                x += width + rng.uniform(.2, .9)
        caps = [material('Tecla ' + c, c, .32, .25, textured='fine', spread=(.9, 1.08)) for c in key_colors]
        legend = unlit('Leyenda luminosa', config.get('legend', '#a59cff'), 2.2)
        legend_dim = unlit('Leyenda tenue', '#5a55a8', .9)
        box('Base del teclado', (0, -18, -.26), (60, 80, .30), material('Chasis', '#07061c', .4, .3))
        pitch = 1.76
        for iy in range(-17, 2):
            for ix in range(-10, 11):
                x, y = ix * pitch + (iy % 2) * .22, iy * pitch * .96
                box('Tecla', (x, y, -.08), (1.58, 1.52, .22), rng.choice(caps), bevel=.07)
                if rng.random() < .42:
                    glyph = label('Leyenda de tecla', rng.choice('ZXCVBNMASDFGHJKLQWERTYUIOP0123456789'),
                                  .34, (x - .45, y + .25, .032), legend if rng.random() < .55 else legend_dim)
                    glyph.rotation_euler.x = 0
        lamp('Reflejo violeta', (-1.2, -2.0, .55), (0, 0, 0), accent, 140, .3, 'POINT')
        lamp('Reflejo ámbar', (2.8, -3.0, .55), (0, 0, 0), '#ff9a3a', 60, .3, 'POINT')
        lamp('Lavado índigo del teclado', (0, -6, 6), (0, -6, 0), '#3d2cff', 260, 14)
    lights = config.get('lights', 'cinematic')
    if lights == 'classic' or style == 'terminal_classic':
        lamp('Luz cálida principal', (-2.9, -4.1, 6.6), (0, 0, 2.8), '#ffd1ac', 515, 3.9)
        lamp('Rebote cálido frontal', (2.0, -3.3, 3.8), (1.8, 0, 1.0), '#ffc789', 285, 2.75)
        lamp('Relleno violeta', (3.2, -1.8, 1.7), (0, 0, 2.3), '#6754ff', 110, 3.1)
        lamp('Contraluz cobre', (2.4, 1.8, 5.2), (0, 0, 2.4), '#ae6aff', 120, 3.5)
    else:
        # Clave cálida alta desde la izquierda, relleno frío débil y contraluz violeta: más
        # contraste y sombras con color, como la referencia.
        # Clave cálida alta y lateral con sombras definidas (la gorra sombrea la frente), relleno
        # frío débil del lado opuesto y contraluces: volumen como en el original.
        lamp('Luz cálida principal', (3.6, -5.4, 7.8), (0, 0, 2.6), '#ffc58a', 820, 1.4)
        lamp('Relleno frío', (-4.6, -4.2, 2.6), (0, 0, 2.0), '#8a7bff', 45, 4.5)
        lamp('Contraluz violeta', (-1.5, 2.6, 6.2), (0, 0, 2.4), '#9a5cff', 380, 3.0)
        lamp('Recorte cálido derecho', (5.0, -.5, 4.5), (1.6, 0, 1.4), '#ffb36b', 140, 1.6)


def _classic_terminal():
    rng = random.Random(9)
    glass = unlit('Vidrio terminal', '#040411', .79)
    edges = unlit('Líneas en azul', '#111541', .77)
    text = unlit('Texto terminal', '#666984', .66)
    muted = unlit('Texto terminal tenue', '#434560', .62)
    led = unlit('Estado rojo', '#ae190c', .78)
    keycaps = [material('Tecla ' + c, c, .35, .38) for c in ['#0c0b25', '#121035', '#181029', '#24203c']]
    wall_y = 1.86
    box('Pared de terminal', (0, wall_y + .13, 8.8), (44, .20, 18), glass)
    mono = font('/System/Library/Fonts/Menlo.ttc')
    rows = TERMINAL_ROWS[:11]
    for ix in range(-8, 9):
        x = ix * 1.44 + .12
        box('Divisor vertical', (x, wall_y, 5), (.015, .015, 10), edges)
        z = .2 + rng.random() * .6
        while z < 12:
            height = rng.uniform(.98, 1.65)
            box('Divisor horizontal', (x + .72, wall_y, z), (1.44, .015, .013), edges)
            label('Registro de terminal', '\n'.join(rng.choices(rows, k=4)), .093,
                  (x + .075, wall_y - .018, z + height - .13), rng.choice([text, muted]), typeface=mono)
            box('Indicador rojo', (x + 1.27, wall_y - .020, z + .13), (.056, .018, .056), led, bevel=.016)
            z += height
    box('Base del teclado', (0, -18, -.24), (48, 78, .30), keycaps[0])
    for iy in [*range(-18, 4), *range(-29, -18)]:
        for ix in range(-11, 12):
            x, y = ix * 1.32 + (iy % 2) * .14, iy * 1.43
            box('Tecla', (x, y, -.10), (1.21, 1.28, .18), rng.choice(keycaps), bevel=.034)
            if ix in range(-6, 7) and iy in range(-9, 2) and rng.random() < .37:
                glyph = label('Leyenda de tecla', rng.choice('ZXCVBNMASDFGHJKLQWERTYUIOP'), .19,
                              (x, y, -.004), muted, align='CENTER')
                glyph.rotation_euler.x = 0
    lamp('Reflejo violeta', (-1, -2.20, .42), (0, 0, 0), '#5741ff', 82, .21, 'POINT')
    lamp('Reflejo ámbar', (2.7, -3.2, .52), (0, 0, 0), '#ff9f3a', 21, .24, 'POINT')
