bl_info = {
    "name": "Gacha Animation Tools",
    "author": "Yolu",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Animation Tools",
    "description": "Animation proxy mesh maker + pose/animation library saved to disk",
    "category": "Animation",
}

import json
import os
import re
import shutil

import bpy
import bmesh
from bpy.props import (BoolProperty, CollectionProperty, FloatProperty,
                       IntProperty, PointerProperty, StringProperty)
from mathutils import Euler, Quaternion, Vector


# ============================================================================
# PART 1 - ANIMATION PROXY
# ============================================================================

def split_keywords(text):
    return [k.strip().lower() for k in text.split(",") if k.strip()]


class ProxySettings(bpy.types.PropertyGroup):
    ratio: FloatProperty(name="Decimate Ratio", default=0.2, min=0.01, max=1.0)
    merge_dist: FloatProperty(name="Merge Distance", default=0.0001,
                              min=0.0, max=1.0, precision=5, unit='LENGTH')
    remove_outline: BoolProperty(
        name="Remove Outline Modifiers", default=True,
        description="Delete modifiers whose name contains one of the keywords below")
    outline_keywords: StringProperty(name="Outline Keywords",
                                     default="outline,edge,inverted hull")
    remove_solidify_flipped: BoolProperty(
        name="Remove Flipped Solidify", default=True,
        description="Solidify modifiers with flipped normals are usually outlines")
    remove_shape_keys: BoolProperty(
        name="Remove Shape Keys", default=False,
        description="Optional: bake the current shape key mix and delete the shape keys "
                    "from the proxy. Not needed for decimating")
    protect: BoolProperty(name="Protect Parts From Decimate", default=True)
    protect_materials: StringProperty(
        name="Materials", default="Face, Eye, Pupil",
        description="Comma separated. Any material whose name contains one of these is kept intact")
    protect_vgroup: StringProperty(
        name="Vertex Group", default="",
        description="Optional: vertices in this group are also kept intact")
    hide_original: BoolProperty(name="Hide Original", default=False)
    solid_texture: BoolProperty(
        name="Solid + Texture Viewport", default=True,
        description="Switch the viewport to Solid shading with Texture color. "
                    "Much faster than Material Preview")
    disable_original_outline: BoolProperty(
        name="Disable Outline On Original", default=True,
        description="Turn off the outline modifiers in the viewport on the original mesh. "
                    "They make the viewport very slow even when the mesh is hidden")
    simple_materials: BoolProperty(
        name="Simplify Materials", default=True,
        description="Replace the shaders on the proxy with a basic Principled BSDF "
                    "using the diffuse texture as Base Color")
    diffuse_suffix: StringProperty(
        name="Diffuse Suffix", default="_D",
        description="Image names ending with this (before the extension) are treated as the base color")
    connect_alpha: BoolProperty(
        name="Connect Alpha", default=False,
        description="Plug the texture alpha into the material alpha (hair cards, lace)")


def is_outline_mod(mod, s):
    kws = split_keywords(s.outline_keywords)
    name = mod.name.lower()
    if s.remove_outline and any(k in name for k in kws):
        return True
    if (s.remove_solidify_flipped and mod.type == 'SOLIDIFY'
            and mod.use_flip_normals):
        return True
    if (s.remove_outline and mod.type == 'NODES' and mod.node_group
            and any(k in mod.node_group.name.lower() for k in kws)):
        return True
    return False


def active_outline_mods(obj, s):
    return [m for m in obj.modifiers if is_outline_mod(m, s) and m.show_viewport]


def clean_modifiers(obj, s):
    removed = []
    for mod in list(obj.modifiers):
        if is_outline_mod(mod, s):
            removed.append(mod.name)
            obj.modifiers.remove(mod)
    return removed


def protected_face_mask(obj, s, bm):
    """Return set of face indices that must NOT be decimated."""
    mat_kws = split_keywords(s.protect_materials)
    protected_slots = set()
    for i, slot in enumerate(obj.material_slots):
        if slot.material and any(k in slot.material.name.lower() for k in mat_kws):
            protected_slots.add(i)

    protected_verts = set()
    vg = obj.vertex_groups.get(s.protect_vgroup) if s.protect_vgroup else None
    if vg:
        dvert = bm.verts.layers.deform.active
        if dvert:
            for v in bm.verts:
                if vg.index in v[dvert] and v[dvert][vg.index] > 0.0:
                    protected_verts.add(v.index)

    protected = set()
    for f in bm.faces:
        if f.material_index in protected_slots:
            protected.add(f.index)
        elif protected_verts and any(v.index in protected_verts for v in f.verts):
            protected.add(f.index)
    return protected


def cleanup_mesh(context, obj, s):
    """Edit-mode Merge by Distance + Decimate Geometry (the Clean Up tools).
    Unlike the Decimate modifier these work on meshes with shape keys."""
    for o in context.view_layer.objects:
        o.select_set(False)
    obj.hide_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj

    before = len(obj.data.vertices)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.reveal(select=False)
    bpy.ops.mesh.select_mode(type='VERT')

    # Merge by distance
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.remove_doubles(threshold=s.merge_dist)

    # Select everything that may be decimated
    bpy.ops.mesh.select_mode(type='FACE')
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    keep = protected_face_mask(obj, s, bm) if s.protect else set()
    for f in bm.faces:
        f.select_set(f.index not in keep)
    bm.select_flush_mode()
    bmesh.update_edit_mesh(obj.data)

    # Decimate Geometry
    bpy.ops.mesh.decimate(ratio=s.ratio, use_vertex_group=False,
                          use_symmetry=False)

    bpy.ops.object.mode_set(mode='OBJECT')
    return before - len(obj.data.vertices), len(keep)


def collect_images(tree, found, visited):
    if tree is None or tree in visited:
        return
    visited.add(tree)
    for node in tree.nodes:
        if node.type == 'TEX_IMAGE' and node.image:
            found.append((node, node.image))
        elif node.type == 'GROUP':
            collect_images(node.node_tree, found, visited)


def find_diffuse_image(mat, suffix):
    found = []
    collect_images(mat.node_tree, found, set())
    if not found:
        return None
    pat = re.compile(re.escape(suffix.lower()) + r"(\.[a-z0-9]+)?(\.\d+)?$")
    for node, img in found:
        if pat.search(img.name.lower()):
            return img
    for node, img in found:  # fallback: node label/name says diffuse
        if "diffuse" in (node.label + node.name).lower():
            return img
    return None


def simplify_materials(obj, s):
    cache = {}
    for slot in obj.material_slots:
        orig = slot.material
        if orig is None:
            continue
        if orig.name not in cache:
            img = find_diffuse_image(orig, s.diffuse_suffix) if orig.use_nodes else None
            new = bpy.data.materials.new(orig.name + "_Proxy")
            new.use_nodes = True
            nt = new.node_tree
            bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
            bsdf.inputs["Roughness"].default_value = 0.8
            for name in ("Specular IOR Level", "Specular"):
                if name in bsdf.inputs:
                    bsdf.inputs[name].default_value = 0.0
                    break
            if img:
                tex = nt.nodes.new('ShaderNodeTexImage')
                tex.image = img
                tex.location = (-380, 200)
                nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
                if s.connect_alpha:
                    nt.links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
                    if hasattr(new, "surface_render_method"):
                        new.surface_render_method = 'BLENDED'
                    elif hasattr(new, "blend_method"):
                        new.blend_method = 'BLEND'
            else:
                new.diffuse_color = orig.diffuse_color
                bsdf.inputs["Base Color"].default_value = orig.diffuse_color
            cache[orig.name] = (new, img is not None)
        slot.material = cache[orig.name][0]
    missing = [k for k, (m, ok) in cache.items() if not ok]
    return len(cache), missing


def make_proxy(context, src, s, report):
    # 1. duplicate (own copy of mesh data)
    proxy = src.copy()
    proxy.data = src.data.copy()
    proxy.name = "Animation Proxy"
    proxy.data.name = src.data.name + "_Proxy"
    for col in src.users_collection:
        col.objects.link(proxy)

    # 2. strip outline modifiers
    removed = clean_modifiers(proxy, s)

    # 3. shape keys
    if proxy.data.shape_keys and s.remove_shape_keys:
        with context.temp_override(object=proxy, active_object=proxy):
            bpy.ops.object.shape_key_remove(all=True, apply_mix=True)

    removed_verts, kept = cleanup_mesh(context, proxy, s)

    if s.simple_materials:
        n, missing = simplify_materials(proxy, s)
        if missing:
            report({'WARNING'}, "No diffuse texture found for: " + ", ".join(missing))

    if s.disable_original_outline:
        for m in active_outline_mods(src, s):
            m.show_viewport = False

    if s.hide_original:
        # Eye icon only, so Alt+H still brings it back.
        src.hide_set(True)

    report({'INFO'}, f"{proxy.name}: {len(proxy.data.vertices)} verts "
                     f"(-{removed_verts}, removed modifiers: {', '.join(removed) or 'none'})")
    return proxy


def set_solid_texture(context):
    spaces = []
    if context.space_data and context.space_data.type == 'VIEW_3D':
        spaces.append(context.space_data)
    else:
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                spaces.append(area.spaces.active)
    for sp in spaces:
        sp.shading.type = 'SOLID'
        sp.shading.color_type = 'TEXTURE'


class OBJECT_OT_make_proxy(bpy.types.Operator):
    bl_idname = "object.make_proxy"
    bl_label = "Create Animation Proxy"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        s = context.scene.proxy_settings
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        sources = [o for o in context.selected_objects if o.type == 'MESH']
        new = []
        for src in sources:
            new.append(make_proxy(context, src, s, self.report))
        if s.solid_texture:
            set_solid_texture(context)
        bpy.ops.object.select_all(action='DESELECT')
        for p in new:
            p.select_set(True)
        context.view_layer.objects.active = new[-1]
        return {'FINISHED'}


class OBJECT_OT_disable_outline(bpy.types.Operator):
    bl_idname = "object.disable_outline"
    bl_label = "Disable Outline (Viewport)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.proxy_settings
        count = 0
        for o in context.selected_objects:
            if o.type == 'MESH':
                for m in active_outline_mods(o, s):
                    m.show_viewport = False
                    count += 1
        self.report({'INFO'}, f"Disabled {count} outline modifier(s) in viewport")
        return {'FINISHED'}


class VIEW3D_PT_proxy_maker(bpy.types.Panel):
    bl_label = "Animation Proxy"
    bl_idname = "VIEW3D_PT_proxy_maker"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Animation Tools"

    def draw(self, context):
        s = context.scene.proxy_settings
        col = self.layout.column()
        obj = context.object
        if obj and obj.type == 'MESH':
            bad = active_outline_mods(obj, s)
            if bad:
                box = col.box()
                box.alert = True
                box.label(text="Outline enabled on this mesh!", icon='ERROR')
                box.label(text="It slows the viewport a lot.")
                box.label(text=f"({', '.join(m.name for m in bad[:2])}"
                               f"{'...' if len(bad) > 2 else ''})")
                box.operator("object.disable_outline", icon='HIDE_ON')
        col.prop(s, "ratio")
        col.prop(s, "merge_dist")
        col.separator()
        col.prop(s, "remove_outline")
        if s.remove_outline:
            col.prop(s, "outline_keywords", text="")
        col.prop(s, "remove_solidify_flipped")
        col.prop(s, "remove_shape_keys")
        col.separator()
        col.prop(s, "protect")
        if s.protect:
            box = col.box()
            box.prop(s, "protect_materials")
            box.prop_search(s, "protect_vgroup",
                            context.object if context.object else context.scene,
                            "vertex_groups" if context.object else "objects")
        col.separator()
        col.prop(s, "simple_materials")
        if s.simple_materials:
            box = col.box()
            box.prop(s, "diffuse_suffix")
            box.prop(s, "connect_alpha")
        col.prop(s, "solid_texture")
        col.prop(s, "hide_original")
        col.prop(s, "disable_original_outline")
        col.operator("object.make_proxy", icon='MOD_DECIM')




# ============================================================================
# PART 2 - POSE / ANIMATION LIBRARY
# ============================================================================

POSE_EXT = ".pose.json"
ANIM_EXT = ".anim.json"
PATH_RE = re.compile(r'^pose\.bones\["(.+?)"\](.*)$')


# ----------------------------------------------------------------------------
# Library folder
# ----------------------------------------------------------------------------

class AnimLibPrefs(bpy.types.AddonPreferences):
    bl_idname = __name__

    library_dir: StringProperty(
        name="Library Folder", subtype='DIR_PATH', default="",
        description="Where poses and animations are stored. "
                    "Leave empty to use Blender's config folder")

    def draw(self, context):
        self.layout.prop(self, "library_dir")
        self.layout.label(text="Current: " + library_dir(context))


def library_dir(context):
    path = ""
    try:
        path = context.preferences.addons[__name__].preferences.library_dir
    except (KeyError, AttributeError):
        pass
    if not path:
        path = os.path.join(bpy.utils.user_resource('CONFIG', create=True),
                            "animation_library")
    return bpy.path.abspath(path)


def sub_dir(context, kind):
    path = os.path.join(library_dir(context), kind)
    os.makedirs(path, exist_ok=True)
    return path


def safe_name(name):
    return re.sub(r'[^\w\-. ]', '_', name.strip())


def list_names(folder, ext):
    if not os.path.isdir(folder):
        return []
    return sorted(f[:-len(ext)] for f in os.listdir(folder) if f.endswith(ext))


def refresh_lists(context):
    wm = context.window_manager
    for coll, kind, ext in ((wm.animlib_poses, "poses", POSE_EXT),
                            (wm.animlib_anims, "anims", ANIM_EXT)):
        coll.clear()
        for n in list_names(sub_dir(context, kind), ext):
            coll.add().name = n


def selected_item(coll, index):
    if 0 <= index < len(coll):
        return coll[index].name
    return None


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def active_armature(context):
    obj = context.active_object
    if obj and obj.type == 'ARMATURE':
        return obj
    return None


def selected_bone_names(context, obj):
    if context.mode == 'POSE' and context.selected_pose_bones is not None:
        return {pb.name for pb in context.selected_pose_bones}
    return {pb.name for pb in obj.pose.bones if pb.bone.select}


def rot_prop(mode):
    if mode == 'QUATERNION':
        return "rotation_quaternion"
    if mode == 'AXIS_ANGLE':
        return "rotation_axis_angle"
    return "rotation_euler"


def quat_from_values(mode, quat, euler, axis):
    if mode == 'QUATERNION':
        return Quaternion(quat)
    if mode == 'AXIS_ANGLE':
        v = Vector(axis[1:4])
        if v.length == 0:
            return Quaternion()
        return Quaternion(v, axis[0])
    return Euler(euler, mode).to_quaternion()


def current_quat(pb):
    return quat_from_values(pb.rotation_mode, pb.rotation_quaternion,
                            pb.rotation_euler, pb.rotation_axis_angle)


def set_rotation(pb, q):
    mode = pb.rotation_mode
    if mode == 'QUATERNION':
        pb.rotation_quaternion = q
    elif mode == 'AXIS_ANGLE':
        axis, angle = q.to_axis_angle()
        pb.rotation_axis_angle = (angle, axis.x, axis.y, axis.z)
    else:
        pb.rotation_euler = q.to_euler(mode, pb.rotation_euler)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_with_backup(path, data):
    """Save JSON; if the file already exists keep the old one as <file>.bak"""
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
    write_json(path, data)


def confirm_save(operator, context, event, kind, name, path):
    s = context.window_manager.animlib
    exists = bool(name) and os.path.exists(path)
    if not s.confirm_save:
        return operator.execute(context)  # old file is still kept as .bak
    if exists:
        title = f"Overwrite {kind}?"
        message = f"'{name}' already exists and will be replaced (old one kept as .bak)."
        ok = "Overwrite"
    else:
        title = f"Save {kind}?"
        message = f"Save {kind} '{name}'?"
        ok = "Save"
    try:
        return context.window_manager.invoke_confirm(
            operator, event, title=title, message=message, confirm_text=ok)
    except TypeError:
        return context.window_manager.invoke_confirm(operator, event)


def iter_fcurves(obj):
    """All fcurves of the object's active action (works on legacy and layered actions)."""
    ad = obj.animation_data
    if not ad or not ad.action:
        return
    action = ad.action
    if hasattr(action, "layers"):  # Blender 4.4+ slotted actions
        slot = getattr(ad, "action_slot", None)
        if slot is None:
            return
        for layer in action.layers:
            for strip in layer.strips:
                if strip.type != 'KEYFRAME':
                    continue
                bag = strip.channelbag(slot)
                if bag:
                    yield from bag.fcurves
    else:
        yield from action.fcurves


# ----------------------------------------------------------------------------
# Pose save / apply
# ----------------------------------------------------------------------------

# ---- bone name matching (so Genshin / WuWa / Rigify names can still meet) ----

_SIDE_L = {"l", "left"}
_SIDE_R = {"r", "right"}
_DROP = {"bip001", "bip", "mixamorig", "valvebiped", "jnt", "joint", "bone", "f"}
_SYN = {"pinkie": "pinky", "little": "pinky", "pointer": "index",
        "forefinger": "index"}


def norm_bone(name):
    """'f_index.01.L', 'Index1_L', 'Bip001 L Index1' ... -> 'index 1 l'"""
    n = re.sub(r'(?i)bip\d*', ' ', name)  # "Bip001" prefix
    n = re.sub(r'([a-z])([A-Z])', r'\1 \2', n)
    n = re.sub(r'([A-Za-z])(\d)', r'\1 \2', n)
    n = re.sub(r'(\d)([A-Za-z])', r'\1 \2', n)
    side, out = "", []
    for t in (t for t in re.split(r'[\s._\-:]+', n.lower()) if t):
        if t in _SIDE_L:
            side = "l"
        elif t in _SIDE_R:
            side = "r"
        elif t in _DROP:
            continue
        elif t.isdigit():
            out.append(str(int(t)))
        else:
            out.append(_SYN.get(t, t))
    return (" ".join(out) + " " + side).strip()


def load_name_map(context):
    """Optional user file <library>/name_map.json : {"source bone": "target bone"}"""
    path = os.path.join(library_dir(context), "name_map.json")
    if os.path.exists(path):
        try:
            return read_json(path)
        except Exception:
            return {}
    return {}


def make_resolver(context, obj, fuzzy):
    pbs = obj.pose.bones
    name_map = load_name_map(context)
    index = {}
    if fuzzy:
        for pb in pbs:
            index.setdefault(norm_bone(pb.name), []).append(pb)

    def resolve(name):
        name = name_map.get(name, name)
        pb = pbs.get(name)
        if pb is not None:
            return pb
        if fuzzy:
            cands = index.get(norm_bone(name))
            if cands and len(cands) == 1:
                return cands[0]
        return None
    return resolve


def rest_quat(pb):
    return pb.bone.matrix_local.to_3x3().normalized().to_quaternion()


def rel_delta(pb):
    """The bone's own rotation expressed in rest-pose (armature) axes.
    Independent of the bone's roll and of how the parent is posed."""
    R = rest_quat(pb)
    return R @ current_quat(pb) @ R.inverted()


def capture_pose(obj, names):
    bones = {}
    for pb in obj.pose.bones:
        if names is not None and pb.name not in names:
            continue
        bones[pb.name] = {
            "mode": pb.rotation_mode,
            "loc": list(pb.location),
            "quat": list(pb.rotation_quaternion),
            "euler": list(pb.rotation_euler),
            "axis": list(pb.rotation_axis_angle),
            "scale": list(pb.scale),
            "rel_delta": list(rel_delta(pb)),
        }
    return {"type": "pose", "version": 1, "source": obj.name, "bones": bones}


def _keyframe_pose_bone(pb, frame):
    for path in ("location", rot_prop(pb.rotation_mode), "scale"):
        pb.keyframe_insert(data_path=path, frame=frame, group=pb.name)


def apply_pose(context, obj, data, selected_only, blend, keyframe,
               resolve, cross_rig, rotation_only, name_filter):
    sel = selected_bone_names(context, obj) if selected_only else None
    frame = context.scene.frame_current
    filters = split_keywords(name_filter)
    applied, missing, fallback = 0, [], 0

    for name, d in data["bones"].items():
        if filters and not any(k in name.lower() for k in filters):
            continue
        pb = resolve(name)
        if pb is None:
            missing.append(name)
            continue
        if sel is not None and pb.name not in sel:
            continue

        if cross_rig and "rel_delta" in d:
            R = rest_quat(pb)
            q_full = R.inverted() @ Quaternion(d["rel_delta"]) @ R
            set_rotation(pb, current_quat(pb).slerp(q_full, blend))
            pb.scale = Vector(pb.scale).lerp(Vector(d["scale"]), blend)
        else:
            if cross_rig:
                fallback += 1
            set_local(pb, d, blend, rotation_only)

        if keyframe:
            _keyframe_pose_bone(pb, frame)
        applied += 1

    context.view_layer.update()
    return applied, missing, fallback


def set_local(pb, d, blend, rotation_only=False):
    s_quat = quat_from_values(d["mode"], d["quat"], d["euler"], d["axis"])
    if blend >= 0.999:
        if not rotation_only:
            pb.location = d["loc"]
        pb.scale = d["scale"]
        if d["mode"] == pb.rotation_mode:  # exact copy, no conversion
            key = {"QUATERNION": "quat", "AXIS_ANGLE": "axis"}.get(d["mode"], "euler")
            setattr(pb, rot_prop(d["mode"]), d[key])
        else:
            set_rotation(pb, s_quat)
    else:
        if not rotation_only:
            pb.location = Vector(pb.location).lerp(Vector(d["loc"]), blend)
        pb.scale = Vector(pb.scale).lerp(Vector(d["scale"]), blend)
        set_rotation(pb, current_quat(pb).slerp(s_quat, blend))


# ----------------------------------------------------------------------------
# Animation save / load
# ----------------------------------------------------------------------------

def capture_animation(context, obj, selected_only):
    sel = selected_bone_names(context, obj) if selected_only else None
    channels, modes = [], {}
    for fc in iter_fcurves(obj):
        m = PATH_RE.match(fc.data_path)
        if not m or not len(fc.keyframe_points):
            continue
        bone, rest = m.groups()
        if sel is not None and bone not in sel:
            continue
        keys = []
        for kp in fc.keyframe_points:
            keys.append({
                "co": [kp.co[0], kp.co[1]],
                "interp": kp.interpolation,
                "easing": kp.easing,
                "hlt": kp.handle_left_type,
                "hrt": kp.handle_right_type,
                "hl": [kp.handle_left[0], kp.handle_left[1]],
                "hr": [kp.handle_right[0], kp.handle_right[1]],
            })
        channels.append({"bone": bone, "path": rest,
                         "index": fc.array_index, "keys": keys})
        pb = obj.pose.bones.get(bone)
        if pb:
            modes[bone] = pb.rotation_mode

    if not channels:
        return None

    start = min(k["co"][0] for c in channels for k in c["keys"])
    end = max(k["co"][0] for c in channels for k in c["keys"])
    for c in channels:
        for k in c["keys"]:
            k["co"][0] -= start
            k["hl"][0] -= start
            k["hr"][0] -= start
    return {"type": "animation", "version": 1, "source": obj.name,
            "fps": context.scene.render.fps, "original_start": start,
            "length": end - start, "rotation_modes": modes,
            "channels": channels}


def load_animation(context, obj, data, at_current, set_range, selected_only, resolve):
    sel = selected_bone_names(context, obj) if selected_only else None
    offset = context.scene.frame_current if at_current else data["original_start"]

    for name, mode in data.get("rotation_modes", {}).items():
        pb = resolve(name)
        if pb and pb.rotation_mode != mode:
            pb.rotation_mode = mode

    chans, skipped_bones = [], set()
    for ch in data["channels"]:
        pb = resolve(ch["bone"])
        if pb is None or (sel is not None and pb.name not in sel):
            if pb is None:
                skipped_bones.add(ch["bone"])
            continue
        full = 'pose.bones["%s"]%s' % (pb.name, ch["path"])
        chans.append((pb, ch, full))

    if obj.animation_data is None:
        obj.animation_data_create()

    # Pass 1: make sure every fcurve exists (this also creates action/slot).
    existing = {(fc.data_path, fc.array_index) for fc in iter_fcurves(obj)}
    for pb, ch, full in chans:
        if (full, ch["index"]) in existing:
            continue
        rel = ch["path"].lstrip(".")
        frame = ch["keys"][0]["co"][0] + offset
        idx = -1 if rel.startswith("[") else ch["index"]
        try:
            pb.keyframe_insert(data_path=rel, index=idx, frame=frame, group=pb.name)
        except (RuntimeError, TypeError):
            try:
                pb.keyframe_insert(data_path=rel, index=-1, frame=frame, group=pb.name)
            except (RuntimeError, TypeError):
                continue
        existing.add((full, ch["index"]))

    # Pass 2: write the real keys.
    fmap = {(fc.data_path, fc.array_index): fc for fc in iter_fcurves(obj)}
    done, failed = 0, 0
    touched = []
    for pb, ch, full in chans:
        fc = fmap.get((full, ch["index"]))
        if fc is None:
            failed += 1
            continue
        for k in ch["keys"]:
            kp = fc.keyframe_points.insert(k["co"][0] + offset, k["co"][1],
                                           options={'FAST'})
            kp.interpolation = k["interp"]
            kp.easing = k.get("easing", 'AUTO')
            kp.handle_left_type = k["hlt"]
            kp.handle_right_type = k["hrt"]
            kp.handle_left = (k["hl"][0] + offset, k["hl"][1])
            kp.handle_right = (k["hr"][0] + offset, k["hr"][1])
        touched.append(fc)
        done += 1
    for fc in touched:
        fc.update()

    if set_range:
        context.scene.frame_start = int(offset)
        context.scene.frame_end = int(round(offset + data["length"]))
    context.view_layer.update()
    return done, failed, sorted(skipped_bones)


# ----------------------------------------------------------------------------
# Settings + list
# ----------------------------------------------------------------------------

class AnimLibItem(bpy.types.PropertyGroup):
    name: StringProperty()


class AnimLibSettings(bpy.types.PropertyGroup):
    pose_name: StringProperty(name="Pose Name", default="Pose")
    pose_save_selected: BoolProperty(
        name="Selected Bones Only", default=True,
        description="Save only the selected bones (e.g. just the hand)")
    pose_apply_selected: BoolProperty(
        name="Apply To Selected Bones Only", default=True,
        description="Only bones you have selected in Pose Mode receive the pose")
    confirm_save: BoolProperty(
        name="Confirm Before Saving", default=True,
        description="Ask 'Are you sure?' before saving a pose or animation. "
                    "Overwritten files are also kept as .bak backups")
    confirm_delete: BoolProperty(
        name="Confirm Before Deleting", default=True,
        description="Ask 'Are you sure?' before deleting a pose or animation")
    match_names: BoolProperty(
        name="Match Similar Bone Names", default=True,
        description="If a bone name isn't found exactly, match it by meaning, "
                    "e.g. f_index.01.L = Index1_L = Bip001 L Index1. "
                    "Extra rules can go in name_map.json in the library folder")
    pose_cross_rig: BoolProperty(
        name="Hand Fix (Match Orientation)", default=False,
        description="Use this when the pose comes from another rig whose bones are "
                    "rolled/oriented differently. Converts each bone's rotation "
                    "through its rest orientation (poses saved with this version "
                    "only; location is not copied)")
    pose_rotation_only: BoolProperty(
        name="Rotation Only (No Location)", default=False,
        description="Don't paste bone locations. Recommended for hands/fingers and "
                    "for poses from another character: IK/hand controls would "
                    "otherwise be thrown to the other rig's positions")
    pose_name_filter: StringProperty(
        name="Only Bones Containing", default="",
        description="Comma separated, e.g. f_,thumb,palm. Only saved bones whose "
                    "name contains one of these are applied. Empty = all")
    pose_blend: FloatProperty(name="Blend", default=1.0, min=0.0, max=1.0,
                              subtype='FACTOR')
    pose_keyframe: BoolProperty(name="Insert Keyframes", default=False)

    anim_name: StringProperty(name="Animation Name", default="Animation")
    anim_save_selected: BoolProperty(name="Selected Bones Only", default=False)
    anim_at_current: BoolProperty(
        name="Start At Current Frame", default=True,
        description="Off: keep the original frame numbers")
    anim_set_range: BoolProperty(name="Set Scene Frame Range", default=False)
    anim_load_selected: BoolProperty(name="Load To Selected Bones Only",
                                     default=False)


class ANIMLIB_UL_items(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        layout.label(text=item.name, icon='ARMATURE_DATA')


# ----------------------------------------------------------------------------
# Operators
# ----------------------------------------------------------------------------

class ANIMLIB_OT_refresh(bpy.types.Operator):
    bl_idname = "animlib.refresh"
    bl_label = "Refresh"

    def execute(self, context):
        refresh_lists(context)
        return {'FINISHED'}


class ANIMLIB_OT_open_folder(bpy.types.Operator):
    bl_idname = "animlib.open_folder"
    bl_label = "Open Folder"

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=library_dir(context))
        return {'FINISHED'}


class ANIMLIB_OT_save_pose(bpy.types.Operator):
    bl_idname = "animlib.save_pose"
    bl_label = "Save Pose"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return active_armature(context) is not None

    def invoke(self, context, event):
        s = context.window_manager.animlib
        name = safe_name(s.pose_name)
        path = os.path.join(sub_dir(context, "poses"), name + POSE_EXT)
        return confirm_save(self, context, event, "pose", name, path)

    def execute(self, context):
        s = context.window_manager.animlib
        obj = active_armature(context)
        name = safe_name(s.pose_name)
        if not name:
            self.report({'ERROR'}, "Enter a pose name")
            return {'CANCELLED'}
        names = None
        if s.pose_save_selected:
            names = selected_bone_names(context, obj)
            if not names:
                self.report({'ERROR'}, "No bones selected")
                return {'CANCELLED'}
        data = capture_pose(obj, names)
        write_with_backup(os.path.join(sub_dir(context, "poses"), name + POSE_EXT), data)
        refresh_lists(context)
        self.report({'INFO'}, f"Saved pose '{name}' ({len(data['bones'])} bones)")
        return {'FINISHED'}


class ANIMLIB_OT_apply_pose(bpy.types.Operator):
    bl_idname = "animlib.apply_pose"
    bl_label = "Apply Pose"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_armature(context) is not None

    def execute(self, context):
        wm = context.window_manager
        s = wm.animlib
        name = selected_item(wm.animlib_poses, wm.animlib_pose_index)
        if not name:
            self.report({'ERROR'}, "Select a pose in the list")
            return {'CANCELLED'}
        data = read_json(os.path.join(sub_dir(context, "poses"), name + POSE_EXT))
        obj = active_armature(context)
        if s.pose_apply_selected and not selected_bone_names(context, obj):
            self.report({'ERROR'}, "No bones selected. Select the bones to apply the "
                                   "pose to, or turn off 'Apply To Selected Bones Only'")
            return {'CANCELLED'}
        resolve = make_resolver(context, obj, s.match_names)
        applied, missing, fallback = apply_pose(
            context, obj, data, s.pose_apply_selected, s.pose_blend,
            s.pose_keyframe, resolve, s.pose_cross_rig,
            s.pose_rotation_only, s.pose_name_filter)
        msg = f"Applied '{name}' to {applied} bones"
        if fallback:
            msg += f" ({fallback} used local copy: re-save the pose with this version)"
        if missing:
            msg += f" ({len(missing)} not found: {', '.join(missing[:4])}...)"
        self.report({'WARNING' if missing and not applied else 'INFO'}, msg)
        return {'FINISHED'}


class ANIMLIB_OT_save_anim(bpy.types.Operator):
    bl_idname = "animlib.save_anim"
    bl_label = "Save Animation"

    @classmethod
    def poll(cls, context):
        obj = active_armature(context)
        return obj is not None and obj.animation_data and obj.animation_data.action

    def invoke(self, context, event):
        s = context.window_manager.animlib
        name = safe_name(s.anim_name)
        path = os.path.join(sub_dir(context, "anims"), name + ANIM_EXT)
        return confirm_save(self, context, event, "animation", name, path)

    def execute(self, context):
        s = context.window_manager.animlib
        obj = active_armature(context)
        name = safe_name(s.anim_name)
        if not name:
            self.report({'ERROR'}, "Enter an animation name")
            return {'CANCELLED'}
        data = capture_animation(context, obj, s.anim_save_selected)
        if data is None:
            self.report({'ERROR'}, "No bone animation found to save")
            return {'CANCELLED'}
        write_with_backup(os.path.join(sub_dir(context, "anims"), name + ANIM_EXT), data)
        refresh_lists(context)
        self.report({'INFO'}, f"Saved '{name}': {len(data['channels'])} channels, "
                              f"{data['length']:.0f} frames")
        return {'FINISHED'}


class ANIMLIB_OT_load_anim(bpy.types.Operator):
    bl_idname = "animlib.load_anim"
    bl_label = "Load Animation"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_armature(context) is not None

    def execute(self, context):
        wm = context.window_manager
        s = wm.animlib
        name = selected_item(wm.animlib_anims, wm.animlib_anim_index)
        if not name:
            self.report({'ERROR'}, "Select an animation in the list")
            return {'CANCELLED'}
        data = read_json(os.path.join(sub_dir(context, "anims"), name + ANIM_EXT))
        obj = active_armature(context)
        done, failed, skipped = load_animation(
            context, obj, data, s.anim_at_current, s.anim_set_range,
            s.anim_load_selected, make_resolver(context, obj, s.match_names))
        msg = f"Loaded '{name}': {done} channels"
        if skipped:
            msg += f", {len(skipped)} bones not found ({', '.join(skipped[:4])}...)"
        if failed:
            msg += f", {failed} channels failed"
        self.report({'WARNING' if (skipped or failed) else 'INFO'}, msg)
        return {'FINISHED'}


class ANIMLIB_OT_delete(bpy.types.Operator):
    bl_idname = "animlib.delete"
    bl_label = "Delete"
    bl_description = "Delete the selected item (a .bak copy is kept in the folder)"
    kind: StringProperty()

    def _target(self, context):
        wm = context.window_manager
        if self.kind == "poses":
            name = selected_item(wm.animlib_poses, wm.animlib_pose_index)
            return name, POSE_EXT, "pose"
        name = selected_item(wm.animlib_anims, wm.animlib_anim_index)
        return name, ANIM_EXT, "animation"

    def invoke(self, context, event):
        name, ext, label = self._target(context)
        if not name:
            self.report({'ERROR'}, "Select an item in the list first")
            return {'CANCELLED'}
        if not context.window_manager.animlib.confirm_delete:
            return self.execute(context)
        try:
            return context.window_manager.invoke_confirm(
                self, event, title=f"Delete {label}?",
                message=f"Delete {label} '{name}'? This removes it from the library.",
                confirm_text="Delete")
        except TypeError:
            return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        name, ext, label = self._target(context)
        if not name:
            return {'CANCELLED'}
        path = os.path.join(sub_dir(context, self.kind), name + ext)
        if os.path.exists(path):
            os.replace(path, path + ".bak")  # recoverable: rename .bak back
        refresh_lists(context)
        self.report({'INFO'}, f"Deleted {label} '{name}'")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

class ANIMLIB_PT_pose(bpy.types.Panel):
    bl_label = "Poses"
    bl_idname = "ANIMLIB_PT_pose"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Animation Tools"

    def draw(self, context):
        wm = context.window_manager
        s = wm.animlib
        lay = self.layout
        row = lay.row(align=True)
        row.operator("animlib.refresh", icon='FILE_REFRESH')
        row.operator("animlib.open_folder", icon='FILE_FOLDER')

        lay.template_list("ANIMLIB_UL_items", "poses", wm, "animlib_poses",
                          wm, "animlib_pose_index", rows=4)

        box = lay.box()
        box.label(text="Delete")
        box.prop(s, "confirm_delete")
        op = box.operator("animlib.delete", text="Delete Selected Pose", icon='TRASH')
        op.kind = "poses"

        box = lay.box()
        box.label(text="Save")
        box.prop(s, "pose_name", text="")
        box.prop(s, "pose_save_selected")
        box.prop(s, "confirm_save")
        box.operator("animlib.save_pose", icon='EXPORT')

        box = lay.box()
        box.label(text="Apply")
        box.prop(s, "pose_apply_selected")
        box.prop(s, "match_names")
        box.prop(s, "pose_cross_rig")
        box.prop(s, "pose_rotation_only")
        box.prop(s, "pose_name_filter", text="Only", icon='FILTER')
        box.prop(s, "pose_blend")
        box.prop(s, "pose_keyframe")
        box.operator("animlib.apply_pose", icon='POSE_HLT')


class ANIMLIB_PT_anim(bpy.types.Panel):
    bl_label = "Animations"
    bl_idname = "ANIMLIB_PT_anim"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Animation Tools"

    def draw(self, context):
        wm = context.window_manager
        s = wm.animlib
        lay = self.layout
        lay.template_list("ANIMLIB_UL_items", "anims", wm, "animlib_anims",
                          wm, "animlib_anim_index", rows=4)

        box = lay.box()
        box.label(text="Delete")
        box.prop(s, "confirm_delete")
        op = box.operator("animlib.delete", text="Delete Selected Animation", icon='TRASH')
        op.kind = "anims"

        box = lay.box()
        box.label(text="Save (from active action)")
        box.prop(s, "anim_name", text="")
        box.prop(s, "anim_save_selected")
        box.prop(s, "confirm_save")
        box.operator("animlib.save_anim", icon='EXPORT')

        box = lay.box()
        box.label(text="Load")
        box.prop(s, "anim_at_current")
        box.prop(s, "anim_load_selected")
        box.prop(s, "match_names")
        box.prop(s, "anim_set_range")
        box.operator("animlib.load_anim", icon='IMPORT')


classes = (
    # proxy
    ProxySettings, OBJECT_OT_make_proxy, OBJECT_OT_disable_outline,
    VIEW3D_PT_proxy_maker,
    # library
    AnimLibPrefs, AnimLibItem, AnimLibSettings, ANIMLIB_UL_items,
    ANIMLIB_OT_refresh, ANIMLIB_OT_open_folder, ANIMLIB_OT_save_pose,
    ANIMLIB_OT_apply_pose, ANIMLIB_OT_save_anim, ANIMLIB_OT_load_anim,
    ANIMLIB_OT_delete, ANIMLIB_PT_pose, ANIMLIB_PT_anim,
)


def _initial_refresh():
    try:
        refresh_lists(bpy.context)
    except Exception:
        pass
    return None


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.proxy_settings = PointerProperty(type=ProxySettings)
    wm = bpy.types.WindowManager
    wm.animlib = PointerProperty(type=AnimLibSettings)
    wm.animlib_poses = CollectionProperty(type=AnimLibItem)
    wm.animlib_anims = CollectionProperty(type=AnimLibItem)
    wm.animlib_pose_index = IntProperty()
    wm.animlib_anim_index = IntProperty()
    bpy.app.timers.register(_initial_refresh, first_interval=0.5)


def unregister():
    wm = bpy.types.WindowManager
    del wm.animlib, wm.animlib_poses, wm.animlib_anims
    del wm.animlib_pose_index, wm.animlib_anim_index
    del bpy.types.Scene.proxy_settings
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
