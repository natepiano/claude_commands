import bpy
import json
import os
import sys
from pathlib import Path

# Get configuration path from command line argument or use default
# In Blender, sys.argv includes all Blender's arguments
# Arguments after '--' separator are for the script
config_path = None
if '--' in sys.argv:
    idx = sys.argv.index('--')
    if idx + 1 < len(sys.argv):
        config_path = sys.argv[idx + 1]

if not config_path:
    # Default path for backward compatibility
    config_path = "/Users/natemccoy/rust/.claude/commands/blender/bake_textures.json"

# Convert to absolute path
config_path = os.path.abspath(config_path)
config_dir = os.path.dirname(config_path)

print(f"Loading configuration from: {config_path}")

try:
    with open(config_path, 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    print(f"ERROR: Configuration file not found at {config_path}")
    print("Usage: python bake_textures.py [path/to/bake_textures.json]")
    exit(1)
except json.JSONDecodeError as e:
    print(f"ERROR: Invalid JSON in configuration file: {e}")
    exit(1)

# Extract configuration - resolve relative paths from config directory
blend_file = config['blend_file']
if not os.path.isabs(blend_file):
    blend_file = os.path.join(config_dir, blend_file)
object_names = config['objects']
output_name = config['output_name']
resolution = config['texture_resolution']
output_dir = config['output_directory']
if not os.path.isabs(output_dir):
    output_dir = os.path.join(config_dir, output_dir)
settings = config['settings']
texture_maps = config['texture_maps']

print("\n=== Configuration ===")
print(f"Blend file: {blend_file}")
print(f"Objects: {', '.join(object_names)}")
print(f"Output name: {output_name}")
print(f"Resolution: {resolution}x{resolution}")
print(f"Output directory: {output_dir}")

# Validate blend file exists
if not os.path.exists(blend_file):
    print(f"ERROR: Blend file not found: {blend_file}")
    exit(1)

# Create output directories
output_path = Path(output_dir)
textures_path = output_path / "textures"
output_path.mkdir(parents=True, exist_ok=True)
textures_path.mkdir(exist_ok=True)
print(f"\nCreated output directories at: {output_path}")

# Open blend file
print(f"\nOpening blend file: {blend_file}")
bpy.ops.wm.open_mainfile(filepath=blend_file)

# Validate objects exist
missing_objects = []
for obj_name in object_names:
    if obj_name not in bpy.data.objects:
        missing_objects.append(obj_name)

if missing_objects:
    print(f"ERROR: Objects not found in scene: {', '.join(missing_objects)}")
    print(f"Available objects: {', '.join([obj.name for obj in bpy.data.objects if obj.type == 'MESH'])}")
    exit(1)

# Select target objects and their children
bpy.ops.object.select_all(action='DESELECT')
def select_with_children(obj):
    """Recursively select object and all its children"""
    obj.select_set(True)
    for child in obj.children:
        select_with_children(child)

for obj_name in object_names:
    obj = bpy.data.objects[obj_name]
    select_with_children(obj)
    bpy.context.view_layer.objects.active = obj

# Get list of all selected objects including children
all_selected = [obj.name for obj in bpy.context.selected_objects]
print(f"\nSelected objects for baking: {', '.join(all_selected)}")

# Set a mesh object as active if the current active is not a mesh
if bpy.context.view_layer.objects.active and bpy.context.view_layer.objects.active.type != 'MESH':
    # Find first mesh object and make it active
    for obj in bpy.context.selected_objects:
        if obj.type == 'MESH':
            bpy.context.view_layer.objects.active = obj
            break

# Set up rendering for baking
bpy.context.scene.render.engine = 'CYCLES'
bpy.context.scene.cycles.bake_margin = settings['bake_margin']

if settings['use_gpu']:
    # Enable GPU rendering if available
    prefs = bpy.context.preferences.addons['cycles'].preferences
    # Detect platform and use appropriate device type
    import platform
    if platform.system() == 'Darwin':  # macOS
        prefs.compute_device_type = 'METAL'
    else:
        prefs.compute_device_type = 'CUDA'
    bpy.context.scene.cycles.device = 'GPU'
    print("GPU rendering enabled")

# Get or create material for selected objects - ONLY MESH objects
selected_objects = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
print(f"Mesh objects for baking: {[obj.name for obj in selected_objects]}")
print(f"Current active object: {bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else 'None'}")

# Ensure all objects have materials
for obj in selected_objects:
    if not obj.data.materials:
        print(f"ERROR: Object {obj.name} has no material")
        exit(1)

# Ensure all objects have UV maps
print("\n--- Checking UV Maps ---")
for obj in selected_objects:
    if not obj.data.uv_layers:
        print(f"Creating UV map for {obj.name}")
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.smart_project(angle_limit=66.0, island_margin=0.02)
        bpy.ops.object.mode_set(mode='OBJECT')
    else:
        print(f"{obj.name} already has UV map: {obj.data.uv_layers[0].name}")

# Create image textures for baking
created_images = {}

def create_bake_image(map_name, color_space='sRGB'):
    """Create a new image for baking"""
    img_name = f"{output_name}_{map_name}"
    img = bpy.data.images.new(
        name=img_name,
        width=resolution,
        height=resolution,
        float_buffer=(map_name == 'normal')
    )
    img.colorspace_settings.name = color_space
    created_images[map_name] = img
    print(f"Created image: {img_name} ({resolution}x{resolution}, {color_space})")
    return img

# Create images for enabled texture maps
if texture_maps['albedo']:
    create_bake_image('albedo', 'sRGB')
if texture_maps['normal']:
    create_bake_image('normal', 'Non-Color')

# Only create individual roughness/metallic if we're not packing them
if settings.get('export_metallic_roughness_packed', True):
    # We'll create these temporarily for baking, but won't save them individually
    if texture_maps['roughness']:
        create_bake_image('roughness', 'Non-Color')
    if texture_maps['metallic']:
        create_bake_image('metallic', 'Non-Color')
else:
    # Create and save individual textures
    if texture_maps['roughness']:
        create_bake_image('roughness', 'Non-Color')
    if texture_maps['metallic']:
        create_bake_image('metallic', 'Non-Color')

if texture_maps['ambient_occlusion']:
    create_bake_image('ao', 'Non-Color')
if texture_maps['emission']:
    create_bake_image('emission', 'sRGB')

print(f"\nCreated {len(created_images)} texture images for baking")

# Setup material nodes for baking
def setup_bake_nodes(material, image):
    """Add image texture node for baking"""
    nodes = material.node_tree.nodes
    
    # Create image texture node
    img_node = nodes.new(type='ShaderNodeTexImage')
    img_node.image = image
    img_node.location = (-400, 0)
    img_node.select = True
    nodes.active = img_node
    
    return img_node

# Baking functions
def bake_albedo():
    """Bake albedo/base color using emission workaround"""
    print("\n--- Baking Albedo ---")
    img = created_images.get('albedo')
    if not img:
        return
    
    for obj in selected_objects:
        # Deselect all objects first
        bpy.ops.object.select_all(action='DESELECT')
        # Select only the object we're baking
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        
        for mat_slot in obj.material_slots:
            if mat_slot.material:
                mat = mat_slot.material
                nodes = mat.node_tree.nodes
                links = mat.node_tree.links
                
                # Find Principled BSDF
                bsdf = None
                for node in nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        bsdf = node
                        break
                
                if bsdf:
                    # Store original connections
                    base_color_links = []
                    if bsdf.inputs['Base Color'].is_linked:
                        for link in bsdf.inputs['Base Color'].links:
                            base_color_links.append((link.from_socket, link.to_socket))
                    
                    # Find material output node
                    output_node = None
                    for node in nodes:
                        if node.type == 'OUTPUT_MATERIAL':
                            output_node = node
                            break
                    
                    # Store original output connection
                    original_output_links = []
                    if output_node and output_node.inputs['Surface'].is_linked:
                        for link in output_node.inputs['Surface'].links:
                            original_output_links.append((link.from_socket, link.to_socket))
                    
                    # Create emission node
                    emission = nodes.new(type='ShaderNodeEmission')
                    emission.location = (bsdf.location[0], bsdf.location[1] - 200)
                    
                    # Connect base color to emission
                    if base_color_links:
                        links.new(base_color_links[0][0], emission.inputs['Color'])
                    else:
                        emission.inputs['Color'].default_value = bsdf.inputs['Base Color'].default_value
                    
                    # Connect emission to output (this is the crucial fix!)
                    if output_node:
                        # Disconnect current surface connection
                        if original_output_links:
                            links.remove(original_output_links[0][0].links[0])
                        # Connect emission to output
                        links.new(emission.outputs['Emission'], output_node.inputs['Surface'])
                    
                    # Add bake node
                    bake_node = setup_bake_nodes(mat, img)
                    
                    # Bake
                    bpy.context.view_layer.objects.active = obj
                    bpy.ops.object.bake(type='EMIT')
                    
                    # Cleanup - restore original connections
                    nodes.remove(emission)
                    nodes.remove(bake_node)
                    
                    # Restore original output connection
                    if output_node and original_output_links:
                        links.new(original_output_links[0][0], original_output_links[0][1])
    
    # Save image
    img.filepath_raw = str(textures_path / f"{output_name}_albedo.png")
    img.save()
    print(f"Saved: {img.filepath_raw}")

def bake_normal():
    """Bake normal map"""
    print("\n--- Baking Normal Map ---")
    img = created_images.get('normal')
    if not img:
        return
    
    for obj in selected_objects:
        # Deselect all objects first
        bpy.ops.object.select_all(action='DESELECT')
        # Select only the object we're baking
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        
        if obj.material_slots:
            mat = obj.material_slots[0].material
            bake_node = setup_bake_nodes(mat, img)
            
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.bake(type='NORMAL', normal_space='TANGENT')
            
            mat.node_tree.nodes.remove(bake_node)
    
    img.filepath_raw = str(textures_path / f"{output_name}_normal.png")
    img.save()
    print(f"Saved: {img.filepath_raw}")

def bake_roughness():
    """Bake roughness map"""
    print("\n--- Baking Roughness ---")
    img = created_images.get('roughness')
    if not img:
        return
    
    for obj in selected_objects:
        # Deselect all objects first
        bpy.ops.object.select_all(action='DESELECT')
        # Select only the object we're baking
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        
        if obj.material_slots:
            mat = obj.material_slots[0].material
            bake_node = setup_bake_nodes(mat, img)
            
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.bake(type='ROUGHNESS')
            
            mat.node_tree.nodes.remove(bake_node)
    
    # Only save individual texture if explicitly requested
    if settings.get('save_individual_metallic_roughness', False):
        img.filepath_raw = str(textures_path / f"{output_name}_roughness.png")
        img.save()
        print(f"Saved: {img.filepath_raw}")

def bake_metallic():
    """Bake metallic using emission workaround"""
    print("\n--- Baking Metallic ---")
    img = created_images.get('metallic')
    if not img:
        return
    
    for obj in selected_objects:
        for mat_slot in obj.material_slots:
            if mat_slot.material:
                mat = mat_slot.material
                nodes = mat.node_tree.nodes
                links = mat.node_tree.links
                
                # Find Principled BSDF
                bsdf = None
                for node in nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        bsdf = node
                        break
                
                if bsdf:
                    # Find material output node
                    output_node = None
                    for node in nodes:
                        if node.type == 'OUTPUT_MATERIAL':
                            output_node = node
                            break
                    
                    # Store original output connection
                    original_output_links = []
                    if output_node and output_node.inputs['Surface'].is_linked:
                        for link in output_node.inputs['Surface'].links:
                            original_output_links.append((link.from_socket, link.to_socket))
                    
                    # Create emission node
                    emission = nodes.new(type='ShaderNodeEmission')
                    
                    # Connect metallic to emission
                    if bsdf.inputs['Metallic'].is_linked:
                        links.new(bsdf.inputs['Metallic'].links[0].from_socket, emission.inputs['Color'])
                    else:
                        emission.inputs['Color'].default_value = (bsdf.inputs['Metallic'].default_value,) * 3 + (1.0,)
                    
                    # Connect emission to output
                    if output_node:
                        # Disconnect current surface connection
                        if original_output_links:
                            links.remove(original_output_links[0][0].links[0])
                        # Connect emission to output
                        links.new(emission.outputs['Emission'], output_node.inputs['Surface'])
                    
                    # Add bake node
                    bake_node = setup_bake_nodes(mat, img)
                    
                    # Bake
                    bpy.context.view_layer.objects.active = obj
                    bpy.ops.object.bake(type='EMIT')
                    
                    # Cleanup - restore original connections
                    nodes.remove(emission)
                    nodes.remove(bake_node)
                    
                    # Restore original output connection
                    if output_node and original_output_links:
                        links.new(original_output_links[0][0], original_output_links[0][1])
    
    # Only save individual texture if explicitly requested
    if settings.get('save_individual_metallic_roughness', False):
        img.filepath_raw = str(textures_path / f"{output_name}_metallic.png")
        img.save()
        print(f"Saved: {img.filepath_raw}")

def bake_ao():
    """Bake ambient occlusion"""
    print("\n--- Baking Ambient Occlusion ---")
    img = created_images.get('ao')
    if not img:
        return
    
    # For AO, we need all objects selected to capture inter-object occlusion
    # First, set up bake nodes on all materials
    bake_nodes = []
    for obj in selected_objects:
        # Deselect all objects first
        bpy.ops.object.select_all(action='DESELECT')
        # Select only the object we're baking
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        
        if obj.material_slots:
            mat = obj.material_slots[0].material
            bake_node = setup_bake_nodes(mat, img)
            bake_nodes.append((mat, bake_node))
    
    # Select all objects for AO baking
    for obj in selected_objects:
        obj.select_set(True)
    
    # Set the first object as active
    if selected_objects:
        bpy.context.view_layer.objects.active = selected_objects[0]
        
        # Bake AO with all objects selected
        bpy.ops.object.bake(type='AO', use_selected_to_active=False)
    
    # Clean up bake nodes
    for mat, bake_node in bake_nodes:
        mat.node_tree.nodes.remove(bake_node)
    
    img.filepath_raw = str(textures_path / f"{output_name}_ao.png")
    img.save()
    print(f"Saved: {img.filepath_raw}")

# Execute baking
print("\n=== Starting Texture Baking ===")

if texture_maps['albedo']:
    bake_albedo()

if texture_maps['normal']:
    bake_normal()

if texture_maps['roughness']:
    bake_roughness()

if texture_maps['metallic']:
    bake_metallic()

if texture_maps['ambient_occlusion']:
    bake_ao()

# Create Bevy-compatible metallic-roughness texture if requested
if settings.get('export_metallic_roughness_packed', True) and all([texture_maps['roughness'], texture_maps['metallic']]):
    print("\n--- Creating Metallic-Roughness Packed Texture (Bevy/glTF format) ---")
    
    # Set render resolution to match texture resolution
    bpy.context.scene.render.resolution_x = resolution
    bpy.context.scene.render.resolution_y = resolution
    bpy.context.scene.render.resolution_percentage = 100
    
    # Enable compositor nodes
    bpy.context.scene.use_nodes = True
    
    tree = bpy.context.scene.node_tree
    nodes = tree.nodes
    links = tree.links
    
    # Clear existing nodes
    nodes.clear()
    
    # Load individual maps
    rough_node = nodes.new(type='CompositorNodeImage')
    rough_node.image = created_images['roughness']
    rough_node.location = (0, 100)
    
    metal_node = nodes.new(type='CompositorNodeImage')
    metal_node.image = created_images['metallic']
    metal_node.location = (0, -100)
    
    # Separate RGB nodes
    sep_rough = nodes.new(type='CompositorNodeSeparateColor')
    sep_rough.location = (200, 100)
    
    sep_metal = nodes.new(type='CompositorNodeSeparateColor')
    sep_metal.location = (200, -100)
    
    # Combine RGB
    combine = nodes.new(type='CompositorNodeCombineColor')
    combine.location = (400, 0)
    
    # Output
    output = nodes.new(type='CompositorNodeComposite')
    output.location = (600, 0)
    
    # Connect nodes
    links.new(rough_node.outputs['Image'], sep_rough.inputs['Image'])
    links.new(metal_node.outputs['Image'], sep_metal.inputs['Image'])
    
    # Bevy/glTF format: Roughness in Green, Metallic in Blue
    # Red channel is unused (set to 1.0)
    combine.inputs['Red'].default_value = 1.0  # Unused channel
    links.new(sep_rough.outputs['Red'], combine.inputs['Green'])   # Roughness in G
    links.new(sep_metal.outputs['Red'], combine.inputs['Blue'])   # Metallic in B
    combine.inputs['Alpha'].default_value = 1.0  # Full alpha
    
    links.new(combine.outputs['Image'], output.inputs['Image'])
    
    # Render to create metallic-roughness texture
    bpy.context.scene.render.filepath = str(textures_path / f"{output_name}_metallic_roughness.png")
    bpy.ops.render.render(write_still=True)
    print(f"Saved: {bpy.context.scene.render.filepath}")

# Create material with baked textures
print("\n--- Creating Baked Material ---")
baked_mat = bpy.data.materials.new(name=f"{output_name}_baked")
baked_mat.use_nodes = True

nodes = baked_mat.node_tree.nodes
links = baked_mat.node_tree.links
nodes.clear()

# Create nodes
tex_coord = nodes.new(type='ShaderNodeTexCoord')
tex_coord.location = (-800, 300)

if texture_maps['albedo']:
    albedo_tex = nodes.new(type='ShaderNodeTexImage')
    albedo_tex.location = (-600, 500)
    albedo_tex.image = created_images['albedo']
    albedo_tex.label = "Albedo"

if texture_maps['normal']:
    normal_tex = nodes.new(type='ShaderNodeTexImage')
    normal_tex.location = (-600, 200)
    normal_tex.image = created_images['normal']
    normal_tex.label = "Normal"
    normal_tex.image.colorspace_settings.name = 'Non-Color'
    
    normal_map = nodes.new(type='ShaderNodeNormalMap')
    normal_map.location = (-300, 200)

if settings.get('export_metallic_roughness_packed', True):
    mr_tex = nodes.new(type='ShaderNodeTexImage')
    mr_tex.location = (-600, -100)
    mr_tex.label = "Metallic-Roughness"
    # Load the saved metallic-roughness image
    mr_path = str(textures_path / f"{output_name}_metallic_roughness.png")
    if os.path.exists(mr_path):
        mr_img = bpy.data.images.load(mr_path)
        mr_tex.image = mr_img
        mr_img.colorspace_settings.name = 'Non-Color'
    
    separate_rgb = nodes.new(type='ShaderNodeSeparateColor')
    separate_rgb.location = (-300, -100)

bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
bsdf.location = (0, 300)

output = nodes.new(type='ShaderNodeOutputMaterial')
output.location = (300, 300)

# Connect nodes
if texture_maps['albedo']:
    links.new(tex_coord.outputs['UV'], albedo_tex.inputs['Vector'])
    links.new(albedo_tex.outputs['Color'], bsdf.inputs['Base Color'])

if texture_maps['normal']:
    links.new(tex_coord.outputs['UV'], normal_tex.inputs['Vector'])
    links.new(normal_tex.outputs['Color'], normal_map.inputs['Color'])
    links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])

if settings.get('export_metallic_roughness_packed', True) and 'mr_tex' in locals():
    links.new(tex_coord.outputs['UV'], mr_tex.inputs['Vector'])
    links.new(mr_tex.outputs['Color'], separate_rgb.inputs['Color'])
    links.new(separate_rgb.outputs['Green'], bsdf.inputs['Roughness'])  # Green = Roughness
    links.new(separate_rgb.outputs['Blue'], bsdf.inputs['Metallic'])   # Blue = Metallic

links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

# Apply baked material to objects
for obj_name in object_names:
    obj = bpy.data.objects[obj_name]
    # Only apply materials to mesh objects
    if obj.type == 'MESH' and obj.data:
        obj.data.materials.clear()
        obj.data.materials.append(baked_mat)

print(f"Applied baked material to objects")

# Export GLB if requested
if settings['export_glb']:
    print("\n--- Exporting GLB ---")
    
    # Select only our objects for export
    bpy.ops.object.select_all(action='DESELECT')
    for obj_name in object_names:
        obj = bpy.data.objects[obj_name]
        obj.select_set(True)
        # If it's an Empty with children, also select all children recursively
        if obj.type == 'EMPTY':
            def select_children(parent):
                for child in parent.children:
                    child.select_set(True)
                    select_children(child)
            select_children(obj)
    
    glb_path = str(output_path / f"{output_name}.glb")
    
    bpy.ops.export_scene.gltf(
        filepath=glb_path,
        export_format='GLB',
        use_selection=True,
        export_texcoords=True,
        export_normals=True,
        export_tangents=True,
        export_materials='EXPORT',
        export_image_format='AUTO'
    )
    
    print(f"Exported: {glb_path}")

# Generate manifest
manifest_path = output_path / "bake_manifest.txt"
with open(manifest_path, 'w') as f:
    f.write(f"PBR Texture Baking Manifest\n")
    f.write(f"===========================\n\n")
    f.write(f"Source: {blend_file}\n")
    f.write(f"Objects: {', '.join(object_names)}\n")
    f.write(f"Resolution: {resolution}x{resolution}\n")
    f.write(f"Bake Margin: {settings['bake_margin']}px\n\n")
    f.write(f"Generated Files:\n")
    
    for file in sorted(output_path.rglob("*")):
        if file.is_file() and file != manifest_path:
            f.write(f"  - {file.relative_to(output_path)}\n")

print(f"\n=== Baking Complete ===")
print(f"Output directory: {output_path}")
print(f"Manifest: {manifest_path}")