import struct, bpy, bmesh
from mathutils import Vector
from . import HMDLShader, HMDLMesh

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
    for prop in mat.items():
        if isinstance(prop[1], int):
            writebuf(struct.pack('I', len(prop[0])))
            writebuf(prop[0].encode())
            writebuf(struct.pack('i', prop[1]))

    transparent = False
    if mat.game_settings.alpha_blend == 'ALPHA' or mat.game_settings.alpha_blend == 'ALPHA_SORT':
        transparent = True
    elif mat.game_settings.alpha_blend == 'ADD':
        transparent = True
    writebuf(struct.pack('b', int(transparent)))

# If this returns true, the material geometry will be split into contiguous faces
def should_split_into_contiguous_faces(mat):
    return False
    #return mat.game_settings.alpha_blend != 'OPAQUE' and \
    #       'retro_depth_sort' in mat and mat['retro_depth_sort']

# Takes a Blender 'Mesh' object (not the datablock)
# and performs a one-shot conversion process to HMDL
def cook(writebuf, mesh_obj, output_mode, max_skin_banks, use_luv=False):
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
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.context.scene.update()
    bpy.ops.object.mode_set(mode='OBJECT')
    copy_mesh.calc_normals_split()
    rna_loops = copy_mesh.loops

    # Send scene matrix
    wmtx = mesh_obj.matrix_world
    writebuf(struct.pack('ffffffffffffffff',
    wmtx[0][0], wmtx[0][1], wmtx[0][2], wmtx[0][3],
    wmtx[1][0], wmtx[1][1], wmtx[1][2], wmtx[1][3],
    wmtx[2][0], wmtx[2][1], wmtx[2][2], wmtx[2][3],
    wmtx[3][0], wmtx[3][1], wmtx[3][2], wmtx[3][3]))

    # Filter out useless AABB points and send data
    pt = copy_obj.bound_box[0]
    writebuf(struct.pack('fff', pt[0], pt[1], pt[2]))
    pt = copy_obj.bound_box[6]
    writebuf(struct.pack('fff', pt[0], pt[1], pt[2]))

    # Create master BMesh and VertPool
    bm_master = bmesh.new()
    bm_master.from_mesh(copy_mesh)
    vert_pool = HMDLMesh.VertPool(bm_master, rna_loops, use_luv, mesh_obj.material_slots)

    # Tag edges where there are distinctive loops
    for e in bm_master.edges:
        e.tag = vert_pool.splitable_edge(e)

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
            writebuf(struct.pack('I', len(mesh_obj.data.materials)))
            for ref_mat in mesh_obj.data.materials:
                ref_mat_split = ref_mat.name.split('_')
                if len(ref_mat_split) != 3:
                    raise RuntimeError('material names must follow MAT_%u_%u format')
                ref_mat_idx = int(ref_mat_split[2])
                found = False
                for mat in bpy.data.materials:
                    if mat.name.endswith('_%u_%u' % (grp_idx, ref_mat_idx)):
                        write_out_material(writebuf, mat, mesh_obj)
                        found = True
                        break
                if not found:
                    raise RuntimeError('uneven material set %d in %s' % (grp_idx, mesh_obj.name))
    else:
        writebuf(struct.pack('II', 1, len(mesh_obj.data.materials)))
        for mat in mesh_obj.data.materials:
            write_out_material(writebuf, mat, mesh_obj)

    # Output vert pool
    vert_pool.write_out(writebuf, mesh_obj.vertex_groups)

    dlay = None
    if len(bm_master.verts.layers.deform):
        dlay = bm_master.verts.layers.deform[0]

    # Generate material meshes (if opaque)
    for mat_idx in sorted_material_idxs:
        mat = mesh_obj.data.materials[mat_idx]
        if should_split_into_contiguous_faces(mat):
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
                if dlay:
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
            HMDLMesh.write_out_surface(writebuf, output_mode, vert_pool, the_list, mat_idx)


    # Generate island meshes (if transparent)
    for mat_idx in sorted_material_idxs:
        mat = mesh_obj.data.materials[mat_idx]
        if not should_split_into_contiguous_faces(mat):
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
            HMDLMesh.write_out_surface(writebuf, output_mode, vert_pool, the_list, mat_idx)

    # No more surfaces
    writebuf(struct.pack('B', 0))

    # Enumerate custom props
    writebuf(struct.pack('I', len(mesh_obj.keys())))
    for k in mesh_obj.keys():
        writebuf(struct.pack('I', len(k)))
        writebuf(k.encode())
        val_str = str(mesh_obj[k])
        writebuf(struct.pack('I', len(val_str)))
        writebuf(val_str.encode())

    # Delete copied mesh from scene
    bm_master.free()
    bpy.context.scene.objects.unlink(copy_obj)
    bpy.data.objects.remove(copy_obj)
    bpy.data.meshes.remove(copy_mesh)

def prop_val_from_colmat(name, m):
    if name in m:
       return m[name]

    return False

# Takes a Blender 'Mesh' object (not the datablock)
# and performs a one-shot conversion process to collision geometry
def cookcol(writebuf, mesh_obj):
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
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.context.scene.update()
    bpy.ops.object.mode_set(mode='OBJECT')
    copy_mesh.calc_normals_split()
    rna_loops = copy_mesh.loops

    # Send scene matrix
    wmtx = mesh_obj.matrix_world
    #writebuf(struct.pack('ffffffffffffffff',
    #wmtx[0][0], wmtx[0][1], wmtx[0][2], wmtx[0][3],
    #wmtx[1][0], wmtx[1][1], wmtx[1][2], wmtx[1][3],
    #wmtx[2][0], wmtx[2][1], wmtx[2][2], wmtx[2][3],
    #wmtx[3][0], wmtx[3][1], wmtx[3][2], wmtx[3][3]))

    # Filter out useless AABB points and send data
    #pt = wmtx * Vector(copy_obj.bound_box[0])
    #writebuf(struct.pack('fff', pt[0], pt[1], pt[2]))
    #pt = wmtx * Vector(copy_obj.bound_box[6])
    #writebuf(struct.pack('fff', pt[0], pt[1], pt[2]))

    # Send materials
    writebuf(struct.pack('I', len(copy_mesh.materials)))
    for m in copy_mesh.materials:
        writebuf(struct.pack('I', len(m.name)))
        writebuf(m.name.encode())
        unknown = prop_val_from_colmat('retro_unknown', m)
        surfaceStone = prop_val_from_colmat('retro_surface_stone', m)
        surfaceMetal = prop_val_from_colmat('retro_surface_metal', m)
        surfaceGrass = prop_val_from_colmat('retro_surface_grass', m)
        surfaceIce = prop_val_from_colmat('retro_surface_ice', m)
        pillar = prop_val_from_colmat('retro_pillar', m)
        surfaceMetalGrating = prop_val_from_colmat('retro_surface_metal_grating', m)
        surfacePhazon = prop_val_from_colmat('retro_surface_phazon', m)
        surfaceDirt = prop_val_from_colmat('retro_surface_dirt', m)
        surfaceLava = prop_val_from_colmat('retro_surface_lava', m)
        surfaceSPMetal = prop_val_from_colmat('retro_surface_sp_metal', m)
        surfaceStoneRock = prop_val_from_colmat('retro_surface_lava_stone', m)
        surfaceSnow = prop_val_from_colmat('retro_surface_snow', m)
        surfaceMudSlow = prop_val_from_colmat('retro_surface_mud_slow', m)
        surfaceFabric = prop_val_from_colmat('retro_surface_fabric', m)
        halfPipe = prop_val_from_colmat('retro_half_pipe', m)
        surfaceMud = prop_val_from_colmat('retro_surface_mud', m)
        surfaceGlass = prop_val_from_colmat('retro_surface_glass', m)
        unused3 = prop_val_from_colmat('retro_unused3', m)
        unused4 =  prop_val_from_colmat('retro_unused4', m)
        surfaceShield = prop_val_from_colmat('retro_surface_shield', m)
        surfaceSand = prop_val_from_colmat('retro_surface_sand', m)
        surfaceMothOrSeedOrganics = prop_val_from_colmat('retro_surface_moth_or_seed_organics', m)
        surfaceWeb = prop_val_from_colmat('retro_surface_web', m)
        projPassthrough = prop_val_from_colmat('retro_projectile_passthrough', m)
        solid = prop_val_from_colmat('retro_solid', m)
        noPlatformCollision = prop_val_from_colmat('retro_no_platform_collision', m)
        camPassthrough = prop_val_from_colmat('retro_camera_passthrough', m)
        surfaceWood = prop_val_from_colmat('retro_surface_wood', m)
        surfaceOrganic = prop_val_from_colmat('retro_surface_organic', m)
        noEdgeCollision = prop_val_from_colmat('retro_no_edge_collision', m)
        surfaceRubber = prop_val_from_colmat('retro_surface_rubber', m)
        seeThrough = prop_val_from_colmat('retro_see_through', m)
        scanPassthrough = prop_val_from_colmat('retro_scan_passthrough', m)
        aiPassthrough = prop_val_from_colmat('retro_ai_passthrough', m)
        ceiling = prop_val_from_colmat('retro_ceiling', m)
        wall = prop_val_from_colmat('retro_wall', m)
        floor = prop_val_from_colmat('retro_floor', m)
        aiBlock = prop_val_from_colmat('retro_ai_block', m)
        jumpNotAllowed = prop_val_from_colmat('retro_jump_not_allowed', m)
        spiderBall = prop_val_from_colmat('retro_spider_ball', m)
        screwAttackWallJump = prop_val_from_colmat('retro_screw_attack_wall_jump', m)

        writebuf(struct.pack('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', unknown, surfaceStone, surfaceMetal, surfaceGrass,
                 surfaceIce, pillar, surfaceMetalGrating, surfacePhazon, surfaceDirt, surfaceLava, surfaceSPMetal,
                 surfaceStoneRock, surfaceSnow, surfaceMudSlow, surfaceFabric, halfPipe, surfaceMud, surfaceGlass,
                 unused3, unused4, surfaceShield, surfaceSand, surfaceMothOrSeedOrganics, surfaceWeb, projPassthrough,
                 solid, noPlatformCollision, camPassthrough, surfaceWood, surfaceOrganic, noEdgeCollision, surfaceRubber,
                 seeThrough, scanPassthrough, aiPassthrough, ceiling, wall, floor, aiBlock, jumpNotAllowed, spiderBall,
                 screwAttackWallJump))

    # Send verts
    writebuf(struct.pack('I', len(copy_mesh.vertices)))
    for v in copy_mesh.vertices:
        xfVert = wmtx * v.co
        writebuf(struct.pack('fff', xfVert[0], xfVert[1], xfVert[2]))

    # Send edges
    writebuf(struct.pack('I', len(copy_mesh.edges)))
    for e in copy_mesh.edges:
        writebuf(struct.pack('IIb', e.vertices[0], e.vertices[1], e.use_seam))

    # Send trianges
    writebuf(struct.pack('I', len(copy_mesh.polygons)))
    for p in copy_mesh.polygons:
        edge_idxs = []
        for loopi in p.loop_indices:
            edge_idxs.append(copy_mesh.loops[loopi].edge_index)
        l0 = copy_mesh.loops[p.loop_indices[0]]
        e0 = copy_mesh.edges[l0.edge_index]
        flip = l0.vertex_index != e0.vertices[0]
        writebuf(struct.pack('IIIIb', edge_idxs[0], edge_idxs[1], edge_idxs[2], p.material_index, flip))

    # Delete copied mesh from scene
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


def fake_writebuf(by):
    pass

# DEBUG operator
import bpy
class hecl_mesh_operator(bpy.types.Operator):
    bl_idname = "scene.hecl_mesh"
    bl_label = "DEBUG HECL mesh maker"
    bl_description = "Test mesh generation utility"

    @classmethod
    def poll(cls, context):
        return context.object and context.object.type == 'MESH'

    def execute(self, context):
        cook(fake_writebuf, context.object, -1)
        return {'FINISHED'}

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
    bpy.utils.register_class(hecl_mesh_operator)
    pass
def unregister():
    bpy.utils.unregister_class(HMDLShader.hecl_shader_operator)
    bpy.utils.unregister_class(hecl_mesh_operator)
    pass
