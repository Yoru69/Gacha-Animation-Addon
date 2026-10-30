bl_info = {
    "name": "Animation Proxy",
    "author": "Yolu",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Proxy",
    "description": "Duplicate mesh, remove outline modifiers, merge by distance and decimate, "
                   "while protecting chosen parts (face, eyes...)",
    "category": "Object",
}

import re
import bpy
import bmesh
from bpy.props import (BoolProperty, FloatProperty, StringProperty,
                       PointerProperty)


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
        name="Remove Shape Keys", default=True,
        description="Decimate can't be applied to meshes with shape keys. "
                    "Current shape key mix is baked in")
    protect: BoolProperty(name="Protect Parts From Decimate", default=True)
    protect_materials: StringProperty(
        name="Materials", default="Face",
        description="Comma separated. Any material whose name contains one of these is kept intact")
    protect_vgroup: StringProperty(
        name="Vertex Group", default="",
        description="Optional: vertices in this group are also kept intact")
    hide_original: BoolProperty(name="Hide Original", default=True)
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


def merge_by_distance(obj, dist):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    before = len(bm.verts)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=dist)
    after = len(bm.verts)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    return before - after


def build_protect_group(obj, s):
    """Create a vertex group with weight 1 on every vertex that may be decimated.
    Vertices outside the group (weight 0) are left alone by Decimate."""
    mesh = obj.data
    mat_kws = split_keywords(s.protect_materials)
    protected_slots = set()
    for i, slot in enumerate(obj.material_slots):
        if slot.material and any(k in slot.material.name.lower() for k in mat_kws):
            protected_slots.add(i)

    protected_verts = set()
    for poly in mesh.polygons:
        if poly.material_index in protected_slots:
            protected_verts.update(poly.vertices)

    vg_src = obj.vertex_groups.get(s.protect_vgroup) if s.protect_vgroup else None
    if vg_src:
        for v in mesh.vertices:
            for g in v.groups:
                if g.group == vg_src.index and g.weight > 0.0:
                    protected_verts.add(v.index)

    if not protected_verts:
        return None, 0

    vg = obj.vertex_groups.new(name="_ProxyDecimate")
    free = [v.index for v in mesh.vertices if v.index not in protected_verts]
    if free:
        vg.add(free, 1.0, 'REPLACE')
    return vg, len(protected_verts)


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
    proxy.name = "Proxy"
    proxy.data.name = src.data.name + "_Proxy"
    for col in src.users_collection:
        col.objects.link(proxy)

    # 2. strip outline modifiers
    removed = clean_modifiers(proxy, s)

    # 3. shape keys
    if proxy.data.shape_keys and s.remove_shape_keys:
        with context.temp_override(object=proxy, active_object=proxy):
            bpy.ops.object.shape_key_remove(all=True, apply_mix=True)

    can_edit_geo = proxy.data.shape_keys is None
    merged = 0
    if can_edit_geo:
        # 4. merge by distance
        merged = merge_by_distance(proxy, s.merge_dist)

        # 5. decimate
        vg = None
        kept = 0
        if s.protect:
            vg, kept = build_protect_group(proxy, s)

        dec = proxy.modifiers.new("ProxyDecimate", 'DECIMATE')
        dec.ratio = s.ratio
        if vg:
            dec.vertex_group = vg.name
            dec.vertex_group_factor = 1.0
        # must be first so it is applied before the Armature modifier
        with context.temp_override(object=proxy, active_object=proxy):
            bpy.ops.object.modifier_move_to_index(modifier=dec.name, index=0)
            bpy.ops.object.modifier_apply(modifier=dec.name)
        if vg:
            proxy.vertex_groups.remove(vg)
    else:
        report({'WARNING'}, "Mesh still has shape keys; merge/decimate skipped")

    if s.simple_materials:
        n, missing = simplify_materials(proxy, s)
        if missing:
            report({'WARNING'}, "No diffuse texture found for: " + ", ".join(missing))

    if s.disable_original_outline:
        for m in active_outline_mods(src, s):
            m.show_viewport = False

    if s.hide_original:
        # hide_viewport (monitor icon) fully removes it from viewport evaluation,
        # unlike the eye icon (hide_set) which can still be evaluated.
        src.hide_set(True)
        src.hide_viewport = True

    report({'INFO'}, f"{proxy.name}: {len(proxy.data.vertices)} verts "
                     f"(merged {merged}, removed modifiers: {', '.join(removed) or 'none'})")
    return proxy


class OBJECT_OT_make_proxy(bpy.types.Operator):
    bl_idname = "object.make_proxy"
    bl_label = "Create Proxy"
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
    bl_label = "Proxy Maker"
    bl_idname = "VIEW3D_PT_proxy_maker"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Proxy"

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
        col.prop(s, "hide_original")
        col.prop(s, "disable_original_outline")
        col.operator("object.make_proxy", icon='MOD_DECIM')


classes = (ProxySettings, OBJECT_OT_make_proxy, OBJECT_OT_disable_outline, VIEW3D_PT_proxy_maker)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.proxy_settings = PointerProperty(type=ProxySettings)


def unregister():
    del bpy.types.Scene.proxy_settings
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
