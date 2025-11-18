from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, TypedDict

import bpy  # pyright: ignore[reportMissingImports]


class ConfigDict(TypedDict):
    blend_file: str
    objects: list[str]
    output_name: str
    texture_resolution: int
    output_directory: str
    settings: dict[str, Any]
    texture_maps: dict[str, bool]


# Get configuration path from command line argument or use default
# In Blender, sys.argv includes all Blender's arguments
# Arguments after '--' separator are for the script
config_path: str | None = None
if '--' in sys.argv:
    idx = sys.argv.index('--')
    if idx + 1 < len(sys.argv):
        config_path = sys.argv[idx + 1]

if not config_path:
    # Default path for backward compatibility
    config_path = str(Path.home() / ".claude/commands/blender/bake_textures.json")

# Convert to absolute path
config_path = os.path.abspath(config_path)
config_dir = os.path.dirname(config_path)

print(f"Loading configuration from: {config_path}")

try:
    with open(config_path) as f:
        config: ConfigDict = json.load(f)  # pyright: ignore[reportAny]
except FileNotFoundError:
    print(f"ERROR: Configuration file not found at {config_path}")
    print("Usage: python bake_textures.py [path/to/bake_textures.json]")
    exit(1)
except json.JSONDecodeError as e:
    print(f"ERROR: Invalid JSON in configuration file: {e}")
    exit(1)

# Extract configuration - resolve relative paths from config directory
blend_file: str = config['blend_file']
if not os.path.isabs(blend_file):
    blend_file = os.path.join(config_dir, blend_file)
object_names: list[str] = config['objects']
output_name: str = config['output_name']
resolution: int = config['texture_resolution']
output_dir: str = config['output_directory']
if not os.path.isabs(output_dir):
    output_dir = os.path.join(config_dir, output_dir)
settings: dict[str, Any] = config['settings']
texture_maps: dict[str, bool] = config['texture_maps']

# Extract specific settings for clarity
bake_separate_per_object: bool = settings.get('bake_separate_per_object', True)
save_individual_metallic_roughness: bool = settings.get('save_individual_metallic_roughness', False)

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
    available_meshes = [obj.name for obj in bpy.data.objects if obj.type == 'MESH']
    print(f"Available objects: {', '.join(available_meshes)}")
    exit(1)

# Select target objects and their children
bpy.ops.object.select_all(action='DESELECT')


def select_with_children(obj: Any) -> None:  # pyright: ignore[reportAny]
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
    if platform.system() == 'Darwin':  # macOS
        prefs.compute_device_type = 'METAL'
    else:
        prefs.compute_device_type = 'CUDA'
    bpy.context.scene.cycles.device = 'GPU'
    print("GPU rendering enabled")

# Get or create material for selected objects - ONLY MESH objects
selected_objects = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
print(f"Mesh objects for baking: {[obj.name for obj in selected_objects]}")
if bpy.context.view_layer.objects.active:
    print(f"Current active object: {bpy.context.view_layer.objects.active.name}")
else:
    print("Current active object: None")

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

# Determine baking mode
bake_separate_per_object: bool = settings.get('bake_separate_per_object', False)

# Create image textures for baking
# Structure depends on baking mode:
# - Separate: created_images[obj_name][map_name] = image
# - Combined: created_images[map_name] = image
created_images: dict[str, Any] = {}


def create_bake_image(map_name: str, color_space: str = 'sRGB', obj_name: str | None = None) -> Any:  # pyright: ignore[reportAny]
    """Create a new image for baking

    Args:
        map_name: Type of texture map (e.g., 'albedo', 'normal')
        color_space: Color space setting ('sRGB' or 'Non-Color')
        obj_name: Object name (only used when bake_separate_per_object=True)
    """
    if bake_separate_per_object and obj_name:
        img_name = f"{output_name}_{obj_name}_{map_name}"
    else:
        img_name = f"{output_name}_{map_name}"

    img = bpy.data.images.new(
        name=img_name,
        width=resolution,
        height=resolution,
        float_buffer=(map_name == 'normal')
    )
    img.colorspace_settings.name = color_space

    # Store in appropriate structure
    if bake_separate_per_object and obj_name:
        if obj_name not in created_images:
            created_images[obj_name] = {}
        created_images[obj_name][map_name] = img
    else:
        created_images[map_name] = img

    print(f"Created image: {img_name} ({resolution}x{resolution}, {color_space})")
    return img


# Create images based on baking mode
print(f"\n=== Creating Bake Images (mode: {'separate per object' if bake_separate_per_object else 'combined'}) ===")

if bake_separate_per_object:
    # Create separate images for each object
    for obj in selected_objects:
        print(f"\nImages for '{obj.name}':")
        if texture_maps['albedo']:
            create_bake_image('albedo', 'sRGB', obj.name)
        if texture_maps['normal']:
            create_bake_image('normal', 'Non-Color', obj.name)

        if settings.get('export_metallic_roughness_packed', True):
            if texture_maps['roughness']:
                create_bake_image('roughness', 'Non-Color', obj.name)
            if texture_maps['metallic']:
                create_bake_image('metallic', 'Non-Color', obj.name)
        else:
            if texture_maps['roughness']:
                create_bake_image('roughness', 'Non-Color', obj.name)
            if texture_maps['metallic']:
                create_bake_image('metallic', 'Non-Color', obj.name)

        if texture_maps['ambient_occlusion']:
            create_bake_image('ao', 'Non-Color', obj.name)
        if texture_maps['emission']:
            create_bake_image('emission', 'sRGB', obj.name)

    total_images = sum(len(images) for images in created_images.values())
    print(f"\nCreated {total_images} texture images for {len(created_images)} objects")
else:
    # Create shared images for all objects (original behavior)
    print("\nShared images for all objects:")
    if texture_maps['albedo']:
        create_bake_image('albedo', 'sRGB')
    if texture_maps['normal']:
        create_bake_image('normal', 'Non-Color')

    if settings.get('export_metallic_roughness_packed', True):
        if texture_maps['roughness']:
            create_bake_image('roughness', 'Non-Color')
        if texture_maps['metallic']:
            create_bake_image('metallic', 'Non-Color')
    else:
        if texture_maps['roughness']:
            create_bake_image('roughness', 'Non-Color')
        if texture_maps['metallic']:
            create_bake_image('metallic', 'Non-Color')

    if texture_maps['ambient_occlusion']:
        create_bake_image('ao', 'Non-Color')
    if texture_maps['emission']:
        create_bake_image('emission', 'sRGB')

    print(f"\nCreated {len(created_images)} shared texture images")


# Helper functions for mode-aware image handling
def get_bake_image(map_name: str, obj_name: str | None = None) -> Any:  # pyright: ignore[reportAny]
    """Get the appropriate bake image based on mode"""
    if bake_separate_per_object and obj_name:
        return created_images.get(obj_name, {}).get(map_name)
    else:
        return created_images.get(map_name)


def save_bake_image(image: Any, map_name: str, obj_name: str | None = None) -> None:  # pyright: ignore[reportAny]
    """Save the bake image with appropriate filename"""
    if bake_separate_per_object and obj_name:
        filename = f"{output_name}_{obj_name}_{map_name}.png"
    else:
        filename = f"{output_name}_{map_name}.png"
    image.filepath_raw = str(textures_path / filename)
    image.save()
    print(f"Saved: {image.filepath_raw}")


# Setup material nodes for baking
def setup_bake_nodes(material: Any, image: Any) -> Any:  # pyright: ignore[reportAny]
    """Add image texture node for baking"""
    nodes = material.node_tree.nodes

    # Create image texture node
    img_node = nodes.new(type='ShaderNodeTexImage')
    img_node.image = image
    img_node.location = (-400, 0)
    img_node.select = True
    nodes.active = img_node

    return img_node


# Core baking helper functions
def fill_image_with_value(img: Any, value: float) -> None:  # pyright: ignore[reportAny]
    """Fill an image with a solid scalar value - avoids circular dependencies"""
    # Create flat list of pixel values (RGBA)
    pixel_count = img.size[0] * img.size[1]
    pixels = [value, value, value, 1.0] * pixel_count
    img.pixels = pixels


def bake_with_emission_workaround(
    obj: Any,  # pyright: ignore[reportAny]
    img: Any,  # pyright: ignore[reportAny]
    source_input_name: str,
) -> None:
    """Bake using emission workaround for non-bake properties like albedo or metallic"""
    # Set up emission for ALL materials before baking
    temp_nodes = []
    bake_nodes = []

    for mat_slot in obj.material_slots:
        if not mat_slot.material or not mat_slot.material.use_nodes:
            continue

        mat = mat_slot.material
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        # Find Principled BSDF
        bsdf = None
        for node in nodes:
            if node.type == 'BSDF_PRINCIPLED':
                bsdf = node
                break

        if not bsdf:
            continue

        # Store original source connection
        source_links = []
        if bsdf.inputs[source_input_name].is_linked:
            for link in bsdf.inputs[source_input_name].links:
                source_links.append((link.from_socket, link.to_socket))

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
                links.remove(link)

        # Create emission node
        emission = nodes.new(type='ShaderNodeEmission')
        emission.location = (bsdf.location[0], bsdf.location[1] - 200)

        # Connect source to emission
        if source_links:
            links.new(source_links[0][0], emission.inputs['Color'])
        elif source_input_name == 'Base Color':
            emission.inputs['Color'].default_value = bsdf.inputs[source_input_name].default_value
        else:
            # For metallic/roughness with no source link: convert scalar to color
            scalar_value = bsdf.inputs[source_input_name].default_value
            emission.inputs['Color'].default_value = (scalar_value,) * 3 + (1.0,)

        # Connect emission to output
        if output_node:
            links.new(emission.outputs['Emission'], output_node.inputs['Surface'])

        # Store for cleanup
        temp_nodes.append((mat, emission, original_output_links))

        # Add bake node
        bake_node = setup_bake_nodes(mat, img)
        bake_nodes.append((mat, bake_node))

    # Bake once with all materials set up
    # Deselect all objects and select only the target object
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.bake(type='EMIT')

    # Cleanup - remove temporary nodes from all materials
    for mat, bake_node in bake_nodes:
        mat.node_tree.nodes.remove(bake_node)

    for mat, emission, original_output_links in temp_nodes:
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.remove(emission)
        # Restore original output connection
        for from_socket, to_socket in original_output_links:
            links.new(from_socket, to_socket)


def bake_simple(obj: Any, img: Any, bake_type: str) -> None:  # pyright: ignore[reportAny]
    """Simple bake operation for types that support direct baking"""
    # Deselect all objects first
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    if obj.material_slots:
        # Set up bake nodes for ALL materials on the object
        bake_nodes = []
        for mat_slot in obj.material_slots:
            if mat_slot.material:
                bake_node = setup_bake_nodes(mat_slot.material, img)
                bake_nodes.append((mat_slot.material, bake_node))

        if bake_type == 'NORMAL':
            bpy.ops.object.bake(type='NORMAL', normal_space='TANGENT')
        elif bake_type == 'ROUGHNESS':
            bpy.ops.object.bake(type='ROUGHNESS')
        elif bake_type == 'AO':
            bpy.ops.object.bake(type='AO', use_selected_to_active=False)
        elif bake_type == 'DIFFUSE':
            bpy.ops.object.bake(type='DIFFUSE')
        elif bake_type == 'METALLIC':
            bpy.ops.object.bake(type='METALLIC')

        # Clean up bake nodes from all materials
        for mat, bake_node in bake_nodes:
            mat.node_tree.nodes.remove(bake_node)


# Generic baking function that handles mode branching
def bake_texture_map(
    map_name: str,
    bake_fn: Any,  # Callable[[Any, Any], None] but avoid complex types  # pyright: ignore[reportAny]
    should_save: bool = True,
) -> None:
    """Generic texture map baking with mode-aware iteration

    Args:
        map_name: Name of the texture map (albedo, normal, etc.)
        bake_fn: Function to call for baking each object. Signature: (obj, img) -> None
        should_save: Whether to save individual textures (for roughness/metallic)
    """
    print(f"\n--- Baking {map_name.title()} ---")

    if bake_separate_per_object:
        # Separate mode: bake each object to its own image
        for obj in selected_objects:
            img = get_bake_image(map_name, obj.name)
            if not img:
                continue

            bake_fn(obj, img)
            if should_save:
                save_bake_image(img, map_name, obj.name)
    else:
        # Combined mode: all objects bake to one shared image
        img = get_bake_image(map_name)
        if not img:
            return

        for obj in selected_objects:
            bake_fn(obj, img)

        if should_save:
            save_bake_image(img, map_name)


# Baking functions using the generic pattern
def bake_albedo() -> None:
    """Bake albedo/base color using DIFFUSE bake type"""
    def bake_fn(obj: Any, img: Any) -> None:  # pyright: ignore[reportAny]
        # Always use EMIT baking to capture base color without lighting
        print(f"  Object '{obj.name}' using EMIT baking for albedo")
        bake_albedo_with_emission(obj, img)

    bake_texture_map('albedo', bake_fn)


def bake_albedo_with_emission(obj: Any, img: Any) -> None:  # pyright: ignore[reportAny]
    """Bake albedo using EMIT for objects with vertex colors"""
    # Deselect all objects first
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    # Set up temporary emission nodes for each material
    temp_nodes = []

    for mat_slot in obj.material_slots:
        if not mat_slot.material or not mat_slot.material.use_nodes:
            continue

        mat = mat_slot.material
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        # Find BSDF and output
        bsdf = None
        output = None
        for node in nodes:
            if node.type == 'BSDF_PRINCIPLED':
                bsdf = node
            elif node.type == 'OUTPUT_MATERIAL':
                output = node

        if not bsdf or not output:
            continue

        # Store original connection
        original_links = []
        if output.inputs['Surface'].is_linked:
            for link in output.inputs['Surface'].links:
                original_links.append((link.from_socket, link.to_socket))
                links.remove(link)

        # Create emission node
        emission = nodes.new(type='ShaderNodeEmission')
        emission.location = (bsdf.location[0], bsdf.location[1] - 200)

        # Connect Base Color source to emission
        base_color_input = bsdf.inputs['Base Color']
        if base_color_input.is_linked:
            source_socket = base_color_input.links[0].from_socket
            links.new(source_socket, emission.inputs['Color'])
        else:
            emission.inputs['Color'].default_value = base_color_input.default_value

        # Connect emission to output
        links.new(emission.outputs['Emission'], output.inputs['Surface'])

        # Store for cleanup
        temp_nodes.append((mat, emission, original_links))

    # Set up bake nodes for all materials
    bake_nodes = []
    for mat_slot in obj.material_slots:
        if mat_slot.material:
            bake_node = setup_bake_nodes(mat_slot.material, img)
            bake_nodes.append((mat_slot.material, bake_node))

    # Bake using EMIT
    bpy.ops.object.bake(type='EMIT')

    # Clean up bake nodes
    for mat, bake_node in bake_nodes:
        mat.node_tree.nodes.remove(bake_node)

    # Restore original material connections
    for mat, emission, original_links in temp_nodes:
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        # Remove emission node
        nodes.remove(emission)

        # Restore original links
        for from_socket, to_socket in original_links:
            links.new(from_socket, to_socket)


def bake_normal() -> None:
    """Bake normal map"""
    def bake_fn(obj: Any, img: Any) -> None:  # pyright: ignore[reportAny]
        bake_simple(obj, img, 'NORMAL')

    bake_texture_map('normal', bake_fn)


def bake_pbr_property(socket_name: str, should_save: bool) -> None:
    """Bake a PBR property (roughness/metallic) using emission workaround

    Args:
        socket_name: Blender socket name (e.g., 'Roughness', 'Metallic')
        should_save: Whether to save individual texture files
    """
    property_name = socket_name.lower()  # Convert to lowercase for file naming

    def bake_fn(obj: Any, img: Any) -> None:  # pyright: ignore[reportAny]
        bake_with_emission_workaround(obj, img, socket_name)

    bake_texture_map(property_name, bake_fn, should_save=should_save)


def bake_roughness() -> None:
    """Bake roughness map using emission workaround"""
    bake_pbr_property('Roughness', save_individual_metallic_roughness)


def bake_metallic() -> None:
    """Bake metallic using emission workaround"""
    bake_pbr_property('Metallic', save_individual_metallic_roughness)


def bake_ao() -> None:
    """Bake ambient occlusion - special handling for inter-object occlusion"""
    print("\n--- Baking Ambient Occlusion ---")

    if bake_separate_per_object:
        # Separate mode: bake each object's AO separately
        for target_obj in selected_objects:
            img = get_bake_image('ao', target_obj.name)
            if not img:
                continue

            # Set up bake node ONLY on the target object
            if not target_obj.material_slots:
                continue

            mat = target_obj.material_slots[0].material
            bake_node = setup_bake_nodes(mat, img)

            # Select only the target object for baking
            bpy.ops.object.select_all(action='DESELECT')
            target_obj.select_set(True)

            # Bake with only target object selected
            bpy.context.view_layer.objects.active = target_obj
            bpy.ops.object.bake(type='AO', use_selected_to_active=False)

            # Clean up bake node
            mat.node_tree.nodes.remove(bake_node)

            # Save this object's AO texture
            save_bake_image(img, 'ao', target_obj.name)
    else:
        # Combined mode: all objects bake to one shared AO image
        img = get_bake_image('ao')
        if not img:
            return

        # Set up bake nodes on all materials
        bake_nodes = []
        for obj in selected_objects:
            if obj.material_slots:
                mat = obj.material_slots[0].material
                bake_node = setup_bake_nodes(mat, img)
                bake_nodes.append((mat, bake_node))

        # Select all objects for AO baking
        bpy.ops.object.select_all(action='DESELECT')
        for obj in selected_objects:
            obj.select_set(True)

        # Bake AO with all objects selected
        if selected_objects:
            bpy.context.view_layer.objects.active = selected_objects[0]
            bpy.ops.object.bake(type='AO', use_selected_to_active=False)

        # Clean up bake nodes
        for mat, bake_node in bake_nodes:
            mat.node_tree.nodes.remove(bake_node)

        # Save once after all objects have been baked
        save_bake_image(img, 'ao')


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

    # Save current view transform and switch to Standard for accurate data texture rendering
    # (AgX/Filmic tone mapping would alter pixel values)
    original_view_transform = bpy.context.scene.view_settings.view_transform
    bpy.context.scene.view_settings.view_transform = 'Standard'

    # Save current file format settings and configure for linear output
    original_file_format = bpy.context.scene.render.image_settings.file_format
    original_color_mode = bpy.context.scene.render.image_settings.color_mode
    original_color_depth = bpy.context.scene.render.image_settings.color_depth
    original_compression = bpy.context.scene.render.image_settings.compression

    # Set to PNG with no color management (saves raw linear data)
    bpy.context.scene.render.image_settings.file_format = 'PNG'
    bpy.context.scene.render.image_settings.color_mode = 'RGBA'
    bpy.context.scene.render.image_settings.color_depth = '16'  # 16-bit for precision
    bpy.context.scene.render.image_settings.compression = 15  # Max compression

    # Determine which objects to pack
    objects_to_pack = [obj.name for obj in selected_objects] if bake_separate_per_object else [None]

    for obj_name in objects_to_pack:
        tree = bpy.context.scene.node_tree
        nodes = tree.nodes
        links = tree.links

        # Clear existing nodes
        nodes.clear()

        # Get the roughness and metallic images for this object
        if bake_separate_per_object and obj_name is not None:
            rough_img = created_images[obj_name]['roughness']
            metal_img = created_images[obj_name]['metallic']
            output_filename = f"{output_name}_{obj_name}_metallic_roughness.png"
            print(f"  Packing metallic-roughness for {obj_name}")
        else:
            rough_img = created_images['roughness']
            metal_img = created_images['metallic']
            output_filename = f"{output_name}_metallic_roughness.png"

        # Load individual maps
        # Ensure roughness and metallic use Non-Color colorspace (data textures, not colors)
        rough_img.colorspace_settings.name = 'Non-Color'
        metal_img.colorspace_settings.name = 'Non-Color'

        rough_node = nodes.new(type='CompositorNodeImage')
        rough_node.image = rough_img
        rough_node.location = (0, 100)

        metal_node = nodes.new(type='CompositorNodeImage')
        metal_node.image = metal_img
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
        bpy.context.scene.render.filepath = str(textures_path / output_filename)
        bpy.ops.render.render(write_still=True)
        print(f"  Saved: {bpy.context.scene.render.filepath}")

    # Restore original view transform and file format settings
    bpy.context.scene.view_settings.view_transform = original_view_transform
    bpy.context.scene.render.image_settings.file_format = original_file_format
    bpy.context.scene.render.image_settings.color_mode = original_color_mode
    bpy.context.scene.render.image_settings.color_depth = original_color_depth
    bpy.context.scene.render.image_settings.compression = original_compression

# Create material with baked textures
print("\n--- Creating Baked Material ---")

if bake_separate_per_object:
    # Create separate materials for each object
    for obj_name in object_names:
        obj = bpy.data.objects.get(obj_name)
        if not obj or obj.type != 'MESH' or not obj.data:
            continue

        baked_mat = bpy.data.materials.new(name=f"{output_name}_{obj_name}_baked")
        baked_mat.use_nodes = True

        nodes = baked_mat.node_tree.nodes
        links = baked_mat.node_tree.links
        nodes.clear()

        # Create nodes
        tex_coord = nodes.new(type='ShaderNodeTexCoord')
        tex_coord.location = (-800, 300)

        # Create texture nodes conditionally and store references
        albedo_tex = None
        if texture_maps['albedo'] and obj_name in created_images and 'albedo' in created_images[obj_name]:
            albedo_tex = nodes.new(type='ShaderNodeTexImage')
            albedo_tex.location = (-600, 500)
            albedo_tex.image = created_images[obj_name]['albedo']
            albedo_tex.label = "Albedo"

        normal_tex = None
        normal_map = None
        if texture_maps['normal'] and obj_name in created_images and 'normal' in created_images[obj_name]:
            normal_tex = nodes.new(type='ShaderNodeTexImage')
            normal_tex.location = (-600, 200)
            normal_tex.image = created_images[obj_name]['normal']
            normal_tex.label = "Normal"
            normal_tex.image.colorspace_settings.name = 'Non-Color'

            normal_map = nodes.new(type='ShaderNodeNormalMap')
            normal_map.location = (-300, 200)

        mr_tex = None
        separate_rgb = None
        if settings.get('export_metallic_roughness_packed', True):
            mr_tex = nodes.new(type='ShaderNodeTexImage')
            mr_tex.location = (-600, -100)
            mr_tex.label = "Metallic-Roughness"
            # Load the saved metallic-roughness image
            mr_path = str(textures_path / f"{output_name}_{obj_name}_metallic_roughness.png")
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
        if albedo_tex:
            links.new(tex_coord.outputs['UV'], albedo_tex.inputs['Vector'])
            links.new(albedo_tex.outputs['Color'], bsdf.inputs['Base Color'])

        if normal_tex and normal_map:
            links.new(tex_coord.outputs['UV'], normal_tex.inputs['Vector'])
            links.new(normal_tex.outputs['Color'], normal_map.inputs['Color'])
            links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])

        if mr_tex and separate_rgb:
            links.new(tex_coord.outputs['UV'], mr_tex.inputs['Vector'])
            links.new(mr_tex.outputs['Color'], separate_rgb.inputs['Color'])
            links.new(separate_rgb.outputs['Green'], bsdf.inputs['Roughness'])  # Green = Roughness
            links.new(separate_rgb.outputs['Blue'], bsdf.inputs['Metallic'])   # Blue = Metallic

        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

        # Apply material to this object
        obj.data.materials.clear()
        obj.data.materials.append(baked_mat)

        print(f"Created and applied material for {obj_name}")
else:
    # Create single shared material for combined mode
    baked_mat = bpy.data.materials.new(name=f"{output_name}_baked")
    baked_mat.use_nodes = True

    nodes = baked_mat.node_tree.nodes
    links = baked_mat.node_tree.links
    nodes.clear()

    # Create nodes
    tex_coord = nodes.new(type='ShaderNodeTexCoord')
    tex_coord.location = (-800, 300)

    # Create texture nodes conditionally and store references
    albedo_tex = None
    if texture_maps['albedo']:
        albedo_tex = nodes.new(type='ShaderNodeTexImage')
        albedo_tex.location = (-600, 500)
        albedo_tex.image = created_images['albedo']
        albedo_tex.label = "Albedo"

    normal_tex = None
    normal_map = None
    if texture_maps['normal']:
        normal_tex = nodes.new(type='ShaderNodeTexImage')
        normal_tex.location = (-600, 200)
        normal_tex.image = created_images['normal']
        normal_tex.label = "Normal"
        normal_tex.image.colorspace_settings.name = 'Non-Color'

        normal_map = nodes.new(type='ShaderNodeNormalMap')
        normal_map.location = (-300, 200)

    mr_tex = None
    separate_rgb = None
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
    if albedo_tex:
        links.new(tex_coord.outputs['UV'], albedo_tex.inputs['Vector'])
        links.new(albedo_tex.outputs['Color'], bsdf.inputs['Base Color'])

    if normal_tex and normal_map:
        links.new(tex_coord.outputs['UV'], normal_tex.inputs['Vector'])
        links.new(normal_tex.outputs['Color'], normal_map.inputs['Color'])
        links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])

    if mr_tex and separate_rgb:
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

    print("Applied baked material to objects")

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
            def select_children(parent: Any) -> None:  # pyright: ignore[reportAny]
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
    _ = f.write("PBR Texture Baking Manifest\n")
    _ = f.write("===========================\n\n")
    _ = f.write(f"Source: {blend_file}\n")
    _ = f.write(f"Objects: {', '.join(object_names)}\n")
    _ = f.write(f"Resolution: {resolution}x{resolution}\n")
    _ = f.write(f"Bake Margin: {settings['bake_margin']}px\n\n")
    _ = f.write("Generated Files:\n")

    for file in sorted(output_path.rglob("*")):
        if file.is_file() and file != manifest_path:
            _ = f.write(f"  - {file.relative_to(output_path)}\n")

print(f"\n=== Baking Complete ===")
print(f"Output directory: {output_path}")
print(f"Manifest: {manifest_path}")
