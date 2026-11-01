bl_info = {
    "name": "Gacha Animation Kit",
    "author": "Yolu,Claude  (includes Animation Layers by Tal Hershkovich and "
              "Animation Auto Offset by Marek Zajac)",
    "version": (2, 0, 0),
    "blender": (4, 4, 0),
    "location": "View3D / Graph Editor / Dope Sheet > Sidebar > Gacha Animation Kit, Map Proxy",
    "description": "Animation proxy, pose and animation library (with flip), motion paths, "
                   "Map Proxy, Animation Layers 2.3.4 and Animation Auto Offset in ONE file",
    "category": "Animation",
}

# Everything is in this single file.
#   PART 1  Animation proxy, pose / animation library, motion paths
#   PART 2  Map Proxy
#   PART 3  Animation Layers 2.3.4 (GPL-2.0-or-later) and Animation Auto Offset
#           (GPL-3.0-or-later), stored as compressed text at the bottom and loaded from
#           memory when the add-on starts. Only their add-on preferences lookup was changed.

# id under which Blender knows this add-on (its preferences live here)
ADDON_ID = __package__ or __name__

# ============================================================================
# PART 1 - ANIMATION PROXY, POSE / ANIMATION LIBRARY, MOTION PATHS
# ============================================================================

import json
import os
import re
import shutil

import bpy
import bmesh
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       FloatProperty, IntProperty, PointerProperty,
                       StringProperty)
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
    bl_category = "Gacha Animation Kit"

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
    bl_idname = ADDON_ID

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
        path = context.preferences.addons[ADDON_ID].preferences.library_dir
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


def flip_bone_name(name):
    """hand_ik.L <-> hand_ik.R, Left <-> Right ... (Blender's own rules)"""
    try:
        return bpy.utils.flip_name(name)
    except Exception:
        return name


def flip_pose_entry(d):
    """Mirror one saved bone across the X axis (same maths as Blender's 'Paste Pose Flipped')."""
    f = dict(d)
    f["loc"] = [-d["loc"][0], d["loc"][1], d["loc"][2]]
    q = list(d["quat"]); q[2] = -q[2]; q[3] = -q[3]; f["quat"] = q
    e = list(d["euler"]); e[1] = -e[1]; e[2] = -e[2]; f["euler"] = e
    a = list(d["axis"]); a[2] = -a[2]; a[3] = -a[3]; f["axis"] = a
    if "rel_delta" in d:
        r = list(d["rel_delta"]); r[2] = -r[2]; r[3] = -r[3]; f["rel_delta"] = r
    return f


def flip_sign(rest_path, index):
    """-1 for the animation channels that change sign when mirrored across X."""
    prop = rest_path.lstrip(".")
    if prop == "location":
        return -1.0 if index == 0 else 1.0
    if prop in ("rotation_quaternion", "rotation_axis_angle"):
        return -1.0 if index in (2, 3) else 1.0
    if prop == "rotation_euler":
        return -1.0 if index in (1, 2) else 1.0
    return 1.0


def apply_pose(context, obj, data, selected_only, blend, keyframe,
               resolve, cross_rig, rotation_only, name_filter, flip=False):
    sel = selected_bone_names(context, obj) if selected_only else None
    frame = context.scene.frame_current
    filters = split_keywords(name_filter)
    applied, missing, fallback = 0, [], 0

    for name, d in data["bones"].items():
        if filters and not any(k in name.lower() for k in filters):
            continue
        pb = resolve(flip_bone_name(name) if flip else name)
        if pb is None:
            missing.append(name)
            continue
        if sel is not None and pb.name not in sel:
            continue
        if flip:
            d = flip_pose_entry(d)

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

def _edge_key(frame, value):
    return {"co": [frame, value], "interp": 'BEZIER', "easing": 'AUTO',
            "hlt": 'AUTO_CLAMPED', "hrt": 'AUTO_CLAMPED',
            "hl": [frame - 1.0, value], "hr": [frame + 1.0, value]}


def capture_animation(context, obj, selected_only, frame_range=None):
    sel = selected_bone_names(context, obj) if selected_only else None
    channels, modes = [], {}
    eps = 1e-4
    for fc in iter_fcurves(obj):
        m = PATH_RE.match(fc.data_path)
        if not m or not len(fc.keyframe_points):
            continue
        bone, rest = m.groups()
        if sel is not None and bone not in sel:
            continue
        keys = []
        for kp in fc.keyframe_points:
            if frame_range and not (frame_range[0] - eps <= kp.co[0] <= frame_range[1] + eps):
                continue
            keys.append({
                "co": [kp.co[0], kp.co[1]],
                "interp": kp.interpolation,
                "easing": kp.easing,
                "hlt": kp.handle_left_type,
                "hrt": kp.handle_right_type,
                "hl": [kp.handle_left[0], kp.handle_left[1]],
                "hr": [kp.handle_right[0], kp.handle_right[1]],
            })
        if frame_range:
            fs, fe = frame_range
            # keep the pose at the edges of the range even if there is no key there
            if not keys or keys[0]["co"][0] > fs + eps:
                keys.insert(0, _edge_key(fs, fc.evaluate(fs)))
            if keys[-1]["co"][0] < fe - eps:
                keys.append(_edge_key(fe, fc.evaluate(fe)))
        channels.append({"bone": bone, "path": rest,
                         "index": fc.array_index, "keys": keys})
        pb = obj.pose.bones.get(bone)
        if pb:
            modes[bone] = pb.rotation_mode

    if not channels:
        return None

    if frame_range:
        start, end = frame_range
    else:
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


def load_animation(context, obj, data, at_current, set_range, selected_only, resolve,
                   flip=False):
    sel = selected_bone_names(context, obj) if selected_only else None
    offset = context.scene.frame_current if at_current else data["original_start"]

    for name, mode in data.get("rotation_modes", {}).items():
        pb = resolve(flip_bone_name(name) if flip else name)
        if pb and pb.rotation_mode != mode:
            pb.rotation_mode = mode

    chans, skipped_bones = [], set()
    for ch in data["channels"]:
        pb = resolve(flip_bone_name(ch["bone"]) if flip else ch["bone"])
        if pb is None or (sel is not None and pb.name not in sel):
            if pb is None:
                skipped_bones.add(ch["bone"])
            continue
        full = 'pose.bones["%s"]%s' % (pb.name, ch["path"])
        sign = flip_sign(ch["path"], ch["index"]) if flip else 1.0
        chans.append((pb, ch, full, sign))

    if obj.animation_data is None:
        obj.animation_data_create()

    # Pass 1: make sure every fcurve exists (this also creates action/slot).
    existing = {(fc.data_path, fc.array_index) for fc in iter_fcurves(obj)}
    for pb, ch, full, sign in chans:
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
    for pb, ch, full, sign in chans:
        fc = fmap.get((full, ch["index"]))
        if fc is None:
            failed += 1
            continue
        for k in ch["keys"]:
            kp = fc.keyframe_points.insert(k["co"][0] + offset, k["co"][1] * sign,
                                           options={'FAST'})
            kp.interpolation = k["interp"]
            kp.easing = k.get("easing", 'AUTO')
            kp.handle_left_type = k["hlt"]
            kp.handle_right_type = k["hrt"]
            kp.handle_left = (k["hl"][0] + offset, k["hl"][1] * sign)
            kp.handle_right = (k["hr"][0] + offset, k["hr"][1] * sign)
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

# ----------------------------------------------------------------------------
# Motion path live update (timer based, experimental)
# ----------------------------------------------------------------------------

_live = {"running": False, "sig": None, "errors": 0}


def _find_view3d_override(ctx):
    for win in ctx.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        return {"window": win, "area": area, "region": region}
    return None


def _pose_signature(obj, sel):
    sig = [tuple(sorted(sel))]
    for fc in iter_fcurves(obj):
        m = PATH_RE.match(fc.data_path)
        if m and m.group(1) in sel:
            for kp in fc.keyframe_points:
                sig.append((round(kp.co[0], 3), round(kp.co[1], 4)))
    return hash(tuple(sig))


def _live_tick():
    ctx = bpy.context
    s = getattr(ctx.window_manager, "animlib", None)
    if s is None or not s.paths_live:
        _live["running"] = False
        return None
    obj = ctx.active_object
    if obj and obj.type == 'ARMATURE' and obj.mode == 'POSE':
        try:
            sel = selected_bone_names(ctx, obj)
            if sel:
                sig = _pose_signature(obj, sel)
                if sig != _live["sig"]:
                    _live["sig"] = sig
                    ov = _find_view3d_override(ctx)
                    if ov:
                        with ctx.temp_override(**ov):
                            need = any(pb.motion_path is None
                                       for pb in obj.pose.bones if pb.name in sel)
                            if need:
                                bpy.ops.pose.paths_calculate()
                            else:
                                bpy.ops.pose.paths_update()
            _live["errors"] = 0
        except Exception:
            _live["errors"] += 1
            if _live["errors"] > 5:  # give up instead of spamming
                s.paths_live = False
                _live["running"] = False
                return None
    return max(0.05, s.paths_interval)


def _paths_live_update(self, context):
    if self.paths_live and not bpy.app.timers.is_registered(_live_tick):
        _live["running"] = True
        _live["sig"] = None
        _live["errors"] = 0
        bpy.app.timers.register(_live_tick, first_interval=0.1)


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
    pose_flip: BoolProperty(
        name="Flip Left / Right", default=False,
        description="Mirror the pose onto the opposite side: a pose saved on hand_ik.L is "
                    "applied to hand_ik.R (and the other way). Select the target side's "
                    "bones if 'Apply To Selected Bones Only' is on")
    anim_flip: BoolProperty(
        name="Flip Left / Right", default=False,
        description="Mirror the animation onto the opposite side: keys saved on the "
                    "left arm are loaded onto the right arm")
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
    anim_range_mode: EnumProperty(
        name="Save Range",
        items=[('ACTION', "Whole Action", "Save every keyframe"),
               ('SCENE', "Timeline Range", "Only the frames inside the scene "
                                            "(or preview) range"),
               ('CUSTOM', "Custom Frames", "Pick the frames below, e.g. only the jump")],
        default='ACTION')
    anim_range_start: IntProperty(name="Start", default=1)
    anim_range_end: IntProperty(name="End", default=30)

    paths_live: BoolProperty(
        name="Live Update", default=False, update=_paths_live_update,
        description="Experimental: refresh the motion paths automatically while you key "
                    "or edit the selected bones (Pose Mode)")
    paths_interval: FloatProperty(name="Check Every (s)", default=0.25, min=0.05,
                                  max=2.0)

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
            s.pose_rotation_only, s.pose_name_filter, s.pose_flip)
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
        frame_range = get_save_range(context, s)
        if frame_range and frame_range[1] <= frame_range[0]:
            self.report({'ERROR'}, "The end frame must be after the start frame")
            return {'CANCELLED'}
        data = capture_animation(context, obj, s.anim_save_selected, frame_range)
        if data is None:
            self.report({'ERROR'}, "No bone animation found to save")
            return {'CANCELLED'}
        write_with_backup(os.path.join(sub_dir(context, "anims"), name + ANIM_EXT), data)
        refresh_lists(context)
        self.report({'INFO'}, f"Saved '{name}': {len(data['channels'])} channels, "
                              f"{data['length']:.0f} frames"
                              + (f" (frames {frame_range[0]}-{frame_range[1]})"
                                 if frame_range else ""))
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
            s.anim_load_selected, make_resolver(context, obj, s.match_names),
            s.anim_flip)
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

# ----------------------------------------------------------------------------
# Save-range helpers
# ----------------------------------------------------------------------------

def get_save_range(context, s):
    sc = context.scene
    if s.anim_range_mode == 'SCENE':
        if sc.use_preview_range:
            return sc.frame_preview_start, sc.frame_preview_end
        return sc.frame_start, sc.frame_end
    if s.anim_range_mode == 'CUSTOM':
        return (min(s.anim_range_start, s.anim_range_end),
                max(s.anim_range_start, s.anim_range_end))
    return None


class ANIMLIB_OT_range_start_here(bpy.types.Operator):
    bl_idname = "animlib.range_start_here"
    bl_label = "Start = Current Frame"

    def execute(self, context):
        s = context.window_manager.animlib
        s.anim_range_start = context.scene.frame_current
        s.anim_range_mode = 'CUSTOM'
        return {'FINISHED'}


class ANIMLIB_OT_range_end_here(bpy.types.Operator):
    bl_idname = "animlib.range_end_here"
    bl_label = "End = Current Frame"

    def execute(self, context):
        s = context.window_manager.animlib
        s.anim_range_end = context.scene.frame_current
        s.anim_range_mode = 'CUSTOM'
        return {'FINISHED'}


class ANIMLIB_OT_range_from_keys(bpy.types.Operator):
    bl_idname = "animlib.range_from_keys"
    bl_label = "Range From Selected Keys"
    bl_description = ("Use the first and last keyframe you have selected in the "
                      "Dope Sheet / Graph Editor as the save range")

    @classmethod
    def poll(cls, context):
        obj = active_armature(context)
        return obj is not None and obj.animation_data and obj.animation_data.action

    def execute(self, context):
        frames = [kp.co[0] for fc in iter_fcurves(active_armature(context))
                  for kp in fc.keyframe_points if kp.select_control_point]
        if not frames:
            self.report({'ERROR'}, "No keyframes selected")
            return {'CANCELLED'}
        s = context.window_manager.animlib
        s.anim_range_start = int(round(min(frames)))
        s.anim_range_end = int(round(max(frames)))
        s.anim_range_mode = 'CUSTOM'
        self.report({'INFO'}, f"Range {s.anim_range_start} - {s.anim_range_end}")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Motion paths
# ----------------------------------------------------------------------------

class _PathOp:
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'ARMATURE' and obj.mode == 'POSE'


class ANIMLIB_OT_paths_calc(_PathOp, bpy.types.Operator):
    bl_idname = "animlib.paths_calc"
    bl_label = "Calculate"
    bl_description = "Calculate motion paths for the selected bones (scene frame range)"

    def execute(self, context):
        try:
            bpy.ops.pose.paths_calculate()
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        return {'FINISHED'}


class ANIMLIB_OT_paths_update(_PathOp, bpy.types.Operator):
    bl_idname = "animlib.paths_update"
    bl_label = "Update"
    bl_description = "Refresh the existing motion paths"

    def execute(self, context):
        try:
            bpy.ops.pose.paths_update()
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        return {'FINISHED'}


class ANIMLIB_OT_paths_clear(_PathOp, bpy.types.Operator):
    bl_idname = "animlib.paths_clear"
    bl_label = "Clear"
    bl_description = "Remove the motion paths of the selected bones"

    def execute(self, context):
        try:
            bpy.ops.pose.paths_clear()
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        return {'FINISHED'}


class ANIMLIB_PT_pose(bpy.types.Panel):
    bl_label = "Poses"
    bl_idname = "ANIMLIB_PT_pose"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Gacha Animation Kit"

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
        box.prop(s, "pose_flip", icon='MOD_MIRROR')
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
    bl_category = "Gacha Animation Kit"

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
        box.prop(s, "anim_range_mode", text="")
        if s.anim_range_mode == 'CUSTOM':
            row = box.row(align=True)
            row.prop(s, "anim_range_start")
            row.prop(s, "anim_range_end")
            row = box.row(align=True)
            row.operator("animlib.range_start_here", text="Start = Now")
            row.operator("animlib.range_end_here", text="End = Now")
            box.operator("animlib.range_from_keys", icon='KEYFRAME')
        box.prop(s, "anim_save_selected")
        box.prop(s, "confirm_save")
        box.operator("animlib.save_anim", icon='EXPORT')

        box = lay.box()
        box.label(text="Load")
        box.prop(s, "anim_at_current")
        box.prop(s, "anim_flip", icon='MOD_MIRROR')
        box.prop(s, "anim_load_selected")
        box.prop(s, "match_names")
        box.prop(s, "anim_set_range")
        box.operator("animlib.load_anim", icon='IMPORT')


class ANIMLIB_PT_paths(bpy.types.Panel):
    bl_label = "Motion Paths"
    bl_idname = "ANIMLIB_PT_paths"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Gacha Animation Kit"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        s = context.window_manager.animlib
        col = self.layout.column()
        row = col.row(align=True)
        row.operator("animlib.paths_calc", icon='ANIM')
        row.operator("animlib.paths_update", icon='FILE_REFRESH')
        col.operator("animlib.paths_clear", icon='X')
        col.separator()
        col.prop(s, "paths_live", toggle=True, icon='TIME')
        if s.paths_live:
            col.prop(s, "paths_interval")
        col.label(text="Uses the scene frame range")


classes = (
    # proxy
    ProxySettings, OBJECT_OT_make_proxy, OBJECT_OT_disable_outline,
    VIEW3D_PT_proxy_maker,
    # library (AnimLibPrefs is registered by the bundle as merged preferences)
    AnimLibItem, AnimLibSettings, ANIMLIB_UL_items,
    ANIMLIB_OT_refresh, ANIMLIB_OT_open_folder, ANIMLIB_OT_save_pose,
    ANIMLIB_OT_apply_pose, ANIMLIB_OT_save_anim, ANIMLIB_OT_load_anim,
    ANIMLIB_OT_delete, ANIMLIB_PT_pose, ANIMLIB_PT_anim,
    # save range + motion paths
    ANIMLIB_OT_range_start_here, ANIMLIB_OT_range_end_here,
    ANIMLIB_OT_range_from_keys,
    ANIMLIB_OT_paths_calc, ANIMLIB_OT_paths_update, ANIMLIB_OT_paths_clear,
    ANIMLIB_PT_paths,
)


def _initial_refresh():
    try:
        refresh_lists(bpy.context)
    except Exception:
        pass
    return None


def _tools_register():
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


def _tools_unregister():
    wm = bpy.types.WindowManager
    del wm.animlib, wm.animlib_poses, wm.animlib_anims
    del wm.animlib_pose_index, wm.animlib_anim_index
    del bpy.types.Scene.proxy_settings
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


# ============================================================================
# PART 2 - MAP PROXY
# ============================================================================

import json
import re

import bmesh
import bpy
from mathutils import Vector
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)


# ----------------------------------------------------------------------------
# Pure logic (no bpy): split objects into spatially compact batches
# ----------------------------------------------------------------------------

def _spread(n):
    n &= 0x3FF
    n = (n | (n << 16)) & 0x030000FF
    n = (n | (n << 8)) & 0x0300F00F
    n = (n | (n << 4)) & 0x030C30C3
    n = (n | (n << 2)) & 0x09249249
    return n


def morton3(x, y, z):
    return _spread(x) | (_spread(y) << 1) | (_spread(z) << 2)


def build_chunks(items, size, mode):
    """items: list of (name, (x, y, z), material_name)."""
    size = max(2, size)
    if not items:
        return []
    lo = [min(it[1][i] for it in items) for i in range(3)]
    hi = [max(it[1][i] for it in items) for i in range(3)]
    span = [(hi[i] - lo[i]) or 1.0 for i in range(3)]

    def key(it):
        return morton3(*[int((it[1][i] - lo[i]) / span[i] * 1023) for i in range(3)])

    by_mat = {}
    for it in items:
        by_mat.setdefault(it[2] if mode == 'MATERIAL' else "", []).append(it)
    chunks = []
    for k in sorted(by_mat):
        g = sorted(by_mat[k], key=key)
        for i in range(0, len(g), size):
            chunks.append(g[i:i + size])
    return chunks


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

class MapProxySettings(bpy.types.PropertyGroup):
    batch_size: IntProperty(
        name="Objects Per Chunk", default=100, min=2, max=2000,
        description="Objects are processed and joined in groups of this size")
    ratio: FloatProperty(name="Decimate Ratio", default=0.2, min=0.01, max=1.0)
    merge_dist: FloatProperty(name="Merge Distance", default=0.0001, min=0.0,
                              max=1.0, precision=5)
    min_verts: IntProperty(
        name="Skip Decimate Below", default=30, min=0,
        description="Objects with fewer vertices than this are only welded, not decimated")
    group_mode: EnumProperty(
        name="Group By",
        items=[('SPATIAL', "Position", "Nearby objects go in the same chunk"),
               ('MATERIAL', "Material + Position", "Same material first, then position")],
        default='SPATIAL')
    hide_others: BoolProperty(
        name="Hide Everything Not Selected", default=True,
        description="Like Shift+H. Restorable with 'Show Originals'")
    hide_originals: BoolProperty(
        name="Hide Selected Originals", default=True,
        description="Hide the originals you selected once their proxy exists")
    disable_lights: BoolProperty(
        name="Disable Lights On Create", default=True,
        description="Also hides every light when creating the proxy. "
                    "You can use the Lights Off / Lights On buttons any time")
    exclude_names: StringProperty(
        name="Skip Entirely", default="",
        description="Comma separated name parts (e.g. Tree,Plant). Matching objects are "
                    "left out of the proxy: they stay as original and visible")
    keep_names: StringProperty(
        name="Keep Detail: Objects", default="",
        description="Comma separated name parts (e.g. Stair,Railing). These objects ARE "
                    "in the proxy but only welded, never decimated")
    keep_materials: StringProperty(
        name="Keep Detail: Materials", default="",
        description="Comma separated material name parts. Faces using them (and their "
                    "vertices) are never decimated")
    keep_vgroup: StringProperty(
        name="Keep Detail: Vertex Group", default="KeepDetail",
        description="Vertices in this vertex group are never decimated. Make it in "
                    "Edit Mode with the Add Selected button")
    simplify_materials: BoolProperty(
        name="Simplify Materials", default=False,
        description="Proxy uses a plain Principled BSDF with the diffuse texture")
    diffuse_suffix: StringProperty(name="Diffuse Suffix", default="_D")
    solid_texture: BoolProperty(name="Solid + Texture Viewport", default=False)
    push_undo: BoolProperty(
        name="Allow Ctrl+Z After Creating", default=True,
        description="Save one undo step when the proxy is finished so Ctrl+Z works "
                    "normally afterwards. Turn off if it makes finishing slow")
    restore_margin: FloatProperty(
        name="Area Margin", default=0.3, min=0.0, precision=2, unit='LENGTH',
        description="Extra space around the selected vertices when restoring the "
                    "original detail of an area")
    separate_scene: BoolProperty(
        name="Proxy In Separate Scene", default=True,
        description="Put the proxy chunks in their own scene. The originals stay in your "
                    "scene untouched, and the proxy scene runs fast because the heavy "
                    "originals are not in it")
    scene_name: StringProperty(name="Scene", default="Map Proxy")
    switch_scene: BoolProperty(name="Switch To Proxy Scene After", default=True)
    result_name: StringProperty(name="Result Name", default="MapProxy")
    collection_name: StringProperty(name="Collection", default="Map Proxy")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def proxy_collection(context, create=True):
    name = context.scene.mapproxy.collection_name or "Map Proxy"
    col = bpy.data.collections.get(name)
    if col is None and create:
        col = bpy.data.collections.new(name)
        context.scene.collection.children.link(col)
    return col


HIDDEN_KEY = "mapproxy_hidden"
LIGHT_KEY = "mapproxy_lights"


def mp_split_keywords(text):
    return [k.strip().lower() for k in text.split(",") if k.strip()]


def copy_settings(src, dst):
    for prop in src.bl_rna.properties:
        if prop.identifier != "rna_type" and not prop.is_readonly:
            try:
                setattr(dst, prop.identifier, getattr(src, prop.identifier))
            except (AttributeError, TypeError, ValueError):
                pass


def get_proxy_scene(context, s):
    src = context.scene
    name = s.scene_name or "Map Proxy"
    sc = bpy.data.scenes.get(name)
    if sc is None:
        sc = bpy.data.scenes.new(name)
        sc.world = src.world
        sc.render.fps = src.render.fps
        sc.render.fps_base = src.render.fps_base
        sc.unit_settings.system = src.unit_settings.system
        sc.unit_settings.scale_length = src.unit_settings.scale_length
        sc.frame_start, sc.frame_end = src.frame_start, src.frame_end
        sc["mapproxy_source_scene"] = src.name
    copy_settings(src.mapproxy, sc.mapproxy)   # always run with the current settings
    return sc


def child_collection(parent, name):
    col = parent.children.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        parent.children.link(col)
    return col


def scene_collections(scene):
    return [scene.collection] + list(scene.collection.children_recursive)


def home_collection(scene, obj):
    for col in scene_collections(scene):
        if obj.name in col.objects:
            return col
    return scene.collection


def get_list(scene, key):
    try:
        return set(json.loads(scene.get(key, "[]")))
    except ValueError:
        return set()


def set_list(scene, key, names):
    scene[key] = json.dumps(sorted(names))


def get_hidden(scene):
    return get_list(scene, HIDDEN_KEY)


def hide_objects(context, objs, key=HIDDEN_KEY):
    """Hide (eye icon) and remember what we hid so it can be restored."""
    names = []
    for o in objs:
        if not o.hide_get():
            o.hide_set(True)
            names.append(o.name)
    set_list(context.scene, key, get_list(context.scene, key) | set(names))
    return len(names)


def _reveal_collection(lc, col, is_root=True):
    if lc.collection == col:
        if not is_root:
            lc.exclude = False
            lc.hide_viewport = False
        return True
    for child in lc.children:
        if _reveal_collection(child, col, False):
            if not is_root:
                lc.exclude = False
                lc.hide_viewport = False
            return True
    return False


def make_visible(context, o):
    """Unhide an object even if the monitor icon, its parent or its collection hides it."""
    chain = []
    p = o
    while p is not None:
        chain.append(p)
        p = p.parent
    for obj in chain:
        obj.hide_viewport = False
        if obj.name in context.view_layer.objects:
            obj.hide_set(False)
    if not o.visible_get():
        for col in o.users_collection:
            try:
                _reveal_collection(context.view_layer.layer_collection, col)
            except (RuntimeError, AttributeError):
                pass
        if o.name in context.view_layer.objects:
            o.hide_set(False)
    return o.visible_get()


def is_kept(o, s):
    """Objects the user does not want in the proxy."""
    if o.get("mapproxy_keep"):
        return True
    flt = mp_split_keywords(s.exclude_names)
    return bool(flt) and any(k in o.name.lower() for k in flt)


def mp_set_solid_texture(context):
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


def selection_items(context, col, s):
    items, kept = [], 0
    for o in context.selected_objects:
        if o.type != 'MESH' or not o.visible_get() or not len(o.data.polygons):
            continue
        if "mapproxy_sources" in o or (col and o.name in col.objects):
            continue
        if is_kept(o, s):
            kept += 1
            continue
        mats = o.data.materials
        mat = mats[0].name if len(mats) and mats[0] else ""
        items.append((o.name, tuple(o.matrix_world.translation), mat))
    return items, kept


# ---- material simplification (diffuse texture -> Principled BSDF) ----

def mp_collect_images(tree, found, visited):
    if tree is None or tree in visited:
        return
    visited.add(tree)
    for node in tree.nodes:
        if node.type == 'TEX_IMAGE' and node.image:
            found.append((node, node.image))
        elif node.type == 'GROUP':
            mp_collect_images(node.node_tree, found, visited)


def mp_find_diffuse_image(mat, suffix):
    found = []
    mp_collect_images(mat.node_tree, found, set())
    pat = re.compile(re.escape(suffix.lower()) + r"(\.[a-z0-9]+)?(\.\d+)?$")
    for node, img in found:
        if pat.search(img.name.lower()):
            return img
    for node, img in found:
        if "diffuse" in (node.label + node.name).lower():
            return img
    return None


def simple_material(orig, suffix):
    img = mp_find_diffuse_image(orig, suffix) if orig.use_nodes else None
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
    else:
        bsdf.inputs["Base Color"].default_value = orig.diffuse_color
    return new


# ----------------------------------------------------------------------------
# One chunk: copy -> weld -> decimate -> join (originals untouched)
# ----------------------------------------------------------------------------

def keep_detail(o, s):
    """-> (None, None) normal | ('ALL', None) weld only | ('PARTIAL', protected vertex set)"""
    me = o.data
    names = mp_split_keywords(s.keep_names)
    if names and any(k in o.name.lower() for k in names):
        return 'ALL', None
    prot = set()
    vg = o.vertex_groups.get(s.keep_vgroup) if s.keep_vgroup else None
    if vg is not None:
        idx = vg.index
        for v in me.vertices:
            for g in v.groups:
                if g.group == idx and g.weight > 0.0:
                    prot.add(v.index)
                    break
    mats = mp_split_keywords(s.keep_materials)
    if mats:
        slots = {i for i, m in enumerate(me.materials)
                 if m and any(k in m.name.lower() for k in mats)}
        if slots:
            for p in me.polygons:
                if p.material_index in slots:
                    prot.update(p.vertices)
    if not prot:
        return None, None
    if len(prot) >= len(me.vertices):
        return 'ALL', None
    return 'PARTIAL', prot


def process_chunk(context, chunk, s, number, col, mat_cache, target=None):
    originals = [bpy.data.objects.get(it[0]) for it in chunk]
    originals = [o for o in originals if o is not None and o.type == 'MESH']
    if not originals:
        return None

    # 1. temporary copies that share the original mesh data (no big copy)
    temps, temp_meshes = [], []
    before = 0
    for o in originals:
        before += len(o.data.vertices)
        mode, prot = keep_detail(o, s)
        if mode == 'PARTIAL':
            # private copy so the protection group never touches the original mesh
            me2 = o.data.copy()
            t = o.copy()
            t.data = me2
            for m in list(t.modifiers):
                t.modifiers.remove(m)
            t.parent = None
            t.matrix_world = o.matrix_world.copy()
            col.objects.link(t)
            vg = t.vertex_groups.new(name="_MPD")
            vg.add([i for i in range(len(me2.vertices)) if i not in prot], 1.0, 'REPLACE')
            temp_meshes.append(me2)
        else:
            t = bpy.data.objects.new(o.name + "_tmp", o.data)
            t.matrix_world = o.matrix_world.copy()
            col.objects.link(t)
        w = t.modifiers.new("Weld", 'WELD')
        w.merge_threshold = s.merge_dist
        if mode != 'ALL' and len(o.data.vertices) >= s.min_verts:
            d = t.modifiers.new("Decimate", 'DECIMATE')
            d.ratio = s.ratio
            if mode == 'PARTIAL':          # weight 0 = vertex is never collapsed
                d.vertex_group = "_MPD"
        temps.append(t)

    # 2. bake the modifiers into brand new small meshes
    context.view_layer.update()
    dg = context.evaluated_depsgraph_get()
    new_objs = []
    for o, t in zip(originals, temps):
        me = bpy.data.meshes.new_from_object(t.evaluated_get(dg))
        no = bpy.data.objects.new(o.name + "_proxy", me)
        no.matrix_world = t.matrix_world.copy()
        col.objects.link(no)
        new_objs.append(no)
    for t in temps:
        bpy.data.objects.remove(t)
    for me in temp_meshes:
        if me.users == 0:
            bpy.data.meshes.remove(me)

    # 3. join the small copies into one object
    context.view_layer.update()
    meshes = [no.data for no in new_objs]
    if len(new_objs) > 1:
        bpy.ops.object.select_all(action='DESELECT')
        for no in new_objs:
            no.select_set(True)
        context.view_layer.objects.active = new_objs[0]
        bpy.ops.object.join()
    result = context.view_layer.objects.active if len(new_objs) > 1 else new_objs[0]
    for me in meshes:
        if me.users == 0:
            bpy.data.meshes.remove(me)

    result["mapproxy_sources"] = json.dumps([o.name for o in originals])
    result.name = f"{s.result_name}_{number:03d}"
    result.data.name = result.name
    context.view_layer.objects.active = result
    bpy.ops.object.select_all(action='DESELECT')
    result.select_set(True)
    bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='BOUNDS')

    # 4. simple materials
    if s.simplify_materials:
        for slot in result.material_slots:
            orig = slot.material
            if orig is None:
                continue
            if orig.name not in mat_cache:
                mat_cache[orig.name] = simple_material(orig, s.diffuse_suffix)
            slot.material = mat_cache[orig.name]

    if target is not None and target is not col:   # move into the proxy scene
        for c in list(result.users_collection):
            c.objects.unlink(result)
        target.objects.link(result)

    return originals, before, len(result.data.vertices)


# ----------------------------------------------------------------------------
# Operators
# ----------------------------------------------------------------------------

class MAPPROXY_OT_run(bpy.types.Operator):
    """Ask for confirmation, then build the proxy"""
    bl_idname = "mapproxy.run"
    bl_label = "Create Map Proxy From Selection"

    _count = 0
    _verts = 0
    _chunks = 0
    _kept = 0

    def invoke(self, context, event):
        s = context.scene.mapproxy
        items, kept = selection_items(
            context, proxy_collection(context, create=False), s)
        MAPPROXY_OT_run._kept = kept
        if not items:
            self.report({'ERROR'}, "Select the map objects you want first "
                                   "(e.g. box select a region)")
            return {'CANCELLED'}
        MAPPROXY_OT_run._count = len(items)
        MAPPROXY_OT_run._verts = sum(len(bpy.data.objects[n].data.vertices)
                                     for n, _, _ in items)
        MAPPROXY_OT_run._chunks = len(build_chunks(items, s.batch_size, s.group_mode))
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        col = self.layout.column()
        col.label(text=f"{self._count} objects, {self._verts:,} vertices "
                       f"-> {self._chunks} chunks")
        if self._kept:
            col.label(text=f"{self._kept} selected objects are kept as original.")
        col.label(text="Originals are not changed, only hidden.")

    def execute(self, context):
        bpy.ops.mapproxy.process('INVOKE_DEFAULT')
        return {'FINISHED'}


class MAPPROXY_OT_process(bpy.types.Operator):
    bl_idname = "mapproxy.process"
    bl_label = "Building Map Proxy..."

    _timer = None
    _chunks = None
    _i = 0
    _number = 0
    _col = None
    _target = None
    _proxy_scene = None
    _src_scene = None
    _start_number = 0
    _mat_cache = None
    _done_objs = 0
    _v_before = 0
    _v_after = 0
    _failed = 0

    def invoke(self, context, event):
        s = context.scene.mapproxy
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        if s.separate_scene:
            self._src_scene = context.scene
            self._proxy_scene = get_proxy_scene(context, s)
            self._target = child_collection(self._proxy_scene.collection,
                                            s.collection_name or "Map Proxy")
            self._col = bpy.data.collections.new("_MapProxyTemp")
            self._proxy_scene.collection.children.link(self._col)
        else:
            self._proxy_scene = None
            self._target = self._col = proxy_collection(context)
        items, _ = selection_items(context, self._target, s)
        if not items:
            self._remove_temp()
            return {'CANCELLED'}
        selected = set(it[0] for it in items)

        # Same-scene mode only: Shift+H style hiding + lights (recorded to restore)
        if not s.separate_scene:
            if s.disable_lights:
                hide_objects(context, [o for o in context.view_layer.objects
                                       if o.type == 'LIGHT'], LIGHT_KEY)
            if s.hide_others:
                hide_objects(context, [o for o in context.view_layer.objects
                                       if o.name not in selected
                                       and o.name not in self._target.objects
                                       and not is_kept(o, s)])

        self._chunks = build_chunks(items, s.batch_size, s.group_mode)
        self._i = 0
        self._number = sum(1 for o in self._target.objects
                           if o.name.startswith(s.result_name + "_")) + 1
        self._start_number = self._number
        self._mat_cache = {}
        self._done_objs = self._v_before = self._v_after = self._failed = 0

        if self._proxy_scene is not None:
            # work inside the small proxy scene: every update/operator is much faster
            # than in the huge original scene
            context.window.scene = self._proxy_scene

        wm = context.window_manager
        wm.progress_begin(0, len(self._chunks))
        self._timer = wm.event_timer_add(0.05, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _remove_temp(self):
        if self._proxy_scene is not None and self._col is not None:
            if self._col.name in bpy.data.collections:
                for o in list(self._col.objects):
                    bpy.data.objects.remove(o)
                bpy.data.collections.remove(self._col)
            self._col = None

    def _finish(self, context, cancelled):
        wm = context.window_manager
        wm.event_timer_remove(self._timer)
        wm.progress_end()
        context.workspace.status_text_set(None)
        s = context.scene.mapproxy
        self._remove_temp()
        if s.solid_texture:
            mp_set_solid_texture(context)
        msg = (f"{self._done_objs} objects -> {self._i - self._failed} chunks, "
               f"{self._v_before:,} -> {self._v_after:,} vertices")
        if self._failed:
            msg += f" ({self._failed} chunks failed)"
        if s.push_undo:
            try:
                bpy.ops.ed.undo_push(message="Create Map Proxy")
            except RuntimeError:
                pass
        self.report({'WARNING' if cancelled else 'INFO'},
                    ("Stopped. " if cancelled else "") + msg)
        if self._proxy_scene is not None:
            made_any = self._number > self._start_number
            context.window.scene = (self._proxy_scene if (s.switch_scene and made_any)
                                    else self._src_scene)
        return {'CANCELLED' if cancelled else 'FINISHED'}

    def modal(self, context, event):
        if event.type == 'ESC':
            return self._finish(context, True)
        if event.type != 'TIMER':
            return {'RUNNING_MODAL'}
        if self._i >= len(self._chunks):
            return self._finish(context, False)

        s = context.scene.mapproxy
        try:
            res = process_chunk(context, self._chunks[self._i], s, self._number,
                                self._col, self._mat_cache,
                                self._target if self._proxy_scene else None)
            if res:
                originals, before, after = res
                self._done_objs += len(originals)
                self._v_before += before
                self._v_after += after
                self._number += 1
                if s.hide_originals and self._proxy_scene is None:
                    hide_objects(context, originals)
        except RuntimeError:
            self._failed += 1
        self._i += 1

        context.window_manager.progress_update(self._i)
        context.workspace.status_text_set(
            f"Map Proxy: chunk {self._i}/{len(self._chunks)}   (Esc to stop)")
        return {'RUNNING_MODAL'}


class MAPPROXY_OT_show_originals(bpy.types.Operator):
    bl_idname = "mapproxy.show_originals"
    bl_label = "Show Originals"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Unhide everything the addon hid, and hide the proxy chunks"

    def execute(self, context):
        names = get_hidden(context.scene)
        n = 0
        for name in names:
            o = bpy.data.objects.get(name)
            if o and o.name in context.view_layer.objects:
                o.hide_set(False)
                n += 1
        col = proxy_collection(context, create=False)
        if col:
            for o in col.objects:
                if o.name in context.view_layer.objects:
                    o.hide_set(True)
        self.report({'INFO'}, f"Showed {n} objects, proxy hidden")
        return {'FINISHED'}


class MAPPROXY_OT_show_proxy(bpy.types.Operator):
    bl_idname = "mapproxy.show_proxy"
    bl_label = "Show Proxy"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Hide the originals again and show the proxy chunks"

    def execute(self, context):
        for name in get_hidden(context.scene):
            o = bpy.data.objects.get(name)
            if o and o.name in context.view_layer.objects:
                o.hide_set(True)
        col = proxy_collection(context, create=False)
        if col:
            for o in col.objects:
                if not o.get("mapproxy_reverted") and o.name in context.view_layer.objects:
                    o.hide_set(False)
        return {'FINISHED'}


class MAPPROXY_OT_delete_proxy(bpy.types.Operator):
    bl_idname = "mapproxy.delete_proxy"
    bl_label = "Delete Proxy Chunks"
    bl_description = "Delete all proxy chunks (in any scene). Originals are not touched"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        n = 0
        for o in [o for o in bpy.data.objects
                  if "mapproxy_sources" in o or "mapproxy_part_of" in o]:
            me = o.data if o.type == 'MESH' else None
            bpy.data.objects.remove(o)
            if me and me.users == 0:
                bpy.data.meshes.remove(me)
            n += 1
        for me in [m for m in bpy.data.meshes if m.get("mapproxy_backup")]:
            me.use_fake_user = False
            if me.users == 0:
                bpy.data.meshes.remove(me)
        for m in [m for m in bpy.data.materials
                  if m.name.endswith("_Proxy") and m.users == 0]:
            bpy.data.materials.remove(m)
        self.report({'INFO'}, f"Deleted {n} proxy objects")
        return {'FINISHED'}


class MAPPROXY_OT_lights_off(bpy.types.Operator):
    bl_idname = "mapproxy.lights_off"
    bl_label = "Lights Off"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Hide every light object in the outliner/viewport"

    def execute(self, context):
        lights = [o for o in context.view_layer.objects if o.type == 'LIGHT']
        n = hide_objects(context, lights, LIGHT_KEY)
        self.report({'INFO'}, f"Disabled {n} lights")
        return {'FINISHED'}


class MAPPROXY_OT_lights_on(bpy.types.Operator):
    bl_idname = "mapproxy.lights_on"
    bl_label = "Lights On"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Show the lights that 'Lights Off' hid"

    def execute(self, context):
        names = get_list(context.scene, LIGHT_KEY)
        n = 0
        for name in names:
            o = bpy.data.objects.get(name)
            if o and o.name in context.view_layer.objects:
                o.hide_set(False)
                n += 1
        set_list(context.scene, LIGHT_KEY, [])
        self.report({'INFO'}, f"Enabled {n} lights")
        return {'FINISHED'}


class MAPPROXY_OT_keep(bpy.types.Operator):
    bl_idname = "mapproxy.keep"
    bl_label = "Keep Original"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = ("Mark selected objects so they are never decimated: "
                      "they stay original and visible")

    def execute(self, context):
        n = 0
        for o in context.selected_objects:
            o["mapproxy_keep"] = True
            n += 1
        self.report({'INFO'}, f"{n} objects will stay original")
        return {'FINISHED'}


class MAPPROXY_OT_unkeep(bpy.types.Operator):
    bl_idname = "mapproxy.unkeep"
    bl_label = "Allow Proxy"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Remove the Keep Original mark from the selected objects"

    def execute(self, context):
        n = 0
        for o in context.selected_objects:
            if "mapproxy_keep" in o:
                del o["mapproxy_keep"]
                n += 1
        self.report({'INFO'}, f"{n} objects can be proxied again")
        return {'FINISHED'}


class MAPPROXY_OT_revert(bpy.types.Operator):
    bl_idname = "mapproxy.revert"
    bl_label = "Revert Selected Chunks"
    bl_description = ("Select proxy chunk(s) you don't like: hides them and brings back "
                      "their original objects into this scene (marked Keep Original). "
                      "Works in the proxy scene too. 'Use Proxy Again' undoes it")
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        scene = context.scene
        chunks = [o for o in context.selected_objects if "mapproxy_sources" in o]
        if not chunks:
            self.report({'ERROR'}, "Select one or more proxy chunks (MapProxy_xxx) "
                                   "first")
            return {'CANCELLED'}

        hidden = get_hidden(scene)
        to_show, missing = [], 0
        for chunk in chunks:
            home = home_collection(scene, chunk)
            try:
                names = json.loads(chunk["mapproxy_sources"])
            except ValueError:
                names = []
            for name in names:
                o = bpy.data.objects.get(name)
                if o is None:
                    missing += 1
                    continue
                o["mapproxy_keep"] = True
                hidden.discard(name)
                if o.name not in scene.objects:   # original lives in another scene
                    home.objects.link(o)
                to_show.append(o)
            chunk["mapproxy_reverted"] = True     # kept, just hidden
            chunk.hide_set(True)
            for pn in json.loads(chunk.get("mapproxy_parts", "[]")):
                po = bpy.data.objects.get(pn)
                if po and po.name in context.view_layer.objects:
                    po.hide_set(True)
        set_list(scene, HIDDEN_KEY, hidden)
        context.view_layer.update()

        restored, still_hidden = [], 0
        for o in to_show:
            if make_visible(context, o):
                restored.append(o)
            else:
                still_hidden += 1

        for o in list(context.selected_objects):
            o.select_set(False)
        for o in restored:
            o.select_set(True)
        if restored:
            context.view_layer.objects.active = restored[0]
            try:
                bpy.ops.view3d.view_selected()
            except RuntimeError:
                pass

        msg = (f"Reverted {len(chunks)} chunk(s): {len(restored)} originals are back "
               f"and selected (marked Keep Original)")
        if missing:
            msg += f", {missing} no longer exist"
        if still_hidden:
            msg += f", {still_hidden} could not be shown"
        self.report({'WARNING' if (missing or still_hidden) else 'INFO'}, msg)
        return {'FINISHED'}


class MAPPROXY_OT_reapply(bpy.types.Operator):
    bl_idname = "mapproxy.reapply"
    bl_label = "Use Proxy Again"
    bl_description = ("Select the originals you got back from a revert: removes them "
                      "from this scene (or hides them) and shows their proxy chunk")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        sel = {o.name for o in context.selected_objects}
        if not sel:
            self.report({'ERROR'}, "Select the original objects you want to proxy again")
            return {'CANCELLED'}
        hide, n = [], 0
        for chunk in [o for o in scene.objects if o.get("mapproxy_reverted")]:
            try:
                names = json.loads(chunk["mapproxy_sources"])
            except (KeyError, ValueError):
                continue
            if not sel.intersection(names):
                continue
            for name in names:
                o = bpy.data.objects.get(name)
                if o is None:
                    continue
                if "mapproxy_keep" in o:
                    del o["mapproxy_keep"]
                if len(o.users_scene) > 1 and scene in o.users_scene:
                    for col in scene_collections(scene):   # just leave this scene
                        if o.name in col.objects:
                            col.objects.unlink(o)
                else:
                    hide.append(o)
            del chunk["mapproxy_reverted"]
            chunk.hide_set(False)
            for pn in json.loads(chunk.get("mapproxy_parts", "[]")):
                po = bpy.data.objects.get(pn)
                if po and po.name in context.view_layer.objects:
                    po.hide_set(False)
            n += 1
        hide_objects(context, hide)
        if not n:
            self.report({'WARNING'}, "No reverted chunk matches the selected objects")
            return {'CANCELLED'}
        self.report({'INFO'}, f"{n} chunk(s) are proxied again")
        return {'FINISHED'}


class MAPPROXY_OT_goto_scene(bpy.types.Operator):
    bl_idname = "mapproxy.goto_scene"
    bl_label = "Open Scene"
    target: StringProperty()

    def execute(self, context):
        if self.target == 'PROXY':
            sc = bpy.data.scenes.get(context.scene.mapproxy.scene_name)
        else:
            sc = bpy.data.scenes.get(context.scene.get("mapproxy_source_scene", ""))
        if sc is None:
            self.report({'ERROR'}, "Scene not found (create a proxy first)"
                        if self.target == 'PROXY' else
                        "This scene has no recorded original scene")
            return {'CANCELLED'}
        context.window.scene = sc
        return {'FINISHED'}


class MAPPROXY_OT_keep_assign(bpy.types.Operator):
    bl_idname = "mapproxy.keep_assign"
    bl_label = "Add Selected"
    bl_description = ("Edit Mode: put the selected vertices (e.g. the stairs) in the "
                      "Keep Detail vertex group so they are never decimated")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        name = context.scene.mapproxy.keep_vgroup or "KeepDetail"
        total = 0
        for obj in context.objects_in_mode_unique_data:
            if obj.type != 'MESH':
                continue
            vg = obj.vertex_groups.get(name) or obj.vertex_groups.new(name=name)
            bm = bmesh.from_edit_mesh(obj.data)
            layer = bm.verts.layers.deform.verify()
            for v in bm.verts:
                if v.select:
                    v[layer][vg.index] = 1.0
                    total += 1
            bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, f"{total} vertices will keep their detail")
        return {'FINISHED'}


class MAPPROXY_OT_keep_remove(bpy.types.Operator):
    bl_idname = "mapproxy.keep_remove"
    bl_label = "Remove Selected"
    bl_description = "Edit Mode: take the selected vertices out of the Keep Detail group"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        name = context.scene.mapproxy.keep_vgroup or "KeepDetail"
        total = 0
        for obj in context.objects_in_mode_unique_data:
            vg = obj.vertex_groups.get(name) if obj.type == 'MESH' else None
            if vg is None:
                continue
            bm = bmesh.from_edit_mesh(obj.data)
            layer = bm.verts.layers.deform.verify()
            for v in bm.verts:
                if v.select and vg.index in v[layer]:
                    del v[layer][vg.index]
                    total += 1
            bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, f"{total} vertices removed from the group")
        return {'FINISHED'}


def _scene_pair(context):
    """(original scene, proxy scene) from wherever you are"""
    s = context.scene.mapproxy
    here = context.scene
    if here.get("mapproxy_source_scene"):
        return bpy.data.scenes.get(here["mapproxy_source_scene"]), here
    return here, bpy.data.scenes.get(s.scene_name)


class MAPPROXY_OT_lights_to_proxy(bpy.types.Operator):
    bl_idname = "mapproxy.lights_to_proxy"
    bl_label = "Lights To Proxy Scene"
    bl_description = ("Link every light of the original scene into the proxy scene "
                      "(the same light objects, nothing is copied)")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        src, proxy = _scene_pair(context)
        if src is None or proxy is None or src == proxy:
            self.report({'ERROR'}, "Create a proxy first (needs the original and the "
                                   "proxy scene)")
            return {'CANCELLED'}
        col = child_collection(proxy.collection, "Map Proxy Lights")
        n = 0
        for o in src.objects:
            if o.type == 'LIGHT' and o.name not in proxy.objects:
                col.objects.link(o)
                n += 1
        self.report({'INFO'}, f"{n} lights added to '{proxy.name}'")
        return {'FINISHED'}


class MAPPROXY_OT_lights_from_proxy(bpy.types.Operator):
    bl_idname = "mapproxy.lights_from_proxy"
    bl_label = "Remove Lights"
    bl_description = "Take the lights out of the proxy scene again (they stay in the original)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        src, proxy = _scene_pair(context)
        if proxy is None or src == proxy:
            return {'CANCELLED'}
        n = 0
        for o in [o for o in proxy.objects if o.type == 'LIGHT']:
            for col in scene_collections(proxy):
                if o.name in col.objects:
                    col.objects.unlink(o)
            n += 1
        self.report({'INFO'}, f"{n} lights removed from '{proxy.name}'")
        return {'FINISHED'}


def _world_bbox(obj):
    pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    return (Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts))),
            Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts))))


class MAPPROXY_OT_restore_area(bpy.types.Operator):
    bl_idname = "mapproxy.restore_area"
    bl_label = "Restore Selected Area"
    bl_description = ("Select the decimated part (e.g. the stairs) on a proxy chunk in "
                      "Edit Mode, then click: that area is replaced with the original "
                      "full detail. The rest of the chunk stays decimated")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH' and "mapproxy_sources" in o

    def execute(self, context):
        s = context.scene.mapproxy
        chunk = context.active_object
        if context.mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')     # flushes the selection
        me, mw = chunk.data, chunk.matrix_world
        sel = [mw @ v.co for v in me.vertices if v.select]
        if not sel:
            self.report({'ERROR'}, "Select the vertices of the area in Edit Mode first")
            return {'CANCELLED'}
        m = s.restore_margin
        lo = Vector((min(p.x for p in sel) - m, min(p.y for p in sel) - m,
                     min(p.z for p in sel) - m))
        hi = Vector((max(p.x for p in sel) + m, max(p.y for p in sel) + m,
                     max(p.z for p in sel) + m))

        def inside(p):
            return (lo.x <= p.x <= hi.x and lo.y <= p.y <= hi.y and lo.z <= p.z <= hi.z)

        if "mapproxy_backup" not in chunk:             # pristine copy for 'Undo Restore'
            backup = me.copy()
            backup.name = chunk.name + "_backup"
            backup.use_fake_user = True
            backup["mapproxy_backup"] = True
            chunk["mapproxy_backup"] = backup.name

        # 1. cut the decimated area out of the chunk
        bm = bmesh.new()
        bm.from_mesh(me)
        doomed = [f for f in bm.faces if inside(mw @ f.calc_center_median())]
        bmesh.ops.delete(bm, geom=doomed, context='FACES')
        bm.to_mesh(me)
        bm.free()
        me.update()

        # 2. add the original geometry that lies inside the area
        col = child_collection(context.scene.collection, "Map Proxy Restored")
        parts = json.loads(chunk.get("mapproxy_parts", "[]"))
        made = 0
        try:
            names = json.loads(chunk["mapproxy_sources"])
        except ValueError:
            names = []
        for name in names:
            o = bpy.data.objects.get(name)
            if o is None or o.type != 'MESH':
                continue
            blo, bhi = _world_bbox(o)
            if (bhi.x < lo.x or blo.x > hi.x or bhi.y < lo.y or blo.y > hi.y
                    or bhi.z < lo.z or blo.z > hi.z):
                continue
            omw = o.matrix_world
            bm2 = bmesh.new()
            bm2.from_mesh(o.data)
            outside = [f for f in bm2.faces if not inside(omw @ f.calc_center_median())]
            bmesh.ops.delete(bm2, geom=outside, context='FACES')
            if not bm2.faces:
                bm2.free()
                continue
            pm = bpy.data.meshes.new(o.name + "_part")
            bm2.to_mesh(pm)
            bm2.free()
            for mat in o.data.materials:
                pm.materials.append(mat)
            part = bpy.data.objects.new(o.name + "_part", pm)
            part.matrix_world = omw.copy()
            part["mapproxy_part_of"] = chunk.name
            col.objects.link(part)
            parts.append(part.name)
            made += 1
        chunk["mapproxy_parts"] = json.dumps(parts)
        context.view_layer.update()

        self.report({'INFO'}, f"Removed {len(doomed)} decimated faces, added the original "
                              f"detail of {made} objects")
        return {'FINISHED'}


class MAPPROXY_OT_restore_undo(bpy.types.Operator):
    bl_idname = "mapproxy.restore_undo"
    bl_label = "Undo Restore"
    bl_description = ("Put the selected chunk back to how it was before any "
                      "'Restore Selected Area' and remove the restored parts")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and "mapproxy_backup" in o

    def execute(self, context):
        chunk = context.active_object
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        backup = bpy.data.meshes.get(chunk["mapproxy_backup"])
        if backup is None:
            self.report({'ERROR'}, "The backup of this chunk is gone")
            return {'CANCELLED'}
        old = chunk.data
        chunk.data = backup.copy()
        chunk.data.name = chunk.name
        chunk.data.use_fake_user = False
        if "mapproxy_backup" in chunk.data:
            del chunk.data["mapproxy_backup"]
        if old.users == 0:
            bpy.data.meshes.remove(old)
        for pn in json.loads(chunk.get("mapproxy_parts", "[]")):
            po = bpy.data.objects.get(pn)
            if po:
                pm = po.data
                bpy.data.objects.remove(po)
                if pm.users == 0:
                    bpy.data.meshes.remove(pm)
        chunk["mapproxy_parts"] = "[]"
        self.report({'INFO'}, "Chunk restored to its decimated version")
        return {'FINISHED'}


def _gather_with_rig(objs):
    """The objects plus their parents, rigs and children, so a character comes complete."""
    out = {}

    def add(o):
        if o is not None and o.name not in out:
            out[o.name] = o

    for o in objs:
        add(o)
        p = o.parent
        while p is not None:
            add(p)
            p = p.parent
        for c in o.children_recursive:
            add(c)
        if o.type == 'MESH':
            for m in o.modifiers:
                if m.type == 'ARMATURE':
                    add(m.object)
    for o in list(out.values()):
        if o.type == 'ARMATURE':
            for c in o.children_recursive:
                add(c)
    return list(out.values())


class MAPPROXY_OT_send_to_proxy(bpy.types.Operator):
    bl_idname = "mapproxy.send_to_proxy"
    bl_label = "Send Selected To Proxy Scene"
    bl_description = ("Link the selected objects (e.g. a character with its rig and "
                      "meshes) into the proxy scene. They are the same objects, not "
                      "copies, so animation shows in both scenes")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.scene.get("mapproxy_source_scene"):
            self.report({'ERROR'}, "You are in the proxy scene. Select the objects in the "
                                   "original scene")
            return {'CANCELLED'}
        objs = list(context.selected_objects)
        if not objs:
            self.report({'ERROR'}, "Select the objects (character...) first")
            return {'CANCELLED'}
        proxy = get_proxy_scene(context, context.scene.mapproxy)
        col = child_collection(proxy.collection, "Map Proxy Extras")
        n = 0
        for o in _gather_with_rig(objs):
            if o.name not in proxy.objects:
                col.objects.link(o)
                n += 1
        self.report({'INFO'}, f"{n} objects added to '{proxy.name}'")
        return {'FINISHED'}


class MAPPROXY_OT_remove_from_proxy(bpy.types.Operator):
    bl_idname = "mapproxy.remove_from_proxy"
    bl_label = "Remove Selected From Proxy Scene"
    bl_description = ("Take the selected objects out of the proxy scene again "
                      "(they stay in the original scene)")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.scene.get("mapproxy_source_scene"):
            proxy = context.scene
        else:
            proxy = bpy.data.scenes.get(context.scene.mapproxy.scene_name)
        if proxy is None:
            self.report({'ERROR'}, "No proxy scene yet")
            return {'CANCELLED'}
        names = {o.name for o in context.selected_objects}
        n = 0
        for o in list(proxy.objects):
            if (o.name in names and "mapproxy_sources" not in o
                    and "mapproxy_part_of" not in o):
                for col in scene_collections(proxy):
                    if o.name in col.objects:
                        col.objects.unlink(o)
                n += 1
        self.report({'INFO'}, f"{n} objects removed from '{proxy.name}'")
        return {'FINISHED'}


class MAPPROXY_PT_main(bpy.types.Panel):
    bl_label = "Map Proxy"
    bl_idname = "MAPPROXY_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Map Proxy"

    def draw(self, context):
        s = context.scene.mapproxy
        col = self.layout.column()
        col.label(text="1. Select part of the map")
        col.label(text="2. Create Map Proxy")
        col.separator()
        col.prop(s, "batch_size")
        col.prop(s, "ratio")
        col.prop(s, "merge_dist")
        col.prop(s, "min_verts")
        col.prop(s, "group_mode")
        col.separator()
        col.prop(s, "separate_scene")
        if s.separate_scene:
            col.prop(s, "scene_name")
            col.prop(s, "switch_scene")
        else:
            col.prop(s, "hide_others")
            col.prop(s, "hide_originals")
            col.prop(s, "disable_lights")
        col.prop(s, "exclude_names")
        col.prop(s, "keep_names")
        col.prop(s, "keep_materials")
        col.prop(s, "simplify_materials")
        if s.simplify_materials:
            col.prop(s, "diffuse_suffix")
        col.prop(s, "solid_texture")
        col.prop(s, "push_undo")
        col.separator()
        col.operator("mapproxy.run", icon='MOD_DECIM')
        row = col.row(align=True)
        row.operator("mapproxy.show_originals", icon='HIDE_OFF')
        row.operator("mapproxy.show_proxy", icon='HIDE_ON')
        col.operator("mapproxy.delete_proxy", icon='TRASH')
        row = col.row(align=True)
        op = row.operator("mapproxy.goto_scene", text="Proxy Scene", icon='SCENE_DATA')
        op.target = 'PROXY'
        op = row.operator("mapproxy.goto_scene", text="Original Scene", icon='SCENE')
        op.target = 'ORIGINAL'

        box = col.box()
        box.label(text="Parts you don't want decimated")
        row = box.row(align=True)
        row.operator("mapproxy.keep", icon='LOCKED')
        row.operator("mapproxy.unkeep", icon='UNLOCKED')
        box.operator("mapproxy.revert", icon='LOOP_BACK')
        box.operator("mapproxy.reapply", icon='LOOP_FORWARDS')

        box = col.box()
        box.label(text="Restore original detail in an area")
        box.label(text="Edit Mode on a chunk: select the stairs")
        box.prop(s, "restore_margin")
        row = box.row(align=True)
        row.operator("mapproxy.restore_area", icon='MOD_MESHDEFORM')
        row.operator("mapproxy.restore_undo", icon='LOOP_BACK')

        box = col.box()
        box.label(text="Keep detail inside chunks (stairs...)")
        box.prop(s, "keep_vgroup", text="Group")
        row = box.row(align=True)
        row.operator("mapproxy.keep_assign", icon='ADD')
        row.operator("mapproxy.keep_remove", icon='REMOVE')
        box.label(text="Edit Mode: select the stairs, click Add")

        box = col.box()
        box.label(text="Lights")
        row = box.row(align=True)
        row.operator("mapproxy.lights_off", icon='LIGHT')
        row.operator("mapproxy.lights_on", icon='OUTLINER_OB_LIGHT')
        row = box.row(align=True)
        row.operator("mapproxy.lights_to_proxy", icon='LIGHT_SUN')
        row.operator("mapproxy.lights_from_proxy", icon='X')

        box = col.box()
        box.label(text="Characters in the proxy scene")
        box.label(text="Select them in the original scene")
        box.operator("mapproxy.send_to_proxy", icon='OUTLINER_OB_ARMATURE')
        box.operator("mapproxy.remove_from_proxy", icon='X')


mp_classes = (MapProxySettings, MAPPROXY_OT_run, MAPPROXY_OT_process,
           MAPPROXY_OT_show_originals, MAPPROXY_OT_show_proxy,
           MAPPROXY_OT_delete_proxy, MAPPROXY_OT_lights_off,
           MAPPROXY_OT_lights_on, MAPPROXY_OT_keep, MAPPROXY_OT_unkeep,
           MAPPROXY_OT_revert, MAPPROXY_OT_reapply, MAPPROXY_OT_goto_scene,
           MAPPROXY_OT_restore_area, MAPPROXY_OT_restore_undo,
           MAPPROXY_OT_keep_assign, MAPPROXY_OT_keep_remove,
           MAPPROXY_OT_lights_to_proxy, MAPPROXY_OT_lights_from_proxy,
           MAPPROXY_OT_send_to_proxy, MAPPROXY_OT_remove_from_proxy,
           MAPPROXY_PT_main)


def mp_register():
    for c in mp_classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.mapproxy = PointerProperty(type=MapProxySettings)


def mp_unregister():
    del bpy.types.Scene.mapproxy
    for c in reversed(mp_classes):
        bpy.utils.unregister_class(c)

# ============================================================================
# PART 3 - ANIMATION LAYERS + ANIMATION AUTO OFFSET (embedded, loaded from memory)
# ============================================================================

import base64
import importlib
import importlib.abc
import importlib.machinery
import sys
import tempfile
import traceback
import zlib

_FAKE_DIR = os.path.join(tempfile.gettempdir(), "animation_tools_embedded")

_LAYERS = {
    "__init__": (
        "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"
    ),
    "addon_updater": (
        "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"
        "sd6r8GO3CavOKxqMv2wjSKUl1w/8TUVUHkhHUFJS3q4xcL3cUshF78S73zKaTotdCT/aHInfsD7jAoQ+VMmwuArnPswtId10"
        "napkYCPWUlev8/lZsb5dcCpH3d5eqCCabeC0bOkhhdDgcInf++X03ub03jy69+P43k/je1ejewuDw6Sgk2nAVdeqOPpiUdBb"
        "lTjOXKzLuO9Ew++Lc+8m4Ddwov/0IPqjf5c2QJurwpb1QGbKA/DoK7v1Jkl3pQi0l19aHQyKpbXAuvqI91O5zgH85EFCsuDm"
        "I1LSJ4WYZekcvfF/7RICfQ2Yo7PshIkUf2h4DapRn3wiYI0bDJGBE3VS6LiZMjRSrxbl7DyihBEGSD6UpeUeNbTkUK+ies6F"
        "stVl4SF69pOSUjirkcwTU5mxLxU0w1/xd46LKzBhi6xi2VpWbtB1vYjNnGrbHVeCdOO8TJuGnnunWlyfPutPOHySymPpKFMf"
        "qzMerljPJ4cN5xvL4Z02HjCuWpZDKxrL58tcVGtKwwoZtYr6mLH6MiufIh49tS/Q13WrIvycEvXqSmaHOtX4YebSHmssFInG"
        "W5Mfy890LcdvvHjLSaOW8d+yDdRp+gVq2CSOJBiOie+9ESWjdQqLGuR7zvWpzlEuS13Zxfjqe9458M3pw6XbVOxbTe23y0/V"
        "hx3Un1dQ5R48jSsC/FVSso41qjhB4LVVKlDwq2fMqoAzf0uRG4DEib9Gs3GFnvO7xWj26Oseg04iNaU9wYXMfIjjymREGwLp"
        "DZpdpYKTtKraIkfBKX6qm72M6xZE2F7ddGE9VxNoYplhx8ycPc2kCTbcwH7ypqWdsrCFm1mG27Ey25pNPnczy1XHsvZlLRdb"
        "PgmgSpXaVUq9gbo1FS0pVIHRvj8ij5xigyZ69JjGG1zaTfkqOqnFUJwOAqOTVVxIPpdc02PznUz+hxjw20LuOw94kLegE7vC"
        "au4qvfNXvhZPbk6q74nFYKlVyXfmu822MOToIVbqFWl5/r0N00jCxO7Ol+7T9D5u9Xcq24FDq0vF5lvEQmVpuXHzc7x2mtPA"
        "OGeEEmAEgB2sLmD49mopwwGbe9gRdIDPqI1w2hmGsRDJ11pXYT359c9dRmsGCkt/eAWBOLzPdtKiMzMTgEj3rzAiHxRs31LD"
        "YXfAc2ZfPl/+4uqXl4+Bj+8Wi6+WGB/M23FSYBrd1fS4tS+ZU6bgkNe+rlv5pMGm3Csnra/xmW03KY4zq3BOBmfgBcupnsvv"
        "oeXomn7qAzBgk6wl11E0bK3Co4uA5MvivE8RawqLw4F/GqN5LDYkMnne3HPma/7qB0a7a3KFpo1u3OLXXMNOe8o5LwjNZfho"
        "EOEJ4xFqFFD3n9s77F1oh/wqND6A5FqNZyqG9Zo95KB0s7yQuOG/lufT1C9e/c03NVLl/M+maWAHvZtmWeyEv/Li3YNWa1HQ"
        "ncOl645/53G5Tg8SXKbAwtEGBeovFmElCKwHz+pYLnpHbXTlu5acE2RHIARtuV5vAGi/BaY8m1cO1t6jxCYJxiECxyZs7laz"
        "6PPpc/TomTxRoHX4h04zc1yDw2fbEIN3q+2Xq4ys51gtAquIYAatjDQi14kvacswUD3mQAF6w51DZsmgTcVpsVpRwQ83kD0D"
        "zugZwoym64x37O1zUj01tFXCiU6xDu0BQCWInFxaU1e/5zqa6z1NkaMSCVFDPL5GriLfQqrcE9XylM+1pimXviA7IAqYuVjG"
        "+RxfcxxhrfkKZiUG4+gaGImecVToZ+xRNtlmaWEljU2FyqFMiu063hvWfHSiY3EQwIFabD8RZhRpxV+093cw8pr0qlRIk9ib"
        "g4Ncy6u8vXGyQr4KgiY+xN8uL2qFV4zkA+grclmlXVYwiz4gnA1TF3JVJR1k01/h61HPgTUVZclkAshFgx5eM0lZyMFrZgh3"
        "rSMkhJoQ2Yk/HBS5dPQFBTISyo6jt1KzuUyXSSqKLz7C0dFsjXH5j5Jyupt9ECUPxBQBRPQcy8jkscpA082ii9fP6cwvk/KU"
        "rb0Yv6e9kAmLc5rpqKdlvGJ2vE0wABSLsmDKYDE+O4OPRlM12CjLlyd2lzL7IPzBzhwgHPV0b2MWFHGKc6R4U1nxiDFszMl0"
        "YMqp3e+dfTd6cGYu76x3X1nkkYlBi+oDysqyR0XHYudR7XmqtvhENUxhUZwhsL8VWV6en1IItDOWEVMrX1ehD9pGRQvA78mW"
        "+rk9bAeHalUFhDfA7qk9tfbz7BNi7fPZJ1zm5zOAevYJAX0e4XN53jIV2OHcxPnQ0Qa22bm5A/bXCPzcidbdxsA+OCGUl1Jx"
        "6fpy2AlNXzfWbHJqI8lP333iaPWxGbkO3BFWO4XLCtEJ39X2wWqs5vTZjJVXcwLlmtJdeu/f61P9DK6h3bT5SHMbPM93PrBL"
        "AjWCW/Dg08pdb3tUQR5S/9i+JznRIR0cOLtnPQ2FKQi+6LXrbSZVDQ492DpajYacqAwoL+306kvw8wJeEUNqNMvfBjAuqTfw"
        "MxWk7s5b0nVAlbQQGvQATtbzkUF4ZHn82oiAh/3ncA7V1TzVIO61nuoX8d1P9ZKGuu2Jhq53PNHB0ww/nn38/mybZygxWuf6"
        "Vof0dudPhVzd7cQcV6oLvRSIMr2RZDNqnmRddu804y9x5I4jfvcWyXLFOo/OCqawdXa/4+qGFLGUGPUYjqvg7OuLZxStbWQT"
        "yfX/Ts8oo8xh9mMlgn0RVZTXqI2GOmPKfiPdxFqcz1agCqOM8rdiFZ87bKPGJurCEvRqpD8sXv25achP8L/AmrDveeeFITD4"
        "T1ur1Xw5eZZC8XjKydydcZX+2DDa/SbMjXv3k/m/nCDGqEFXYjJvFcjefxWFELRpWbn5VD5yxpeC8tMOj3Sd0Cm+rBBjWFOy"
        "2WZYcwzPknx4Dp+k+PLapFJXz6Mreou1zFL5EWzE/wV7sCiI"
    ),
    "addon_updater_ops": (
        "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"
    ),
    "anim_layers": (
        "eNrtvftzG0fSIPg7/oo2EXsALAiWPPZeHGPoW0qEJJ4pUktS9noZjI4m0CT7E4jGoAHRHIf/96vMrEfWq7sBUh7PrB2734jo"
        "emZlZWbls7hblMtVcrV46BT0z7JS/5qv7xYPSVYl80XnelneQas0/3W1FD/JJtm8uEvXq2KmO1UPVUe3HmWLxeg2m09n+VL3"
        "WYh/F9Uqn6+o3Uh9uMo+5Wm5qJyfq/VVNVkWi1VRzt1v2XRaztP1Ypqt8mWg7916tio+5Q+dzv8w0ybyv840v05mZTaFXcyy"
        "B9GgX+Wz62EyKecrsdHBbgfa9Xq9n2/zOTYt5jdJllwXszyZ3OaTT0lxnaxuxR/r5RKGFv3zySqfJuXVf4l/JEWV3Ber28RM"
        "MYQu83IF3ebWIMscmlflXb66hXnyWZUnq/VyDsPNxSpwNdUkn+fJHoJXrnOEv+FXPBCcKKUVVKLpxf5RWqzyu5Fc1HW5TORP"
        "STGnEUfiB9njEoED/6fr7M5sCjegljZ09pKJr2I7rLluSlugE72CbbzJxC5psvX8tpjmovc0qValgEY2m+H0y3yWGZhW2GJS"
        "zgDSgBPJVT7J1gJW5XVytb5JFstCbHBVJn8b/XccWbRNYej0c5HfI3CG9m+AK8NE/DnN5wAFABqtJmXzpALVAAf6PpAHHZwI"
        "ACt+AaDC8QiszCTMK0Il+E8ACNtUyXE5z83vtNL5qpivc4mgdpeRANUIICkWgjCY5fM+/C72vqxSPMaBPZ64H7DEdFWmDMnF"
        "L8MktAnetfteXEdxVOIcsJXY1DT/VZ39HZwPnjocEI1rdWdLpkmo+w960fsw/xF2HCTPk5f2wuE/2f+ixwboXYqjiQ1hjcCR"
        "7Hy5zgOrU+ekrhGgAoFEndroczZb51V/4K+uWmXLlQNVG4Jwf/1+gEHpXJx8aqbz+1rHn8/Mev1js6dw8YTfML1058KPlvld"
        "+TkP4PUIgY7Ls9cXaCoHwa1g2+j92fw+0uQCCvpUza4t/jCy/krlmgatGovL0qdL1NHsAcFoDllsIcYizkVLoNrybpbXRDqP"
        "j/aJA7gHs1iWgiWtHhRdh/PdA/5xPSqmKZAOi9xHSD20E1/1v9PVwyI36NQVNH4paKdYyrJc39wiSXV4FHAkQbwzQUCvBN1O"
        "7oERIS0XTYh5zVbJs2QyKyafOnUUbJnD5joad+G8YD9yy6Yhjp6aReNdkoA1xAk+mZPr3pfLT5nYhQAu0NmiqsTNFHC5A6gj"
        "2/8MLF6A9rvRy9ELTjyVKKIa7O0l/e+Gycth8sK52bBkvB1CaBDjplUuMDsT+Ps5D1Nsc58CPfb8T6k4gYveUTnJAOmGyWm5"
        "wn8l/1dyNslmee8ycFVXpbgdorfgDTcVkPtUjHi9zO5yQRUF8V+lNINo4lI7G+GxTz6fSpEpFTPOrrLJJ3lDkGJ55Kd7irdI"
        "sXMEtbkSjBMbkItVmAO4vwVxqUj+joTbJT6xAzAtLopLJbmIgwPME8cPTCjeMHZGPs0r2tDsInm2Z9hLxyKCDTfQ5eOmTxCd"
        "ukoCsH5lFwv+W93n2af0rhTUcr0QRATujzhDSSLcKzRMhOgm+LfkBAN/+YhR81nmMQtNPsr57CER4NIyHfSVGIwncZstcoGD"
        "JJuJqyaak2BQrcTqF7NsIqUEPTbcYfGUAIlxzpYilpHiz5WHFzARYBB+HlUrgdWC76thRnfrVeDogvDsHuREBLWISaOB0Jh9"
        "LospgQwIC8I6AVgT4nPJ1pZ4+LIuXlyO5AwhBmwvZr+qihsSpK6ACpO4JZYCv0hKktkXjEHEgI7a+CBwW4gluWsNDC+nuM2q"
        "bLVa9hk69ah1Ws3KVS8gGYXmxMbRifFr47rTqxmQLrhfgZHMx06UggFA8QErYXwtHjafBROhZ84DsT77rcL5GApsAmM09XVw"
        "lEhKio9OKS/IGfouV2NXTL39MvH/56V+5IhbYagVidoWLResWeDuVS5nnXYCN6WB1iqRhBGRGnkRWubTYoUQUOLQoh6/u7Q4"
        "a+VETPiTV+yabRXIgbqVWthRFIPEIf6+wl98YtFi+wHK6lLrHh7zMpvf5OlimQMC9PST2R5tms/o54tQp8tOJ3CprAMVF6tc"
        "r8pr7OjeKxzdYuWmbWczURjl2msh2BsBjB4vu7ZYiZqDy46zZETknib/2JIv1nQHiUtcSXyp2e0HgUFHkrYgJ4ELVvFRA21H"
        "pmXtguKLckYJLtJCCkIWNhrBslGaVQK3aWOeDeLRUcG5ATlbSbKEPJTTfdBswH24LpbVyvAHveAEBYrVbYbKFqmtEZcMWbOc"
        "XL0z/EMO4QK9HsTiSOF1JQTQPJvDpJbaC/Vv9BQWJDObLfNs+pBM18g6J9Blvejo17JFK+A2M2GIg3XXpWUhJmQEBfe0PZZv"
        "iV1f7QXOo3mAoM6BtBkh4SVAaVnPEYBm2Q9LiIp4e7MplU+L+RS4PcVHF56p+Dhdiz3OkmKV3OSgUoNNC/SS+HRb3sNZizfq"
        "JGesBYcWeH3Rk086VMf49B+Ic7mgG0V7BfEyvc/widzvHR7/dPLjOD0Yv9n/eHTeG2wHfYAHSkBShhDTTG77rtQwDAqX1Lte"
        "dooCcUsQ0JVO5XK3BwbRG5DGl6AwoQ25EgbIxU5DpqfaDa5VCDRVdpOTwq5S/RpW2LXX2Gk6sW21gttrAzvsYuGZmkG7G2qP"
        "anV+3UbtkqezZ5omKTyWsym89NcLdk49I0URm6B3E75HgAqLPooSQ89eZwMa35YMN7xbQ3iqCFkLehW78+Gruxc+9IYhNfT2"
        "ghTh4vnLS3d61UU/NgXzeNkwjUXLnQHsdwrso3f28dX56f7r8149eY9cEXstEb0Aw7rWuoHIuxEOPtLGLFapVtTmHUlKWd2C"
        "SnAtF10Xv4rBE9BKgE3n/raY3Ao5AKQ38fSB15A0wwl0hNFA/BAAxOugzU75r5PZGj6RelBMRcKTkoXaKHDqOLLUQ7VAcvbO"
        "AOVYgAgyzSqfBHRas3LyKTiUGkbr0S1W5CkIQM1jcMQjraF3YEjb5YyjOCQebJBC7nZaWC5hz0ee4VL9WFm2S/1r0HzZYW9x"
        "m5xaHAoUiQAfeGi7c7P3TNyC5vUabIpU7eVajj+hYxW9dmsVe1qI6WxGndnEQaopKFpRpVU5Ky3cMy0i5NaspUsvCq0PlBbz"
        "6XoxKyZo952DqoUENlKVYYP1vPjHOsePHa1/S6mtQB3SD8GftqrRLOFSM8BiiMPA53y+vkOdZ5+NZ79E2QeBx0KO7sO/gUQ7"
        "vIE1FNcYbcqwZPyFD0+z22SSgVhQAFzdnjNgSOVhd1Nsa28vZF8NtYXzjE8mb9YN+FEsU34XGO6YlmShQJIwlH8I3gdWEUtO"
        "Eg+QlL6iOoPb4LrVw3xyuyznxT9Je7fKwbEjWz6Is7qerfP5JIdXylIQrgd+nMpkzc4zzjtrYdeC5WNTsa0IQC3VD/6qGGo7"
        "UYos0vLZgCACELJxRgx6ngCGnSfralXecSi7Tx0YjZ0XiIzmr2hbOk79b0/krpdZ2ugfu8v1XJ4m2awqrzt/SewlL2KuB38X"
        "n9ClJsZ8mXyymd+EFlr2p9OQv4xU5RP/M0obGlXJInUm/kT5lRC+KLnDMSkHifZciFdkWvbYLreGwetkwDtoixvMYn2Q5MFu"
        "h79KdRiofVIzdp87pojdvobvoB/aP9LK7pmgKNoKrX5EbbzUbpvHDo6mgaYNja2NjNLOHjUdun46UkSgDq5biHMwu63cLJjJ"
        "0Tc3SlOjfBLWU1rq+bmoCvGoqCXHXWOSm5QCNycreioSa9T2r1FyyK0RxskHkI6o3Io84cRN1Y4/nO5qNmvobvTR59Jf264H"
        "dNcY9wx30i+n9+Pz/Z7lB+Xo5opnL2u1cEq0Dql4ojTeWY7+DpIWXe18mmre1MeGrmtPp4ukDM2LAojrgvAfpRr4ASQ2AiRy"
        "X8SFhlM2t2uZA5EUryMcs7zWxhQmXJXrFf1ULtQT9ZNGhY+HyZHoaumLCRvRK49tTrru0B4NqEVP2ZYxabwxcNIFir1g89FW"
        "NDzEdaXgATx+SUzedZKxmKi0rYVNP7wFoI12mFiUxbyNKQg3d9HTO0BdEw2rfyPOLnFI2koYj6UfpSBjeTQSQ5O7KyRPgwPQ"
        "couDq2a5MJuyYhBzh18MirH5VTPZzhLH+E+ijfqhWuS5/mOZLwQyqb/K62tBM4ZRiWIw8GR60BvJLd2LM1e0HCX56X+JESTt"
        "EaCZutDwhLKBpO1xeUKNz51PtpEH1fS4UWR3kgnEnBP0uSzKRb9InquV+D59/HQaG6v9cOcTi3iGwSx4+SOA/EMLoY2bMkgq"
        "Y0rSLTiBelRZwHZvQBIGOTlA9cWMARWWC3LTuP9imLxAF7CX+A/qPHDge78sVsq91Qd/nVWn63h2Az+tVmCyl6Mxp2Wlaacz"
        "EeC/EYwgrONkj6vNwEwnDQMLxtjmwSG1VHx/RkhkBBKODGmjOUG3CXjmsCbwZ6eW+QceJzWs1zxK+MvVXkno9fXVXsSfTy6b"
        "mjG6H/CSaWCKHe85Bqo8Gzmk8FslRK3l1UWmor5AT8EUVw8tzu2iB13JMiL+wfUE8jvjA9iMXRDRmkPW6iDWFmr+0muOPCTU"
        "9FuvKXGYUNu/eW2J/4Tafue19blTqN/3gjmjcFUrvGnRStqzE9WSSTf6dUIWeiVsiEt+m30uyiV75BEqhWcMvuCEEJOKGyIJ"
        "giXYDDoG98MjctlWTK7G+io0WNDdokmQGiYIl2VvENQoRPt5JnD/KaShuNc4Wpgc1Mwun2Lq54CzhyPqJfAc4f4ecbOq1D+Y"
        "BsUVaazC/tK2xl4Rfbe3WPG14CNiJVHhBwfTp+0NHlp72Ha83TaMr1CNrbCm7Z40i1heOAGldiogIZlTDZDsYTo6jMKbr94L"
        "aOv564f1do+DNkPLUthDnzqlfRJr2fHvmz1TAFWqdP9IKXQMqjMBdK7FGKX2cmRR6Rx0m02597jtbSm5ZAi2QdpYb1Lipp6R"
        "UvC7Wpk6G0u87ZcKOnmS+L3O5qFfAKxOd4v/kvfg7QrKJZIa0cj65uPx6/PDk+OzpPuE/yFWorEAuBguX6sQaGPayo14LnHU"
        "qBn0G72rPX/31IDopRv2Th54fcLtFL9RXDswAa6ydhZbT6BZ4EwGjFRM1JAzSN9AWxUnPgJqeCoSkgFGN8tSCDJSsvSFYN5K"
        "CtV7SQ/4TkJ8pxd4jXniKLg2o6+vDji6EjNVMK0QVqY8aFTpnrCB43dNrnr4Jb0uZqtcOqGKFTa5fPCjJzmd0EbijBhhaNo4"
        "3nhdRROrEpwb+0L27Ju2A85j54sRNOqL/yULn9VQ2sWbZtdiJjTVqrDiDpChKiEMG35ANdoQLApXOUERQAdu2YgzejglcFqP"
        "SThTVw/29bdauc+hJbbyz3xZVn0aYKBw2hOkxPR5NrlNBZb3e5OyN7SHGnhXUIwszgkMu3N4rBpQOT0vdne/vbThrL8RTHW4"
        "oO2TH5ZmBEAwuhjpkwUquCcA2ptltrhFUJbLoYkEQQ1tlYM3nXg3qIMhA4Ak2fK1Bg3mgq2Se4p/JjwQ+vra0q9yUSBqHGT8"
        "KUbA3EF80uW2aEu0wiMzPwL/FWHzU2Ws0iEEgSAKu4dFcOwoIbl2JFARPzW7zYU1mOtSE1D/bjCOaz/g5ySf+ERv7zMiuNcY"
        "xCjurRAVBQDEQ1KFWUGElRNQva1BW6uGpY3KN5XuNSqCgIvsKawYgQ9h3yhFh4kyuTKDFnGOloZtVzlieI5Uk2g7EwEf2uG1"
        "koxLMQdrFnuQkG3HZW02orlxo0qszASd2/WCtwKNyEIlXti9t6f7H96l44PD85NTwTG7SIDQBmdRCrJ8cPLTZJsIAQ4G3/X2"
        "iVNaDlptGKqgTVvy7mSRCXYjKaoiJ/jcgg+2rRsxEiZJ7UVpN2HyLhOA/HByNu4x4dveA04pZDv4awR/pCAjpcW0P8DFwe9u"
        "qL8eYlFWOY1z6QpJ+HKC8UbVYlYI9nY56g2EwPasd9nTZmF/KSGyEnmWdyazrKqSsTj0/dmMEPBkAbe7XKLbJqBSNVI/ydPB"
        "/7OzswPdKkpxspgpmiF+x+9XMwEAid876ITuxG7pZrPsKp9BKxgvEeuQN0E3KGXA2V7yW+90/Pbw7Hx8Kvh87+PxwUnvd9tI"
        "WMw/l59yO0Z+mOSf8/mK0Sd+ktcjssirk3Gs8DXedKrzgNOrAP9S7WJMDAn14bxYFdkM/IywvX5jVLv2as0HMc20mKx800eA"
        "Vqs12AQbKFxpbhAjQ3IrYZOUouoxkQG+FXBx9KwO7W8ZJqu3uiET4MpGAoXttyLFBZlqo9kyG1xcdDZzb9klDR9o5GCVTeu3"
        "0Ji7Dcv/PRDV0z3IV+iSTNLAXQmGHySlQPmXxc3titIrAIRm+bX6CxCDx+XoARda0VSxq3MvzqK8T++yeXZDK8PON9VIXM1M"
        "0IkR68epHft514t0kbQypVXv8cb2p2dJ7/3JR0Gp/SEmmWg+00P0Tg/fvjunxjq61p5GNDoav1Ft0G+L/VCjNfbGGoaXYAYb"
        "Wuvp2Ncd20MYZ1V5vmm2N1U0PtYSQCJnJVhdNktlqixSConZB65+ThDgj8fHh8dv0/cnB/tHLvXFUZqI74rr0tDz+kjlPGpQ"
        "NMW8x5iHI2L7a6UULFY9/uRRob1At5QZNBRJrz2cH/rxVwNPtiR3ECAdvtPbBmHM9HZaiNe4YP7/WBeYf4OAnJSSJw+TwNLY"
        "lnuDiDD3W+/N4fHh2bvxgTzFcHIADUsbcCYXFotYv75uAqNKPvNngR3HhKcDWq1A0ZSCi0/2Yf/sLD1/d3ry8e07Z8LuYSw1"
        "GtsHRe1/KhbiJoSOpga7m1w8N1iqO3OEBKlTCfDHFp0gfMqoCg8E/RXv2R+1OiqcJSJAYb3XmhNsiPcBM/6A3gtIWzWkZ9Sq"
        "TMBXmBgsRzN3/9iLXmkC/38LMIzeW2AKZ/B//lfvd3yPUCfyYoO33en4aLxvvUo22dQ2Jyf2jixspnRQtL9AWhBvBWYD9q45"
        "TxR7HZ+97v3eakPh+74FLvY5XPcYXBN1vGbJveOTY+TWh8evxuc/j8fHKbLs9yc/jXu/U3Spu9YtdxNOAxTZX5AuGHowLa5R"
        "XlqZfHmYFUIsQSZOFMvIkTwaETRErr4yAn1kW0T4lMN2jYRrni7haL5I6JL9Oviij50v/eCx3hW+z9jWD8mneDs99fsp+oby"
        "buQTvKW+wHvKd9ByrBqRrtK2EfIpido4bIr7lnKuefa4ipJtLHMgIfJqS1xNxtnk1hjuwK8r+SxIQUCr101+xrDWCtMtMGEC"
        "uw51QGuVi9ObKhdqMgQp/yKwv8SRr4W5JmD/YhxHepNPytn6ThkfYYBkkRVLMD2SISTpZ9crmbmE1hQaTe5HiJ8ziiS4S/rU"
        "mjwEYIiBfBRf7A6TC3DNvLz0RoK54QD10kfiFMDVov9c9Ph2UNe3XN6kqj/+b/OF6Cb7lAcF6Tp6oRkrrKSn6qjUCQaoOF+5"
        "SRGVClYgWuYpDdzHFuGcFVZQ2aDN2DRmmsMhoaRQO3yMeFqz+tBBHKE0MYQmDpb46hwwpOVT9ItRp3BB/3d3+OIS+Jz662Xg"
        "BLvuAHFg8oatYOpPJiiAuW90zowIlJB/cpGFriCjMmgcNhsMXQ3dPL4ZZSXdahvaT8csHsOrpiioT0NMwCXiQDICBHygnAk3"
        "5UjteWwsUDXGezULkkHgxjQsZ5HRqNWgDfe6CGz6spH3bMaEa2jPndjD8iFdQ2aW9OphRc4MD3geVfHPvLw2qwjScGuAT1eA"
        "Y/6Q3yQvX3z7XfwF7wwi+Ibzy6CVYAFCBL+UI9jBYwUWL8kttz1JU1S5LG6KuXgUSsGx88WwMbRCHEamXNXICKYnpMkaKTlk"
        "Apc4cDqnQCIokKy09yn5smSvhnPFeZLmjP48Vrga2HdJLDW0pCpJICIRBZkheq6IqfN/rAHsN1kxD09NRJ/T/C3kmUrLMzjM"
        "6HqWrVbigg4cOHYBC2VcTSyNY9dKipRPR+v5tEwX6+q2LzMkoQpbBueg/xs9ZJi+w82RhL6nhAGPnPcjDRKdN2R9jD7JjR79"
        "10m+WCVj/B+4IFmVuKlpk6OSMCBfLh1plqjEzhg+7O4Mk9zPYrWVFlFSccgH3v+tNz49PTnt/Y7BFf18AAaHUfI/lW4RzZfv"
        "lTVUWvMHYXC83j9+PT460mpEFuTocAxSprt8w4s5s5ynpHGXP5RpAO6SlwS0NgHLvKFA1LGteYxecaKD8fUL6zrb9I74ALZM"
        "titkDAs8F/pxBJAxLyUFG/eSc7FlNCkFrYW2ctTqMhgzoLruYQxNPzywjRwUQe402UU/QJ08llkfdPItm7OEuJ4frCC3Id/U"
        "IWJg86Wm+AUzcljweARY6hA9qNcM684Cq8AAyBeDTmC7XlChawjzVmNf4yivda6zxXHNot3ube/0l73S7W7Lbh0G2njHdw9v"
        "5heXm+rJujdSQQI3gbRZ0h0ZctKJcUEuuZFPqHl+LwUEvDzgvBJ8HyqtmHg53UPsZN9ZJ0gLe86WBm4EkHJqlZple684wzB5"
        "eRnq1X3L9qSUtxPQtazuc6nAxYx7kH5SbEnuMptCQJgUw8iQWwEXvc9nM28ONuweW+ZzZ1eBx7ZyQsVSDVLIkyCPaJmcs2fz"
        "1WJKAYnhFuWMcpUA7X81/t+H49Ne/d2W5Z5S8GKA6Z7tsc226onuEH5Xr2/gkpKbm0Mq3HYdnm6xRa5u7RKnyiCIs/eV1xWp"
        "r9C/GC2dtluW8nxrlxcL/7lVJiv9sSmvVzTnRoPPZtsU8nWuxM6zvdaJOOyXymKfI4m7ZN6uxOUurjpeN5ba+L1iEDBTSSfL"
        "XZ3QXCmE5VjfaI9O7UxeYqToNLnLlp8Sk/xgE/mLC19fbSd8ed6wvowtWZkMW5KMRXZS29qNvZeVIGIGkAEPtVpIpcpyM4do"
        "Hg51I1In+JMlp3qW9N0sVM/9ZoPkG/kjhRlTzD7pb0OjPrfCvFP+6Wv5qYISLU6Csyo4mNUDFizbBtfpNqZFdgPTWgHEUkG4"
        "2/GUjgx+z2un7jjqx34Q2DUDGCDbi7d/7nDfV06Gm9WdHEf+Y3Dj5TcbYYfd/LlaqPPBLrz0pFgSO75A9E2kPJayJIHEpHiD"
        "TngUeFZZmXKqhiJZbVJutuO8XRlCwjO0YVVGTN2B6svyTrB1yK4v085KEz6PXImn8f1qL5pJdqMAIFrsslYi4XlQW9ckUZGv"
        "kXonxr9hoda4npNxgSx7xbyobmXwk7h6TuACDhvwdnL5kuy2TaITSlu5ZfilhEChg9GdmJlAPjLfE8GqU6TmVqni6nP7huMo"
        "J3h3koxdHpYsDK6vfAHgjK1EsKj/QshvQUzbN0IoE5S6SphLUVzYCwfSQCQyfodYZBpTB5ykMl2K1TOwmkCaIf2pqOA5U0xd"
        "hYRP8DYJHq6tzeCpAevjm0LWvbhU5GDYoHaYCNdkRorWFl3HN4IYox7x25Bfu8RM+QxmjrmmrhamnYJXtz9Pmt1B+lxwgJmv"
        "wAuDxc1+/WL0feiwXR0SuEq7A7bp5xsOAo4QLbsDoxEDXMAubMIJXwaXX8cXaKOJno6HbbW4C+ZAWNFWkPdtd1R0kySdFDxR"
        "5yodOvkHysC6YPkryxVJxYwbh7u6mE576f5bJPFhsZ5DgxRHjQkWJk9Fm9ZaDDmSnmTUKRRH/95+uBtCGwjobVHDswVTjSYR"
        "dfl9K/e8DeJF23vkbe6J1+iEFwo23YpdfckIVeUG0QLB657Zvko8HKXaqZ9TV7xpY08z81mlaJyGgeIune2ycyQfTk8+jE/P"
        "f3lE9g+ZX4kKiqaT22I2XeZzXkMY8AH+HrYt6k34Z0agRqq8LWQEMcRhAY+z8lqW/tYhFmz6oR2qrbicWqnS/ph6DKyvIhLd"
        "2iFUwXJd3HzPlLpjP9t5AMUHmNrd60gN6hYfhzZaraT/LcNu4aO2EJgZL1S7Sz2sED7ExauKzy2ukVwiA/4GweEwtXWubl/n"
        "c1DP5SOMyo8qvgyimYnDIKhfTVOH5vVBK7Y8f0AmtzGYBnfEqzGCSEeCGaqTxdsNqfeq9HMQzAWFsdGSvW5hHeqbJwa3uJgY"
        "n/OE11y3lYCInCfXY7QaWeYHnEdrmdelg3foCNwvfrNlmTweAwVDsiK04E5RWXmhNRFR2Z2pSLrYUzYDrxA+PiNGLDuQv22T"
        "Fyl0TCplEoRNrm4LEu/X87u1tpHlD3lSTKTAyIq2s85gAtSUji1xmc/QmUV5L+lqpxlkif6cW8Ap5lTNR4laKaRlqdipmAk1"
        "ZYym1HdJYkuBwIc7bcAlzSzLQQ2p8D5vRhhItxWcD5w1nXvJTsYeILQgeNc4+XR5BmAf9M8E7BXzkIBPJc2HQd32lx03k27o"
        "NGEVoU/Ooly+5ySEMyWmRm5LJ+VTDE2psqZOBWlQleMZ26634N3Ov5Q6utsedLYihkOOQywHU4gubj6gSimIGo1VAmXp1S0j"
        "AjAkN8P8Af0K5SfjWMgEIX9y6wC8OwfCdxKSqLwVN4/jkh62QzuJtr5n0MsrEQDAZfXcoLtVSEMmirMoZoYZ91nJcjIm80pd"
        "v/3+R9XN2LYkRliAtHOgt9H3N+ciDc5koHWhu15aFgO7+llQtGCf3YPkhfkai/IhZsjz1tk1oTqE7glcOdNVkTK8GplMzUlt"
        "bGIo05pjEG88cwT9qbPqRprLDJzLHJMT028j+puI+myawnIQbeBXTI+ki3iQqqdQ9dzdJvILm8TuDr6wx0f7Kr0SKUiCsxli"
        "p9KF+qlCBdWZlMspuzuYmB1yt1spCuwrKfNzhu+pPDa8NgZ7uNmqSxKexfRR2FPrg3PVGVj7BOE9+p8hbnYP/s+A1zNlG5jn"
        "+bSSqQK1Vg/+n1s3GltkV7McvbVKJThylOG2sfpigqC+wFteVFj0F9vYTYJZKLimQvQb6X5p/mvBk+wwd+VTKGJ8l5PbsTda"
        "TcFCW8hqta+NRnQfkXihHXfquwzj2FFZSzUCULfqVv5eryqVNkyAxfUSqa9gHEpkF6riufXJwLb6AAtG1phJY89nbYGTC/ia"
        "G59ylbrX8yn34+PJDIkCAYAGX18A4dt8tnAcb1qfuGLWfpkyz/c17PfapsYZe5+DWZ9dYRI8IYsKZvIQY2T4ZhQ7PCrvBW3C"
        "ATfB/KDLbiNeKxpJQgdbINrrfTKpRGLG02T06jxAQkeQYKbqDzw1lZWsu22xdqujvyHb87GOvSj+FWxjGBdnVB3mfMw03zFr"
        "BNlitHOsvKATiI69l7kCljKEGL5L002p9YyqchT5mX0yXhOyVJ5U7YsBjnjZ24gXhSO4dRPKPsWSqNSaKLiRo64iXU1B3fqy"
        "ozXCnuX6GPK8cPwDZL6PECdoKEbimv8DA+lZY5WmEUTcHOvCRbf119CaBfEs6W59SqeISjehoCIyhwEeWcU10cRltFACSyGi"
        "iwY1pbCCcT5N9bN8j6WY78s2+5YPJTohjDAH1qo2ShvAWDXtNiG9qdSZamLJIOd9C5bAlkycwdyrjek6g3hY4fiGdFuDYNvn"
        "hqPwcXx3+Np5aiI2xZb58Tdbb/u8+hY6DgJVDzaRENWWNzmyTXmxmxw14IDlVT8N2JO5NZEeoLRGKLwUZEUxhiCtpxgr5NLq"
        "keFs7eteh6NoCXSwOrUKLBFlrvCY6rgn8PNUqYNNTXfLKP+0MNRMnSAIda59CHZqk461gUtX1smgZGFXZQmlhnRULcoCsj4f"
        "K7QNpRSnZV7NV4kbAW17T/JaY/iwE5+HSVUAfcckv2C4l/ZKMa14DMI0uLCbWXkFxF4sq+M9DoBoiw/RPNpS1j/OvCcabtMS"
        "LnUWSDHz7pNV71M5kwL5kmgFQXHYB3WNxGyHg0skbvk4aBjFntqCmT9mlyRG4+2jpXUURXBIN2sJ/qg4fIsCfg0xJUY+khsI"
        "+Duqm80uFdDMfy1Z2qBaPZdybWIfqCLOfe1MRdtYC7+6bSCzqZvUHBOUQD3X8tpV/IIjq8r0XuuOEy6kXVcm2ybLsKZ/AV02"
        "ivJAEvHYkyurquJm7qY1B5OronWfxROblGFIe8Hv+ypPlMeIfHnJTK3qRdXoB9/RyQco+tNfsFTzSjWeortWE6uFkzOdKTX9"
        "1OasY9uVGuKyWT8V8R1aOp6Vvn6pdGPc4Oa3ucv1Ov+6kp3xd2oD3dtg0EjNemk8CJT7Dk5tFdrFSsNX8Gq7y1e35RQWfl8u"
        "UbmIjzZMjaU9cBUKQg9NbY6PZClt/KShit9+HP/Sc3Kgm75Jb/+ox2VM0NXVnayOsiD+jI5H+a9i3xUFEEAYbZZQGZ1El/CM"
        "ayw2UFCYBaJrAivva1VetkfkB+uJFZcWRFUkIpvHgZvMx0+7wxZ91nhoGg02QXfAUlWukb3qegZhemHNSju25xQLxFjqFkUE"
        "t2DVSmniMUj3PkZ0IwGVR0jdYZ3XV7xcLJOmqof5RAjTcygI4OyUc3wWm1vM3YaOs4f9kYX1qsS5ZlmOtir6ORjFu+dF8VrV"
        "ce1RGhUv4So+2XpVplS+hugGG0MDVs/EroHXji+oQyUkpHYInFhS2lTf4+NvmasGtTEaKisJn6qcRZVZCS9QwtUqLp1H1wkz"
        "2/ItFzcyynWlCjmD4n99uIZBbqRh1ogGu7VbMIMlChTOQVTotmNgUqtCxRZK62ddrEsrPpB+c88SvR0a3fPRX46V1bVd87XQ"
        "JgNMTGygDZZaDOooN7VcWcKIJe1zwVD6RMIkcjvqYaQBQL/vuhlB9KdRLCmIVVoG47qNjrR9eh6CVCypj9werw3UidUGoo/2"
        "acErzUqC9q88K/mY/Pc9K9xA9Kzk9tRZ2dXR1VnJRvTRPiuMfvliN2vTw5Lv03/fw1JxU5HS8LQ9dVjwp3dWsg1+c84qIhjL"
        "t1NEyo1yo00tad2Ev+35iwUDZxzps0kPsIF0GntDORqSWkmuub/FHq3K3QRSy+rUtTQ5TgX2bhIe58XoBfz3kk3DaoxbYXl4"
        "8Fdi41N67FKqHnnyKCo71eVf++Ul6V5gzD/GUpcrqiD9j7WYZzmHu0QhfULymdxiViCZowgnRk1QFai5Zt53cElOXv1/49fn"
        "vVqJzype3UZUrAnmig64WUlJBF9dnDcXNQedECVJQ+VxewhwXeDNJipwDL3AOYSbNzgqyKJMOkUVvKpTDNQSL7lUfqW99K1x"
        "MXJ6lC2X2QPdx8Hgwvnlkr1kNHagZm6ZL2bZJFcpq6RrrvYVW+bT9UQiIHCHl0ZpXIuJnZosZfUpyvQLxNwWWQniw9H+6zHp"
        "JYg4OS32Dw6wTkTo29nHV+en+wKvA9nVeEasl5fJ8z37KDrtMlRt0xOzYjV1xFyh/SBM9I5DH82WjSonAtNGmDzbGibPtoWJ"
        "19EloTVaJfClYQ92LEeJJNB3Xzg+2k9ewbcnyCny2Iec1sVZp+Qebqu3ODZnMZ/NnIcxMT6/uyI6Bvbejx3AR3BFXiMJwYc9"
        "sqZzsV50WFslgkoaVqQ5Lxt4N6IwbeGrtK3fUL2yeCtlsN1po6QuUh/ilR7malDLnhZEMTmK7tQeObfvaQxY7hgu9nC+ZnBn"
        "f7GYPVDCRfrMMElzLnQ+mk4FEFkJQoVT1+VsKhWpAEJolsrfgGVKqSykvrKDhXh1QxyluhBE9+Q4PTy4tD+aIdhG/Xl3+fJk"
        "qdqywjq1o2mxxBul/oYoPuyVQq7QPE0Hg8DmVOurrKILyQa36qWzbtYSxalJQRHejbH7LIteZMkrsaxP0/LeFGCXprFy+ami"
        "6vTgj/pUiZoeS1RVarAlVNnua6FKpgMz6VZsZzX6LPVpikQOk78xW8vGCQqYvOlJml1KBusYIjtWnvJADeSkm+FVQbcNcUMi"
        "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"
    ),
    "bake_ops": (
        "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"
        "9g=="
    ),
    "multikey": (
        "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"
        "bks+uAM0J/CKRy831vz8D8B66TI="
    ),
    "subscriptions": (
        "eNrdPWuPG8eR3/kr2uYHkjbFrOwLDlh4AyiKEggXy0Yk5z4sFoNZcrg7ETnDmxlqvQ78368e/ah+zHC4luzAQmJpp7urq7vr"
        "XdW95f5QN526PTxOJtum3quVKvlTXpX7bJc/Fk0btNzm74usPrQT/XN13B8eVd6q6mA+deW+MP8uq/ZQrLvJZFNsVXu8bddN"
        "eejKumqzptjXH4r5fV5tdkWjrtS75lgsLicK/kzXuyJvVL7bKd3eAk4bhqABcEdVAtx1URWqbNWbGv6+1D9f4cJW67rqih+7"
        "FX2b0Ji7XX2b7wJs6oeqaKgZAM4SbTNYix7azjWa+Acn2bd3t8d2RUhnt488YJ4AsrDDNkUaA4lhWW13x6JaF9n74rFdqrbY"
        "wV4Wm+wW1tlOLLZ+vz5EcUa/p1uuB3gIQA8KVd2Zg3L9m6I7NpXpsr4v1u8zc9owA25cfjiszAGvNsWhvWvyw312PGzyrsgO"
        "TeHv88nuK01U3mQLgwJSNRN1tm3yfZHEglqyNfx8ByDrthtAIepr5g9nWqQYIN9s5kSWS5VkAk0F22O1zppjVZXVHW+4/AJD"
        "/grnVPz2lE3jEi2AYX37L6Caue4zfYmng7gDQnwQDe6gggP8UBYP6gHECZJOsVF1pR7uiwqb2haHfO/Nc1tkXZ3pcbzVWVHp"
        "bV3EPbsmB7qooFs/d3oD8jW2nzOi7aBL1hZdB+i2IwdZvuzvD3tl6O8D0BU0qz9dqfl/LRX870IcWwr/dld3ZlPG8GyaaXHE"
        "p2Jc6IEH5zMuDu9l3j5sfhkDazx6GDj8bPhXaxk4BZprfWyaouqA8qndTMNfs21Z5TvuR6ulPnM+5elfjg2Red50JXBdSwoX"
        "wDQFcEHeAXHdHjtSdsX+0D1K0nCqjjprfZiUx9NvCeqxKVR3X9CyciQUHHSABSIKqG9xj/91bDtlpE2u9jV1POTdvaSkGIFV"
        "CSLOAM401DQ2UiAYIcAiwRcFdaPq7XbJIuFI8uC+7kCVqe9V2alNXbTVDJAt1vVdVf7EQnFaV7tHteF9xdUy+WgaMGvgg7or"
        "uvmM2hsmCUZmtlCfmbM8tvZzRp0E52GH69T4G9U73I6mYUB7hivWYP3cgriaL1J7ZlmDaavtgGCWykpAtH60pbYSPULhOO0f"
        "DluhBSqhKUeBLIZGKak8LYRqpj529ZYGzgyjJjSM1leus21xnwxoBHuhvrmyzPWNQLfADqg6J1Jk9HV3CEAnN5P7PPI4YiRT"
        "JzWJuhKivP+vK7XOAfd6a1msrLqamVvrzRapFvqBDq7Ui79n+itLEPyYuY8eItOyAzoEaUEc0N039fHu3gJ9KLt74nzFEs3x"
        "GWNsAV7DP1f8o9oiC97+C8+T6dlNfcPk6Dro7/5uY1MomHoWYs/T9gH5UlbiI7kpcDo59BYuy8p+z7rHQzEHkAslsXDjkrgE"
        "bFbtcjYcWjMNDl25zxI0EvuuqOaudTEIHPerXCrqiptWgENFh9YPAWbBGahtReZGS/Lpud/N2zGvZUp6p0VAKA9p00AegosF"
        "3NJ24O4Jq8wbyV3JoFu9wJ34O234dXkTYYgNKwYn5cgAjgEE3MlY0sTjT3iVxA6LaFRShkUUR7sLDXKvry/8xYZ4O4FzESM7"
        "9eiUbUVto4HWynfrOTUtwxnjBVCTlO3KyrrErAG4lZ4zOVx9oZ7/gc8PnHeQCM/0aYLuhU0Lm6PZ5Aq18PSmY32iJxNKZ6n0"
        "2gnN4RWzjnL//lI9v1hd+GRe7MRp/OkqJf/FscX7mTzAvq2/SPYctxUXZ2zCOIgRhku7EbxT5++1P955CIHcvpykMA17SWtC"
        "auwhfh/hPDteJ2vdcyb0lAxvNpu9UPsc5C360sT93X3eqUPRgEDeoyRsi6YE87vWYFptb+ZmOgChTaj+cBO1vytBoJOclyYn"
        "Ba5mu7ztMkYTo2Z9ttJUWkvBGNEhaLFUOTWHZz7jXyv8j/E6hNziPs8iYMANq4vnEh9pwSfmlvC0tcMKB5yOvEH/zdoYZbXe"
        "HTcgaWDpzhcxZgkaP34sySjZ0ARZJN0LtDn80+HuEieNCvkbrEI2igKf2KbpTCOCjehtULO0nzSIXdl2kyFz5xzGiNeNqhdI"
        "Y4XfM8kfousZdtHktEkU4+CMIHRnwFBxnQP0hgxjcZCBPRGdo5QRHLdo0ReeW0SWaubsJXDX6ia5WeAMwhcOYmbo7C3UlfEv"
        "6qZ3K832bTZ4ThRXsYEAxH2prsHU3ksjGX9OWsm84+Nsyj5zyyyL2rOy2hQ/sk2CquCD1QisHxhBsVPCsJw4c5CIQIEvzbaf"
        "YIRUjEFPVB/QVsXltrTfsS0tbeMBACuMsOFZzF5Be6PePRT5e/VtvSlmlynDcAjUocF/diC9r2dIn8cD/IhqEGRT8SHfHUm8"
        "zG5i5W72VYyICPjJC/sW5cZL0AEVeKvBqiSVfSjb8nZXZJK85JFpgfttThETHcMB3cWUhugXm7LLwF3VIAAXcz5tvS/u64fA"
        "1bNc/TgXZOyDMoTRDlL3grM0A5gE0ZI+fM2uG/1lKJT9JJxEC13UJO1jtQbnFiM+llg50ssM4trp5KMtTYc/eaor0THNdxzN"
        "hSnicIk247WxZXQt9WXTPwqt5LuH/LEljHVjDCES2SSFO+SYbA8c47ZntavX793q6Nv1DD9SRMoRNpnKsWQPYJK0HgHXxTdc"
        "vglXZDYlIumAq6bWJyRPEsC6c03NHwZbbFQlkGcgv4HocSW5qooHxWJSYXBctHe12wURnmQmK7tZy/RWN0RvHEYhK4F8RQFz"
        "csqDhOUYRUYNoMREvH6mmYkscf7uhVJkAw3AGIA7P9FwGfu03rieYRNhxRDZU9DSEwZpo+a+3BRZ3DlQRO7YdRjcSy0648SR"
        "vCUmGjA/wZg6ZH++IADLfry8Wdq4HQXZKDINdAQf/kDqk4LOrOg/lDkRCkyIzsNExHIEDqgrRplEjnvZooDt1vKzIjl9BS5M"
        "p6NEpIRQcNs4k5hxtSm3W7DKKf0EQ5jF7BA2Qjie52G0mJyrugJ6Eqel/pRc9LPnsWUpR131jBKygI0g4ndeCTEss6XJ0g0H"
        "0sNIeOTnaZ1sN4sOgDY5OBOPf/1zv3YjbwaCelGwzMgXRJJ1Slll+6LLh2F7IaU+M1MMi0Kco8J7EbYwOhWbCKINtPB5+jyC"
        "WGgy+OeHM6bq0JRVN9/O4rjNv6NPP0ehj38HH35WHgRvrBjl+s98fJB8MC2DjJPYwewhb1CezGev3/zzu/95lf3l1V9f/PD3"
        "dwGUVCoI/+iYEJOClno2F81KRpgSQnuz4OdMldAQEyFxWK9THvRMP8MKVupqEnEMxahMynuQItWKgDEzcRYtN065QYFp4JlJ"
        "eiIgrthPGUFIvbZRykWIZuVJB+bEiFZCtKQd5xj8SUM1iv5ennDKx8FbjAsUaDRBRLpvZZtpX+0RdjzfoAadG0JMwx04JrvP"
        "4aSTU+cxiq4ceAd01AESw8TimQOvscVBFib20KKdok75el20rc7WtJ1uKqsWLC3ufF+Cv9ms7x8BlhYHD/flruA4GTF0dF5G"
        "d8hmY5tqlqePunhpjItj1/NmpxfQ6mBfpyzvkwbmyiI2mKgAA1tkNVBZ0XqPJW91jTE3HYrN7zB0q1P6vHO2sGwrcvwaUMsT"
        "aNsf19HiNmvB0l9CcUYFw4kgsOAqL22X9jz7fIYoZfwyna0zbtL1LFYvMuxxjjZmhe/1k59izTxeEWkPtBUSWS6SijrjvORS"
        "7bGGxXpe6L5RqQpoET6MVBLzKeUOnn6dAiuUZO+b8jxBaTC14GPyKQ3laWrWZLp08AwjiNJWE63hLdyo2wJMS8+ImskTHSiw"
        "kGWEbkRc55CZ0oQ+q2siZ5dDRltqI+c5swQjZB3OuKDkgm2MicaIB5PM0nKUt3tb73b1gzFO+mkI2aapj1hJEzPPVwvwCKNG"
        "Sux9tWBW4uaeUX7/Jxu7s6+f/zGx/k9pijolEce2rFqwlV5Y2i14l4LPGJstUlwDloiaM5VjogjH//Bad9Ju5dtyX+7KvCH2"
        "ByRSh397xAAOxmX5kLkexmCiy90R2EsmDywbK9scPNqNddifIkDOYNgBZj2HUQfLgn7lyh7LjigdUzVM/4klQNE8H7cIKAL/"
        "K5UB/YJanlPGxK9iVDzdyx2uiBpT58OT+VUf0UJ7R7CQCHZhsI5oHk3oaZYsoVl8mbCr887pk8ViZOnUIlE+ZJTKH/9b9asJ"
        "NesfqoMvWRx8yeLgi9iyf/eueGwYJtiFnxNIfhw1mCqf/0XFMRxpGU4IWcVKOk8qO45E02hWiDbnJyIq45ySs+J+Jmguwg0Y"
        "LqEFwFR33b1Phyc6jyggGD0donY87Aotd0zdHeLP/EOFUfCfxaWaUimSCSqILTXbuN3G0tQDqSTAuJ5hTHaoH9urK904l8G6"
        "lTgRALNUvY3PbxaLy1P1DpHHL5zbxaR31X2TsreNRD0uLXgpovjvWPVSDm4DGIAj9aFQLimNMQaRKDxTnQZ2TKAOg5BiX2D7"
        "BC+dMwypeAs+yofiI0DAf5qNyg41SOMQ6DQ/HKhAvHAno0xkTumobFfsD+4jsYK//aZsas5mNkidrgTTmk9lkSriGEA43nLW"
        "JvHpXLvrjxS8DoHa1lj7mQKEp4E4gb20Xu39EhdNOllqZeONqZEysllubQR96MZPXN6zRdVqXCw83rrJm0dx8DqXiaEMJgId"
        "6yKzGi8AAXYzeRoz9SVy/zwRQF1A00xsJasgDxKOCNr1nga2Pbla3GJkB+/AUlmLHwG+ffnqzavZIlR2MWd5kTwxpxZh/MEg"
        "5C7rYQUyt622sMi5XY2d0u8+orJO4+cGDbCuxFqXJfon6dOtQVWbed4sC68WUpMTOeJNQSEoiqxRAtkTErfFOj9ymE7eWKFj"
        "UHQMsmjSwfYJ19ZIManTT0AtLzgR4EpPbMVnwAwyey47xweNCkzwj90M/nHhh19H51Mmfgm5kwP7Y5c+7ainlhhBTn9qyIL7"
        "232XVbhBExb9ri4uLp6LeSwJlVULnD0X4m6RRkdn9LQNGhZe2EoN2p0guSfJg23QVBEOpgBq0BP5sauh2RZTt+855aEqsDEA"
        "r8LEhmykbl/yRWBTzNCqh0JoH1u0izEj4Zlrb9lH3Xebp4ojHD21CYLKnn3Vf6pUd5GYRNCJJWa57T25gORTAIMEZ2pfoxlS"
        "hgB2jlIWq66ud9ahJns6IKEMz80zrEc+QjDwEIG0jqdsamMih3Dnq52h1bdE7JnGmIyQPqybs4wqx72CEC3rHLH61oyOQK4C"
        "TKltWzYgDZmQy0r0COLUvQ809Mf/yqrsynxX/lRk/qB5cFFDLOthH+SdHoBs64dsn1f5nbvhamjiYb8yxan9vt7nL968/jb7"
        "7l148J8b3CWU62fPb1a3u6zcoOjur8uzHt0o0vziKxrkjgUk8mH1U9HU7Zwh9QivANAKkyf5+p7qvWfrerZ0ME1J5st6f8gb"
        "W8WFhEHBaCAxo+I4pcMn3Orc6gZcsHLN4WSkRV3vpfEFZ6XidO66ydt7fL7BHAW4R02TP2bF/x3z3Tx858Oh17+XfTImKcX9"
        "6jmi4ys3y2QibsskqvCsfHc3VGUui1TuWtzg9mo4CAyLaBPU13lbuuW9P+66Ep0PjkZanIIYhZHKeDoBisktMg9h+K+VsNX+"
        "eOn8vUSH4sd1cejUG8DiVdPU4m0Ev3OY6zWNhxrEpQMXIOanOVGE+0A/Owk1SAk9FSdZUReXamrnhahNEge/VBAqfNZ7rOIx"
        "6JZvsYSfE2h86CiLQTbj6wIYrau39tkB70mH4GoVik5zi4igjciceP1dhan82pulTzxa4437LDFQ5Ky1lVvvNjZ5CLaIF2Od"
        "PO3S6ohE6PgVTl+AS8cm1oNGFHNmI9DEAhdvnsTdu8m0/4/69u3f/vzDW/X2hz+/ffmP19+/e/3dm7dqOvSHBd70pV6wu66H"
        "vshbkaLFaLqttInzcJZk/8ZWTVQkAtjXbEuAWUplNfU2zgEbyvxFjy5YX4t0A1rBePufsYCjeX5hFUjtJavp65LfOkh2bIP9"
        "cJjq7EPfYyT6UqeLU6T6yBu0jET6+m2QIemFNzjZN+qC57BgtWmY3nk++n42scf/qtp4JSMd/YRL0DKJ97A24ss84KHPfVSV"
        "T5g+7SnyGS0Bzk05j3gY4nVvFly4TMhiAKDvro9XmQ3gdVd5uTJODWsXy8hxhqkBrtjkCt2FJ6WFp1FeOHHt0csft30JZJB7"
        "/CZQfCPB7JJIJlcCYpSvPZF0HnoNAU3mVNqZfNfh+wKDoG3RuoiW/1QeQnDLZM57kUz/jn/6oRerM/Pbg3DG5Hb7bvX3ZXbH"
        "38RPM++46/ctCDCUNkOPLQw8ivAR7+ifdfce9PVb8wZZQjURauI1vOE33FyCc7A3OSGs+cwDUvDViO0T7+KZEk47g1SXGM8F"
        "8dzWO5DPn9sJP1/KFU+VWTHJ1Noi4YzitqQQMKp8ftnkrmxBCgt/yr6LFrwPZxZ7CivRz8fOgRKvT/WAit6nCheqSwGGj0PH"
        "FVG42I64zOvkqpZpDG8SCTrxJqEb01T53PYAH+bKNrk6SzrtqwQFuC55c9dezRfuA5xTuX286jMrlqnyUInh4Xi7K9t7ws9D"
        "y7CJMncpjNBgezE0X75AzIATOOCLBd7a8XLXDllg2buI5hYifvXqnMWVuH4jSUPXN94sUB1uxRiPe/zCxicpbkXQXOhafu15"
        "OSL1fJxxW/vsp5OPOvSVoz3tNQVjmZ77qsJTTItR1sSv/4IVhcaDOyGXz57fjLQ+AnTA0m0avCPq42T0EIUagj69d0efVjjn"
        "tu+zq/h8R5pO0718RNG9amKigsWGeKUNJxdXMj9Lld+t/Ciqq4nfUlo9r+wGmrsXFOj2sIEt11c6TUqO7yS27OhGN+YDFHuQ"
        "otpP/2DSxljQ6boHnoZ6JbYk+SZDCpXBQUnb8arXdgxjqOkJYqPlxHOyVqBG5hBNUGtS8AT0VEwQPkAQqGoE0qfozVgq2UGx"
        "1K7e7PJ3TC4zEuEiGHGmVmXo8g4DP/QLC3KXGXi9c+RPej0YDZ0dMIWoyRyhk6fqBeg+dcjblq/mr6NI0JwKoRb9anyq0uEj"
        "W+jA+i5U+0ktGer8cfpeJ0rd3Un9pPBvrXx/RSV6rvKcPkV7jn7HaJxCPFXJLQCe/VLPR7pnaauEzr38eUbpUHDKmGsFIcQi"
        "TAcPbAWHKOjo3ycjX0XnHgF76vnt31DC2p0XIvaFrkKSAvZc2aormX7nwjUlBJ8oW03tTqK69KSL4wphTsrUT+etnHg/MEyt"
        "ansO32t1NS76TezX6ohHeaw2tbmAyFWYuAyT9rBHU1dkTOqX71DTgPUa1838hlrg3IfpeqOk6UFaYAfC0nv9KQXNXJEZEaFM"
        "VAel4pC9pahBdddkbBVsOO5kxVCyIu3UVj+p4jgqyFHvdG64AVb6kFcdF2GQK6zreoQQSb43/5SipaA4tqf6meKKXIPE1xPN"
        "bXaKvaCSIeer7WrteLkcursN7NdL0MJ0YnzSR0ijt3Nkid9wmV/qbFiUDpcjOf2r06rHQ1xV1QZh2KD26z+6NOhk1cwZv8aj"
        "31hx26WzkJ7V4qaQVZ6+Z/dWvx8WHvu51ocd/3s3QHqNBGmF9Bqk/PtUPskZ6xfZ0sfrPQ/3dP8dx//eDxjXmDhbPtJEmzAO"
        "X/CtOfteiXwnUMty/+ki/n0k+rUG3YNkj3zn7TcJc39Kgyt6UufjG2jjXwiejJnlPP/as9rOBWcsu+TLisElOf3aYixtRvwu"
        "qX6B4z9xF0mdRK1OWuaIPrNFVFM0NMYlApO/bmBQxMWzeg1Dk4cdzSUHnU1PD6LWmfn9Noci78eQm6X8tb+XiR5V6Ev5xytb"
        "RotaMppLjcOyp/jnZjL0O8G+Xqqv/d8JNqXyS/cwsip+LFttZ3O15p+BZzbw99err1KP5WTHcgyBQLdZULaYUea0PE0q/lix"
        "pV/6ewr9lh7oG3cQqBfoHoD4XVnt5RnZY6Mr4f/L4GGVpynJkYryicoyqTCfojSl4vSETqQ7Rz+9cOnVxCVfsQ1EN3fS3PeN"
        "50dP/1EQhesQhv5NJPgLU447/m0Bt3nLvyQszH+bukmZKOdXqoHRHL1pSew97JC8Ve3sH0Yi8QqEeubDe6a8a9pZf9eF+kIW"
        "/6SextK/IU5vk9iNsvU3YQMsnqPBae9E6W3h+yvGkxm3nlMbMhFPSVKlkghDuKXoDjwT9eD3HfgDiC1ZIft6m3zBqRVX8E1V"
        "KDAL6f269at6uaLwvAcMwmj1tdBmNz1veHgdWWnd9D4PojtrNeL68YeJqCdhiyDNj/Oe0ii/u5UhwYWK35Uh+mkNS1khFr5u"
        "tfj4VufHMDrPykZNzn9QRx5gU2ya/CHLmyJv59dq9s/Xr/43+/ovs5vF5P8BKtBReQ=="
    ),
}

_OFFSET = {
    "__init__": (
        "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"
        "Y8N9cIw9v10YDUc06tBqM/E24Kbku/d/exEFiQ=="
    ),
}

_EMBEDDED = {"animtools_layers": _LAYERS, "animtools_offset": _OFFSET}


class _EmbeddedFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serves the embedded add-ons as normal Python packages (relative imports work)."""

    def find_spec(self, fullname, path=None, target=None):
        root, _, rest = fullname.partition(".")
        files = _EMBEDDED.get(root)
        if files is None:
            return None
        key = rest or "__init__"
        if key not in files:
            return None
        origin = os.path.join(_FAKE_DIR, root, key + ".py")
        spec = importlib.machinery.ModuleSpec(fullname, self, origin=origin,
                                              is_package=not rest)
        spec.has_location = True
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        root, _, rest = module.__name__.partition(".")
        src = zlib.decompress(base64.b64decode("".join(_EMBEDDED[root][rest or "__init__"])))
        module.ADDON_ID = ADDON_ID
        exec(compile(src.decode("utf-8"), module.__spec__.origin, "exec"), module.__dict__)


_state = {"finder": None, "prefs": None, "layers": None, "offset": None, "map": False,
          "status": {"Animation Layers": "not loaded", "Auto Offset": "not loaded",
                     "Map Proxy": "not loaded"}}


def _fail(label, exc_text):
    print(f"[Gacha Animation Kit] {label} failed:\n{exc_text}")
    last = [ln for ln in exc_text.strip().splitlines() if ln.strip()][-1]
    _state["status"][label] = "FAILED: " + last


def _run_without_sub_prefs(fn, mod=None):
    """The embedded add-ons register their own AddonPreferences under their own id,
    which Blender never uses. Skip those: the merged class below replaces them."""
    real_register = bpy.utils.register_class
    real_unregister = bpy.utils.unregister_class

    def is_sub_prefs(cls):
        return (issubclass(cls, bpy.types.AddonPreferences)
                and cls.bl_idname != ADDON_ID)

    def register_class(cls):
        if not is_sub_prefs(cls):
            real_register(cls)

    def unregister_class(cls):
        if not is_sub_prefs(cls):
            real_unregister(cls)

    saved = {}
    bpy.utils.register_class = register_class
    bpy.utils.unregister_class = unregister_class
    if mod is not None:   # Animation Layers imports the two functions at the top
        for name, wrapper in (("register_class", register_class),
                              ("unregister_class", unregister_class)):
            if name in mod.__dict__:
                saved[name] = mod.__dict__[name]
                mod.__dict__[name] = wrapper
    try:
        fn()
    finally:
        bpy.utils.register_class = real_register
        bpy.utils.unregister_class = real_unregister
        if mod is not None:
            for name, original in saved.items():
                mod.__dict__[name] = original


def _draw_prefs(self, context):
    layout = self.layout
    box = layout.box()
    box.label(text="Status", icon='INFO')
    for label, text in _state["status"].items():
        box.label(text=f"{label}: {text}",
                  icon='CHECKMARK' if text == "OK" else 'ERROR')

    box = layout.box()
    box.label(text="Pose / Animation Library", icon='FILE_FOLDER')
    box.prop(self, "library_dir")
    box.label(text="Current: " + library_dir(context))

    if _state["layers"]:
        box = layout.box()
        box.label(text="Animation Layers", icon='NLA')
        box.prop(self, "category", text="Tab Category")
        box.prop(self, "enabled_editors")
        box.prop(self, "auto_rename")
        box.prop(self, "blend_type", text="Default Blend Type")
        box.prop(self, "frame_range_settings", text="Custom Frame Range")
        box.prop(self, "lock_nlatracks")
    if _state["offset"]:
        box = layout.box()
        box.label(text="Animation Auto Offset", icon='MOD_TIME')
        box.prop(self, "auto_key_override")


# ---- Auto Offset: show its controls in the sidebars too (it only has a small
# header toggle by default) ----

def draw_auto_offset(layout, context):
    scene = context.scene
    if not hasattr(scene, "use_anim_offset_mode"):
        layout.label(text="Auto Offset is not loaded", icon='ERROR')
        return
    col = layout.column()
    if context.tool_settings.use_keyframe_insert_auto:
        col.label(text="Turn Auto Keying off first!", icon='ERROR')
    col.prop(scene, "use_anim_offset_mode", toggle=True, icon='CON_TRANSLIKE',
             text="Relative Editing (Auto Offset)")
    col.prop(scene, "anim_offset_mode_only_selected")
    sub = col.column(align=True)
    sub.active = scene.anim_offset_mode_only_selected
    sub.prop(scene, "anim_offset_easing_window", text="Easing Window")
    sub.prop(scene, "anim_offset_easing_function", text="")
    sub.prop(scene, "anim_offset_easing_mode", text="")


class ANIMTOOLS_PT_autooffset_3d(bpy.types.Panel):
    bl_label = "Auto Offset"
    bl_idname = "ANIMTOOLS_PT_autooffset_3d"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Gacha Animation Kit"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        draw_auto_offset(self.layout, context)


class ANIMTOOLS_PT_autooffset_graph(bpy.types.Panel):
    bl_label = "Auto Offset"
    bl_idname = "ANIMTOOLS_PT_autooffset_graph"
    bl_space_type = 'GRAPH_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Gacha Animation Kit"

    def draw(self, context):
        draw_auto_offset(self.layout, context)


class ANIMTOOLS_PT_autooffset_dope(bpy.types.Panel):
    bl_label = "Auto Offset"
    bl_idname = "ANIMTOOLS_PT_autooffset_dope"
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Gacha Animation Kit"

    def draw(self, context):
        draw_auto_offset(self.layout, context)


class ANIMTOOLS_PT_status(bpy.types.Panel):
    bl_label = "Add-on Problems"
    bl_idname = "ANIMTOOLS_PT_status"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Gacha Animation Kit"

    @classmethod
    def poll(cls, context):
        return any(t not in ("OK", "not loaded") for t in _state["status"].values())

    def draw(self, context):
        for label, text in _state["status"].items():
            if text not in ("OK", "not loaded"):
                self.layout.label(text=f"{label}: {text}", icon='ERROR')
        self.layout.label(text="Details: Window > Toggle System Console")


_extra_classes = (ANIMTOOLS_PT_autooffset_3d, ANIMTOOLS_PT_autooffset_graph,
                  ANIMTOOLS_PT_autooffset_dope, ANIMTOOLS_PT_status)


def register():
    finder = _EmbeddedFinder()
    sys.meta_path.insert(0, finder)
    _state["finder"] = finder

    for label, modname, key in (("Animation Layers", "animtools_layers", "layers"),
                                ("Auto Offset", "animtools_offset", "offset")):
        try:
            _state[key] = importlib.import_module(modname)
        except Exception:
            _state[key] = None
            _fail(label, traceback.format_exc())

    # one preferences class that carries the properties of all three parts
    ann = dict(getattr(AnimLibPrefs, "__annotations__", {}))
    layers, offset = _state["layers"], _state["offset"]
    if layers:
        ann.update(layers.AnimLayersAddonPreferences.__annotations__)
        ann["category"] = bpy.props.StringProperty(
            name="Tab Category", default="Gacha Animation Kit",
            description="Sidebar tab of the Animation Layers panels",
            update=layers.update_panel)
    if offset:
        ann.update(offset.AnimAutoOffsetPreferences.__annotations__)
    prefs = type("AnimationToolsPrefs", (bpy.types.AddonPreferences,), {
        "bl_idname": ADDON_ID, "__annotations__": ann,
        "draw": _draw_prefs, "__module__": __name__})
    bpy.utils.register_class(prefs)
    _state["prefs"] = prefs

    _tools_register()
    for c in _extra_classes:
        bpy.utils.register_class(c)

    try:
        mp_register()
        _state["map"] = True
        _state["status"]["Map Proxy"] = "OK"
    except Exception:
        _fail("Map Proxy", traceback.format_exc())

    for label, mod in (("Animation Layers", layers), ("Auto Offset", offset)):
        if mod is None:
            continue
        try:
            _run_without_sub_prefs(mod.register, mod)
            _state["status"][label] = "OK"
        except Exception:
            _fail(label, traceback.format_exc())


def unregister():
    for label, key in (("Auto Offset", "offset"), ("Animation Layers", "layers")):
        mod = _state[key]
        if mod is not None:
            try:
                _run_without_sub_prefs(mod.unregister, mod)
            except Exception:
                print(f"[Gacha Animation Kit] unregistering {label} failed:\n"
                      + traceback.format_exc())
    if _state["map"]:
        try:
            mp_unregister()
        except Exception:
            print("[Gacha Animation Kit] unregistering Map Proxy failed:\n"
                  + traceback.format_exc())
        _state["map"] = False
    for c in reversed(_extra_classes):
        try:
            bpy.utils.unregister_class(c)
        except RuntimeError:
            pass
    _tools_unregister()
    if _state["prefs"] is not None:
        bpy.utils.unregister_class(_state["prefs"])
        _state["prefs"] = None
    if _state["finder"] in sys.meta_path:
        sys.meta_path.remove(_state["finder"])
    for name in [n for n in sys.modules if n.partition(".")[0] in _EMBEDDED]:
        del sys.modules[name]
    for key in ("layers", "offset"):
        _state[key] = None
    for label in _state["status"]:
        _state["status"][label] = "not loaded"


if __name__ == "__main__":
    register()
