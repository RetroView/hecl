'''
HMDL Export Blender Addon
By Jack Andersen <jackoalan@gmail.com>

This Python module provides a generator implementation for
the 'HMDL' mesh format designed for use with HECL.

The format features three main sections:
* Shader table
* Skin-binding table
* Mesh table (VBOs [array,element], VAO attribs, drawing index)

The Shader table provides index-referenced binding points
for mesh-portions to use for rendering. 

The Skin-binding table provides the runtime with identifiers
to use in ensuring the correct bone-transformations are bound
to the shader when rendering a specific primitive.

The Mesh table contains Vertex and Element buffers with interleaved
Positions, Normals, UV coordinates, and Weight Vectors
'''

import struct, bpy, bmesh
from mathutils import Vector
from . import HMDLShader, HMDLSkin, HMDLMesh

# Generate Skeleton Info structure (free-form tree structure)
def generate_skeleton_info(armature, endian_char='<'):
    
    bones = []
    for bone in armature.data.bones:
        bone_bytes = bytearray()
        
        # Write bone hash
        bone_bytes += struct.pack(endian_char + 'I', hmdl_anim.hashbone(bone.name))
        
        for comp in bone.head_local:
            bone_bytes += struct.pack(endian_char + 'f', comp)

        parent_idx = -1
        if bone.parent:
            parent_idx = armature.data.bones.find(bone.parent.name)
        bone_bytes += struct.pack(endian_char + 'i', parent_idx)

        bone_bytes += struct.pack(endian_char + 'I', len(bone.children))

        for child in bone.children:
            child_idx = armature.data.bones.find(child.name)
            bone_bytes += struct.pack(endian_char + 'I', child_idx)
                
        bones.append(bone_bytes)

    # Generate bone tree data
    info_bytes = bytearray()
    info_bytes += struct.pack(endian_char + 'I', len(bones))
    
    cur_offset = len(bones) * 4 + 4
    for bone in bones:
        info_bytes += struct.pack(endian_char + 'I', cur_offset)
        cur_offset += len(bone)
            
    for bone in bones:
        info_bytes += bone
            
    return info_bytes

def write_out_material(writebuf, mat, mesh_obj):
    hecl_str, texs = HMDLShader.shader(mat, mesh_obj)
    writebuf(struct.pack('I', len(mat.name)))
    writebuf(mat.name.encode())
    writebuf(struct.pack('I', len(hecl_str)))
    writebuf(hecl_str.encode())
    writebuf(struct.pack('I', len(texs)))
    for tex in texs:
        writebuf(struct.pack('I', len(tex)))
        writebuf(tex.encode())

    prop_count = 0
    for prop in mat.items():
        if isinstance(prop[1], int):
            prop_count += 1
    writebuf(struct.pack('I', prop_count))
    prop_count = 0
    for prop in mat.items():
        if isinstance(prop[1], int):
            writebuf(struct.pack('I', len(prop[0])))
            writebuf(prop[0].encode())
            writebuf(struct.pack('i', prop[1]))

# Takes a Blender 'Mesh' object (not the datablock)
# and performs a one-shot conversion process to HMDL; packaging
# into the HECL data-pipeline and returning a hash once complete
def cook(writebuf, mesh_obj, max_skin_banks, max_octant_length=None):
    if mesh_obj.type != 'MESH':
        raise RuntimeError("%s is not a mesh" % mesh_obj.name)
    
    # Copy mesh (and apply mesh modifiers with triangulation)
    copy_name = mesh_obj.name + "_hmdltri"
    copy_mesh = bpy.data.meshes.new(copy_name)
    copy_obj = bpy.data.objects.new(copy_name, copy_mesh)
    copy_obj.data = mesh_obj.to_mesh(bpy.context.scene, True, 'RENDER')
    copy_mesh = copy_obj.data
    copy_obj.scale = mesh_obj.scale
    bpy.context.scene.objects.link(copy_obj)
    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.scene.objects.active = copy_obj
    copy_obj.select = True
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.quads_convert_to_tris()
    bpy.context.scene.update()
    bpy.ops.object.mode_set(mode='OBJECT')
    rna_loops = None
    if copy_mesh.has_custom_normals:
        copy_mesh.calc_normals_split()
        rna_loops = copy_mesh.loops

    # Filter out useless AABB points and send data
    pt = copy_obj.bound_box[0]
    writebuf(struct.pack('fff', pt[0], pt[1], pt[2]))
    pt = copy_obj.bound_box[6]
    writebuf(struct.pack('fff', pt[0], pt[1], pt[2]))

    # Create master BMesh and VertPool
    bm_master = bmesh.new()
    bm_master.from_mesh(copy_obj.data)
    vert_pool = HMDLMesh.VertPool(bm_master, rna_loops)

    # Sort materials by pass index first
    sorted_material_idxs = []
    source_mat_set = set(range(len(mesh_obj.data.materials)))
    while len(source_mat_set):
        min_mat_idx = source_mat_set.pop()
        source_mat_set.add(min_mat_idx)
        for mat_idx in source_mat_set:
            if mesh_obj.data.materials[mat_idx].pass_index < mesh_obj.data.materials[min_mat_idx].pass_index:
                min_mat_idx = mat_idx
        sorted_material_idxs.append(min_mat_idx)
        source_mat_set.discard(min_mat_idx)

    # Generate shaders
    if mesh_obj.data.hecl_material_count > 0:
        writebuf(struct.pack('I', mesh_obj.data.hecl_material_count))
        for grp_idx in range(mesh_obj.data.hecl_material_count):
            writebuf(struct.pack('I', len(sorted_material_idxs)))
            for mat_idx in sorted_material_idxs:
                found = False
                for mat in bpy.data.materials:
                    if mat.name.endswith('_%u_%u' % (grp_idx, mat_idx)):
                        write_out_material(writebuf, mat, mesh_obj)
                        found = True
                        break
                if not found:
                    raise RuntimeError('uneven material set %d in %s' % (grp_idx, mesh_obj.name))
    else:
        writebuf(struct.pack('II', 1, len(sorted_material_idxs)))
        for mat_idx in sorted_material_idxs:
            mat = mesh_obj.data.materials[mat_idx]
            write_out_material(writebuf, mat, mesh_obj)

    # Output vert pool
    vert_pool.write_out(writebuf, mesh_obj.vertex_groups)

    dlay = None
    if len(bm_master.verts.layers.deform):
        dlay = bm_master.verts.layers.deform[0]

    # Generate material meshes (if opaque)
    for mat_idx in sorted_material_idxs:
        mat = mesh_obj.data.materials[mat_idx]
        if mat.game_settings.alpha_blend != 'OPAQUE':
            continue
        mat_faces_rem = []
        for face in bm_master.faces:
            if face.material_index == mat_idx:
                mat_faces_rem.append(face)
        if dlay:
            mat_faces_rem = HMDLMesh.sort_faces_by_skin_group(dlay, mat_faces_rem)
        while len(mat_faces_rem):
            the_list = []
            skin_slot_set = set()
            faces = list(mat_faces_rem)
            for f in faces:
                ret_faces = None
                for v in f.verts:
                    sg = tuple(sorted(v[dlay].items()))
                    if sg not in skin_slot_set:
                        if max_skin_banks > 0 and len(skin_slot_set) == max_skin_banks:
                            ret_faces = False
                            break
                        skin_slot_set.add(sg)

                if ret_faces == False:
                    break

                the_list.append(f)
                mat_faces_rem.remove(f)

            writebuf(struct.pack('B', 1))
            HMDLMesh.write_out_surface(writebuf, vert_pool, the_list, mat_idx)


    # Generate island meshes (if transparent)
    for mat_idx in sorted_material_idxs:
        mat = mesh_obj.data.materials[mat_idx]
        if mat.game_settings.alpha_blend == 'OPAQUE':
            continue
        mat_faces_rem = []
        for face in bm_master.faces:
            if face.material_index == mat_idx:
                mat_faces_rem.append(face)
        if dlay:
            mat_faces_rem = HMDLMesh.sort_faces_by_skin_group(dlay, mat_faces_rem)
        while len(mat_faces_rem):
            the_list = []
            skin_slot_set = set()
            faces = [mat_faces_rem[0]]
            while len(faces):
                next_faces = []
                ret_faces = None
                for f in faces:
                    ret_faces = HMDLMesh.recursive_faces_islands(dlay, the_list,
                                                                 mat_faces_rem,
                                                                 skin_slot_set,
                                                                 max_skin_banks, f)
                    if ret_faces == False:
                        break
                    next_faces.extend(ret_faces)
                if ret_faces == False:
                    break
                faces = next_faces

            writebuf(struct.pack('B', 1))
            HMDLMesh.write_out_surface(writebuf, vert_pool, the_list, mat_idx)

    # No more surfaces
    writebuf(struct.pack('B', 0))

    # Delete copied mesh from scene
    bm_master.free()
    bpy.context.scene.objects.unlink(copy_obj)
    bpy.data.objects.remove(copy_obj)
    bpy.data.meshes.remove(copy_mesh)


def draw(layout, context):
    layout.prop_search(context.scene, 'hecl_mesh_obj', context.scene, 'objects')
    if not len(context.scene.hecl_mesh_obj):
        layout.label("Mesh not specified", icon='ERROR')
    elif context.scene.hecl_mesh_obj not in context.scene.objects:
        layout.label("'"+context.scene.hecl_mesh_obj+"' not in scene", icon='ERROR')
    else:
        obj = context.scene.objects[context.scene.hecl_mesh_obj]
        if obj.type != 'MESH':
            layout.label("'"+context.scene.hecl_mesh_obj+"' not a 'MESH'", icon='ERROR')
        layout.prop(obj.data, 'hecl_active_material')
        layout.prop(obj.data, 'hecl_material_count')

# Material update
def material_update(self, context):
    target_idx = self.hecl_active_material
    if target_idx >= self.hecl_material_count or target_idx < 0:
        return
    slot_count = len(self.materials)
    for mat_idx in range(slot_count):
        for mat in bpy.data.materials:
            if mat.name.endswith('_%u_%u' % (target_idx, mat_idx)):
                self.materials[mat_idx] = mat

import bpy
def register():
    bpy.types.Scene.hecl_mesh_obj = bpy.props.StringProperty(
        name='HECL Mesh Object',
        description='Blender Mesh Object to export during HECL\'s cook process')
    bpy.types.Scene.hecl_actor_obj = bpy.props.StringProperty(
        name='HECL Actor Object',
        description='Blender Empty Object to export during HECL\'s cook process')
    bpy.types.Mesh.hecl_material_count = bpy.props.IntProperty(name='HECL Material Count', default=0, min=0)
    bpy.types.Mesh.hecl_active_material = bpy.props.IntProperty(name='HECL Active Material', default=0, min=0, update=material_update)
    bpy.utils.register_class(HMDLShader.hecl_shader_operator)
    pass
def unregister():
    bpy.utils.unregister_class(HMDLShader.hecl_shader_operator)
    pass
