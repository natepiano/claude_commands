from __future__ import annotations

import json
import os
import platform
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol, TypedDict

import bpy  # type: ignore[import-not-found]

# === TYPE DEFINITIONS ===


class ConfigDict(TypedDict):
    blend_file: str
    objects: list[str]
    output_name: str
    texture_resolution: int
    output_directory: str
    settings: dict[str, Any]
    texture_maps: dict[str, bool]


class BlenderObject(Protocol):
    """Protocol for Blender objects"""

    name: str
    type: str
    children: Any
    material_slots: Any
    data: Any

    def select_set(self, state: bool) -> None: ...


class BlenderImage(Protocol):
    """Protocol for Blender images"""

    name: str
    size: tuple[int, int]
    pixels: Any
    filepath_raw: str
    colorspace_settings: Any

    def save(self) -> None: ...


class BlenderMaterial(Protocol):
    """Protocol for Blender materials"""

    name: str
    use_nodes: bool
    node_tree: Any


# === CONFIGURATION LOADING ===


def get_config_path() -> str | None:
    """Extract config path from command line"""
    if "--" in sys.argv:
        idx = sys.argv.index("--")
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return None


def resolve_path(path: str, base_dir: str) -> str:
    """Resolve relative paths against base directory"""
    if os.path.isabs(path):
        return path
    return os.path.join(base_dir, path)


def generate_name(suffix: str, obj_name: str | None = None, extension: str = "") -> str:
    """Generate consistent names for images, materials, files

    Args:
        suffix: The suffix/type (e.g., 'albedo', 'normal', 'baked')
        obj_name: Object name for separate mode, None for combined mode
        extension: Optional file extension (e.g., '.png')

    Returns:
        Formatted name string

    Examples:
        >>> generate_name("albedo", "donut", ".png")
        "nateroid_donut_albedo.png"
        >>> generate_name("baked", None)
        "nateroid_baked"
    """
    # Note: output_name is a global variable set at module load time
    if obj_name:
        name = f"{output_name}_{obj_name}_{suffix}"
    else:
        name = f"{output_name}_{suffix}"
    return f"{name}{extension}" if extension else name


def get_image_for_mode(map_name: str, obj_name: str | None) -> Any:
    """Get image from created_images based on mode

    Args:
        map_name: The texture map name (e.g., 'albedo', 'roughness', 'metallic')
        obj_name: Object name for separate mode, None for combined mode

    Returns:
        Blender Image object or None

    Examples:
        >>> get_image_for_mode("albedo", "donut")  # separate mode
        <Image 'nateroid_donut_albedo'>
        >>> get_image_for_mode("albedo", None)  # combined mode
        <Image 'nateroid_albedo'>
    """
    # Note: created_images is a global dict populated during image creation
    if obj_name:
        return created_images.get(obj_name, {}).get(map_name)
    else:
        return created_images.get(map_name)


def load_configuration() -> tuple[ConfigDict, str]:
    """Load and validate configuration from command line

    Returns:
        Tuple of (config dict, config directory path)
    """
    config_path_str = get_config_path()

    if config_path_str is None:
        example_path = Path.home() / ".claude/config/bake_textures_example.json"
        print("ERROR: No configuration file specified")
        print("\nUsage:")
        print("  blender --background --python bake_textures.py -- /path/to/your_config.json")
        print(f"\nExample configuration available at:")
        print(f"  {example_path}")
        print("\nCopy and modify the example to create your own configuration.")
        sys.exit(1)

    config_path = os.path.abspath(config_path_str)
    config_dir = os.path.dirname(config_path)
    print(f"Loading configuration from: {config_path}")

    try:
        with open(config_path) as f:
            config: ConfigDict = json.load(f)
        return config, config_dir
    except FileNotFoundError:
        print(f"ERROR: Configuration file not found at {config_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in configuration file: {e}")
        sys.exit(1)


# === GLOBAL STATE ===
# (Loaded once at startup)

config, config_dir = load_configuration()

# Resolve paths
blend_file = resolve_path(config["blend_file"], config_dir)
output_dir = resolve_path(config["output_directory"], config_dir)

object_names: list[str] = config["objects"]
output_name: str = config["output_name"]
resolution: int = config["texture_resolution"]
settings: dict[str, Any] = config["settings"]
texture_maps: dict[str, bool] = config["texture_maps"]

# Mode settings
bake_separate_per_object: bool = settings.get("bake_separate_per_object", True)
save_individual_metallic_roughness: bool = settings.get(
    "save_individual_metallic_roughness", False
)

# Output paths
output_path = Path(output_dir)
textures_path = output_path / "textures"

# Image storage
# Separate mode: created_images[obj_name][map_name] = image
# Combined mode: created_images[map_name] = image
created_images: dict[str, Any] = {}

# Selected objects (populated during setup)
selected_objects: list[Any] = []

print("\n=== Configuration ===")
print(f"Blend file: {blend_file}")
print(f"Objects: {', '.join(object_names)}")
print(f"Output name: {output_name}")
print(f"Resolution: {resolution}x{resolution}")
print(f"Output directory: {output_dir}")
print(f"Mode: {'Separate per object' if bake_separate_per_object else 'Combined'}")


# === BLENDER SETUP ===


def setup_blender_environment() -> None:
    """Initialize Blender environment: open file, select objects, configure rendering"""
    global selected_objects

    # Validate blend file
    if not os.path.exists(blend_file):
        print(f"ERROR: Blend file not found: {blend_file}")
        sys.exit(1)

    # Create output directories
    output_path.mkdir(parents=True, exist_ok=True)
    textures_path.mkdir(exist_ok=True)
    print(f"\nCreated output directories at: {output_path}")

    # Open blend file
    print(f"\nOpening blend file: {blend_file}")
    bpy.ops.wm.open_mainfile(filepath=blend_file)

    # Validate objects exist
    missing_objects = [name for name in object_names if name not in bpy.data.objects]
    if missing_objects:
        print(f"ERROR: Objects not found in scene: {', '.join(missing_objects)}")
        available_meshes = [obj.name for obj in bpy.data.objects if obj.type == "MESH"]
        print(f"Available objects: {', '.join(available_meshes)}")
        sys.exit(1)

    # Select target objects and their children
    bpy.ops.object.select_all(action="DESELECT")

    def select_with_children(obj: Any) -> None:
        """Recursively select object and all its children"""
        obj.select_set(True)
        for child in obj.children:
            select_with_children(child)

    for obj_name in object_names:
        obj = bpy.data.objects[obj_name]
        select_with_children(obj)
        bpy.context.view_layer.objects.active = obj

    # Get list of all selected mesh objects
    selected_objects = [
        obj for obj in bpy.context.selected_objects if obj.type == "MESH"
    ]
    print(
        f"\nSelected mesh objects for baking: {[obj.name for obj in selected_objects]}"
    )

    # Set a mesh object as active
    if selected_objects:
        bpy.context.view_layer.objects.active = selected_objects[0]

    # Configure rendering
    bpy.context.scene.render.engine = "CYCLES"
    bpy.context.scene.cycles.bake_margin = settings["bake_margin"]

    if settings.get("use_gpu"):
        prefs = bpy.context.preferences.addons["cycles"].preferences
        if platform.system() == "Darwin":
            prefs.compute_device_type = "METAL"
        else:
            prefs.compute_device_type = "CUDA"
        bpy.context.scene.cycles.device = "GPU"
        print("GPU rendering enabled")

    # Validate materials
    for obj in selected_objects:
        if not obj.data.materials:
            print(f"ERROR: Object {obj.name} has no material")
            sys.exit(1)

    # Ensure UV maps
    print("\n--- Checking UV Maps ---")
    for obj in selected_objects:
        if not obj.data.uv_layers:
            print(f"Creating UV map for {obj.name}")
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project(angle_limit=66.0, island_margin=0.02)
            bpy.ops.object.mode_set(mode="OBJECT")
        else:
            print(f"{obj.name} already has UV map: {obj.data.uv_layers[0].name}")


# === LAYER 1: IMAGE MANAGEMENT ===


def create_bake_image(map_name: str, obj_name: str | None = None) -> Any:
    """Create a new image for baking

    Args:
        map_name: Type of texture map (albedo, normal, roughness, metallic, ao)
        obj_name: Object name (required in separate mode, None in combined mode)

    Returns:
        Blender Image object
    """
    # Determine image name
    img_name = generate_name(map_name, obj_name)

    # Determine color space
    color_space = "sRGB" if map_name in ("albedo", "emission") else "Non-Color"

    # Create image
    img = bpy.data.images.new(
        name=img_name,
        width=resolution,
        height=resolution,
        float_buffer=(map_name == "normal"),
    )
    img.colorspace_settings.name = color_space

    # Store in appropriate structure
    if obj_name:
        if obj_name not in created_images:
            created_images[obj_name] = {}
        created_images[obj_name][map_name] = img
    else:
        created_images[map_name] = img

    print(f"  Created: {img_name} ({resolution}x{resolution}, {color_space})")
    return img


def get_bake_image(map_name: str, obj_name: str | None = None) -> Any | None:
    """Get the appropriate bake image for a map type

    Args:
        map_name: Type of texture map
        obj_name: Object name (used in separate mode)

    Returns:
        Blender Image object or None if not found
    """
    if obj_name:
        return created_images.get(obj_name, {}).get(map_name)
    else:
        return created_images.get(map_name)


def save_bake_image(image: Any, map_name: str, obj_name: str | None = None) -> None:
    """Save a baked image to disk

    Args:
        image: Blender Image object
        map_name: Type of texture map
        obj_name: Object name (used in separate mode)
    """
    filename = generate_name(map_name, obj_name, ".png")

    image.filepath_raw = str(textures_path / filename)
    image.save()
    print(f"  Saved: {image.filepath_raw}")


# === LAYER 2: PRIMITIVE BAKING OPERATIONS ===


def setup_bake_nodes(material: Any, image: Any) -> Any:
    """Add image texture node for baking to a material

    Args:
        material: Blender Material
        image: Blender Image to bake to

    Returns:
        Created image texture node
    """
    nodes = material.node_tree.nodes
    img_node = nodes.new(type="ShaderNodeTexImage")
    img_node.image = image
    img_node.select = True
    nodes.active = img_node
    return img_node


@contextmanager
def bake_nodes_context(
    materials: list[Any], img: Any
) -> Iterator[list[tuple[Any, Any]]]:
    """Context manager for bake node setup and cleanup

    Args:
        materials: List of Blender Materials
        img: Blender Image to bake to

    Yields:
        List of (material, bake_node) tuples
    """
    bake_nodes: list[tuple[Any, Any]] = []
    try:
        for mat in materials:
            bake_node = setup_bake_nodes(mat, img)
            bake_nodes.append((mat, bake_node))
        yield bake_nodes
    finally:
        for mat, bake_node in bake_nodes:
            mat.node_tree.nodes.remove(bake_node)


def select_only(obj: Any) -> None:
    """Select only the specified object, deselecting all others"""
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def select_all(objects: list[Any]) -> None:
    """Select all specified objects"""
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]


def bake_object_to_image(obj: Any, img: Any, map_type: str) -> None:
    """Bake a standard map type for one object to an image

    This handles albedo, normal, roughness, and metallic.
    AO requires special handling (see bake_ao_separate/combined).

    Args:
        obj: Blender Object to bake
        img: Blender Image to bake to
        map_type: Type of map (albedo, normal, roughness, metallic)
    """
    select_only(obj)

    if map_type == "albedo":
        bake_with_emission_workaround(obj, img, "Base Color")
    elif map_type == "normal":
        bake_simple(obj, img, "NORMAL")
    elif map_type in ("roughness", "metallic"):
        bake_with_emission_workaround(obj, img, map_type.title())
    else:
        print(f"  WARNING: Unknown map type '{map_type}'")


def bake_simple(obj: Any, img: Any, bake_type: str) -> None:
    """Simple bake operation for types that support direct baking

    Args:
        obj: Blender Object
        img: Blender Image
        bake_type: Blender bake type (NORMAL, ROUGHNESS, DIFFUSE, etc.)
    """
    materials = [slot.material for slot in obj.material_slots if slot.material]
    with bake_nodes_context(materials, img):
        # Bake
        if bake_type == "NORMAL":
            bpy.ops.object.bake(type="NORMAL", normal_space="TANGENT")
        elif bake_type == "ROUGHNESS":
            bpy.ops.object.bake(type="ROUGHNESS")
        elif bake_type == "DIFFUSE":
            bpy.ops.object.bake(type="DIFFUSE")
        elif bake_type == "METALLIC":
            bpy.ops.object.bake(type="METALLIC")
        else:
            bpy.ops.object.bake(type=bake_type)


def bake_with_emission_workaround(obj: Any, img: Any, source_input_name: str) -> None:
    """Bake using emission workaround for properties like Roughness/Metallic

    This connects the source input to an Emission shader and bakes using EMIT.

    Args:
        obj: Blender Object
        img: Blender Image
        source_input_name: Name of BSDF input socket (e.g., 'Roughness', 'Metallic')
    """
    temp_nodes: list[tuple[Any, Any, list[tuple[Any, Any]]]] = []

    # Set up emission for all materials
    for mat_slot in obj.material_slots:
        if not mat_slot.material or not mat_slot.material.use_nodes:
            continue

        mat = mat_slot.material
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        # Find Principled BSDF
        bsdf = None
        for node in nodes:
            if node.type == "BSDF_PRINCIPLED":
                bsdf = node
                break

        if not bsdf:
            continue

        # Store original source connection
        source_links: list[tuple[Any, Any]] = []
        if bsdf.inputs[source_input_name].is_linked:
            for link in bsdf.inputs[source_input_name].links:
                source_links.append((link.from_socket, link.to_socket))

        # Find material output
        output_node = None
        for node in nodes:
            if node.type == "OUTPUT_MATERIAL":
                output_node = node
                break

        # Store original output connection
        original_output_links: list[tuple[Any, Any]] = []
        if output_node and output_node.inputs["Surface"].is_linked:
            for link in output_node.inputs["Surface"].links:
                original_output_links.append((link.from_socket, link.to_socket))
                links.remove(link)

        # Create emission node
        emission = nodes.new(type="ShaderNodeEmission")

        # Connect source to emission
        if source_links:
            links.new(source_links[0][0], emission.inputs["Color"])
        else:
            # Use default value - handle both scalar (roughness/metallic) and color (base color) inputs
            default_val = bsdf.inputs[source_input_name].default_value
            if hasattr(default_val, "__len__"):
                # Color input (already RGBA tuple)
                emission.inputs["Color"].default_value = default_val
            else:
                # Scalar input (roughness/metallic) - convert to grayscale
                emission.inputs["Color"].default_value = (
                    default_val,
                    default_val,
                    default_val,
                    1.0,
                )

        # Connect emission to output
        if output_node:
            links.new(emission.outputs["Emission"], output_node.inputs["Surface"])

        temp_nodes.append((mat, emission, original_output_links))

    # Collect materials for bake node setup
    materials = [mat for mat, _, _ in temp_nodes]

    # Bake with context-managed bake nodes
    with bake_nodes_context(materials, img):
        bpy.ops.object.bake(type="EMIT")

    cleanup_emission_nodes(temp_nodes)


def bake_ao(objects: list[Any], img: Any, select_all_objects: bool) -> None:
    """Unified AO baking with configurable selection mode

    Args:
        objects: Objects to bake
        img: Target image
        select_all_objects: If True, select all (inter-object occlusion)
                           If False, bake each separately (self-occlusion)
    """
    if select_all_objects:
        # Combined mode - all objects selected for inter-object occlusion
        materials = [
            obj.material_slots[0].material for obj in objects if obj.material_slots
        ]

        with bake_nodes_context(materials, img):
            select_all(objects)
            bpy.ops.object.bake(type="AO", use_selected_to_active=False)
    else:
        # Separate mode - bake each object individually for self-occlusion only
        for obj in objects:
            if obj.material_slots:
                mat = obj.material_slots[0].material
                with bake_nodes_context([mat], img):
                    select_only(obj)
                    bpy.ops.object.bake(type="AO", use_selected_to_active=False)


# === LAYER 3: MAP TYPE WORKFLOWS ===

STANDARD_MAPS = ["albedo", "normal", "roughness", "metallic"]


def bake_standard_maps(objects: list[Any], obj_name: str | None) -> None:
    """Bake all standard maps for a set of objects

    Standard maps (albedo, normal, roughness, metallic) are baked to one image.
    In separate mode, obj_name identifies the target object.
    In combined mode, obj_name is None and all objects bake to one shared image.

    Args:
        objects: List of objects to bake
        obj_name: Object name for image naming (None in combined mode)
    """
    for map_type in STANDARD_MAPS:
        if not texture_maps.get(map_type):
            continue

        print(
            f"\n--- Baking {map_type.title()} {'for ' + obj_name if obj_name else 'Combined'} ---"
        )

        img = get_bake_image(map_type, obj_name)
        if not img:
            continue

        # Bake all objects to this image
        for obj in objects:
            print(f"  Baking {obj.name}")
            bake_object_to_image(obj, img, map_type)

        # Save if configured
        should_save = True
        if map_type in ("roughness", "metallic"):
            should_save = save_individual_metallic_roughness

        if should_save:
            save_bake_image(img, map_type, obj_name)


# === LAYER 4: MODE-SPECIFIC WORKFLOWS ===


def process_object_separate(obj: Any) -> None:
    """Complete baking workflow for one object in separate mode

    Args:
        obj: Blender Object to process
    """
    print(f"\n{'=' * 60}")
    print(f"Processing object: {obj.name}")
    print("=" * 60)

    # Bake standard maps
    bake_standard_maps([obj], obj.name)

    # Bake AO (special handling)
    if texture_maps.get("ambient_occlusion"):
        print(f"\n--- Baking Ambient Occlusion for {obj.name} ---")
        img = get_bake_image("ao", obj.name)
        if img:
            print(f"  Baking {obj.name} (self-occlusion only)")
            bake_ao([obj], img, select_all_objects=False)
            save_bake_image(img, "ao", obj.name)


def process_all_objects_combined(objects: list[Any]) -> None:
    """Complete baking workflow for all objects in combined mode

    Args:
        objects: List of Blender Objects to process together
    """
    print(f"\n{'=' * 60}")
    print(f"Processing all objects combined: {[obj.name for obj in objects]}")
    print("=" * 60)

    # Bake standard maps
    bake_standard_maps(objects, None)

    # Bake AO (special handling)
    if texture_maps.get("ambient_occlusion"):
        print("\n--- Baking Ambient Occlusion Combined ---")
        img = get_bake_image("ao")
        if img:
            print("  Baking all objects (inter-object occlusion)")
            bake_ao(objects, img, select_all_objects=True)
            save_bake_image(img, "ao")


def cleanup_emission_nodes(
    temp_nodes: list[tuple[Any, Any, list[tuple[Any, Any]]]],
) -> None:
    """Clean up temporary emission nodes and restore original connections

    Args:
        temp_nodes: List of (material, emission_node, original_links) tuples
    """
    for mat, emission, original_links in temp_nodes:
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.remove(emission)
        for from_socket, to_socket in original_links:
            links.new(from_socket, to_socket)


def pack_metallic_roughness_for_object(obj_name: str | None) -> None:
    """Pack metallic and roughness textures into glTF format

    glTF format: R=unused, G=roughness, B=metallic, A=1.0

    Args:
        obj_name: Object name (None in combined mode)
    """
    # Get images
    rough_img = get_image_for_mode("roughness", obj_name)
    metal_img = get_image_for_mode("metallic", obj_name)

    if obj_name:
        print(f"  Packing for {obj_name}")
    else:
        print("  Packing combined")

    output_filename = generate_name("metallic_roughness", obj_name, ".png")

    # Configure compositor
    tree = bpy.context.scene.node_tree
    nodes = tree.nodes
    links = tree.links
    nodes.clear()

    # Ensure Non-Color colorspace
    rough_img.colorspace_settings.name = "Non-Color"
    metal_img.colorspace_settings.name = "Non-Color"

    # Create nodes
    rough_node = nodes.new(type="CompositorNodeImage")
    rough_node.image = rough_img

    metal_node = nodes.new(type="CompositorNodeImage")
    metal_node.image = metal_img

    sep_rough = nodes.new(type="CompositorNodeSeparateColor")

    sep_metal = nodes.new(type="CompositorNodeSeparateColor")

    combine = nodes.new(type="CompositorNodeCombineColor")

    output = nodes.new(type="CompositorNodeComposite")

    # Connect
    links.new(rough_node.outputs["Image"], sep_rough.inputs["Image"])
    links.new(metal_node.outputs["Image"], sep_metal.inputs["Image"])

    combine.inputs["Red"].default_value = 1.0
    links.new(sep_rough.outputs["Red"], combine.inputs["Green"])
    links.new(sep_metal.outputs["Red"], combine.inputs["Blue"])
    combine.inputs["Alpha"].default_value = 1.0

    links.new(combine.outputs["Image"], output.inputs["Image"])

    # Render
    bpy.context.scene.render.filepath = str(textures_path / output_filename)
    bpy.ops.render.render(write_still=True)
    print(f"  Saved: {bpy.context.scene.render.filepath}")


def setup_baked_material_shader_nodes(mat: Any, obj_name: str | None) -> None:
    """Set up shader nodes for a baked material

    Args:
        mat: Blender Material to configure
        obj_name: Object name (None in combined mode) for image retrieval
    """
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    # Create base nodes
    tex_coord = nodes.new(type="ShaderNodeTexCoord")

    bsdf = nodes.new(type="ShaderNodeBsdfPrincipled")

    output = nodes.new(type="ShaderNodeOutputMaterial")

    # Get images based on mode
    albedo_img = get_image_for_mode("albedo", obj_name)
    normal_img = get_image_for_mode("normal", obj_name)

    mr_path = str(textures_path / generate_name("metallic_roughness", obj_name, ".png"))

    # Albedo
    if texture_maps["albedo"] and albedo_img:
        albedo_tex = nodes.new(type="ShaderNodeTexImage")
        albedo_tex.image = albedo_img
        albedo_tex.label = "Albedo"
        links.new(tex_coord.outputs["UV"], albedo_tex.inputs["Vector"])
        links.new(albedo_tex.outputs["Color"], bsdf.inputs["Base Color"])

    # Normal
    if texture_maps["normal"] and normal_img:
        normal_tex = nodes.new(type="ShaderNodeTexImage")
        normal_tex.image = normal_img
        normal_tex.label = "Normal"
        normal_tex.image.colorspace_settings.name = "Non-Color"

        normal_map = nodes.new(type="ShaderNodeNormalMap")

        links.new(tex_coord.outputs["UV"], normal_tex.inputs["Vector"])
        links.new(normal_tex.outputs["Color"], normal_map.inputs["Color"])
        links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])

    # Metallic-Roughness
    if settings.get("export_metallic_roughness_packed") and os.path.exists(mr_path):
        mr_tex = nodes.new(type="ShaderNodeTexImage")
        mr_tex.label = "Metallic-Roughness"
        mr_img = bpy.data.images.load(mr_path)
        mr_tex.image = mr_img
        mr_img.colorspace_settings.name = "Non-Color"

        separate_rgb = nodes.new(type="ShaderNodeSeparateColor")

        links.new(tex_coord.outputs["UV"], mr_tex.inputs["Vector"])
        links.new(mr_tex.outputs["Color"], separate_rgb.inputs["Color"])
        links.new(separate_rgb.outputs["Green"], bsdf.inputs["Roughness"])
        links.new(separate_rgb.outputs["Blue"], bsdf.inputs["Metallic"])

    links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])


def create_and_apply_material(obj_name: str | None, target_objects: list[Any]) -> None:
    """Create baked material and apply to objects

    Args:
        obj_name: Object name for naming (None for combined)
        target_objects: Objects to apply material to
    """
    # Create material
    mat_name = generate_name("baked", obj_name)
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    setup_baked_material_shader_nodes(mat, obj_name)

    # Apply to objects
    for obj in target_objects:
        if obj.type == "MESH" and obj.data:
            obj.data.materials.clear()
            obj.data.materials.append(mat)

    if obj_name:
        print(f"  Created and applied material for {obj_name}")
    else:
        print("  Created and applied combined material")


def create_images_for_maps(obj_name: str | None) -> None:
    """Create bake images for all configured map types

    Args:
        obj_name: Object name (separate mode) or None (combined mode)
    """
    for map_type in STANDARD_MAPS:
        if texture_maps.get(map_type):
            create_bake_image(map_type, obj_name)
    if texture_maps.get("ambient_occlusion"):
        create_bake_image("ao", obj_name)
    if texture_maps.get("emission"):
        create_bake_image("emission", obj_name)


def configure_compositor_for_packing() -> dict[str, Any]:
    """Configure Blender compositor for metallic-roughness packing

    Returns:
        Dict of original settings to restore later
    """
    bpy.context.scene.render.resolution_x = resolution
    bpy.context.scene.render.resolution_y = resolution
    bpy.context.scene.render.resolution_percentage = 100
    bpy.context.scene.use_nodes = True

    return {
        "view_transform": bpy.context.scene.view_settings.view_transform,
        "file_format": bpy.context.scene.render.image_settings.file_format,
        "color_mode": bpy.context.scene.render.image_settings.color_mode,
        "color_depth": bpy.context.scene.render.image_settings.color_depth,
        "compression": bpy.context.scene.render.image_settings.compression,
    }


def apply_packing_settings() -> None:
    """Apply compositor settings for metallic-roughness packing"""
    bpy.context.scene.view_settings.view_transform = "Standard"
    bpy.context.scene.render.image_settings.file_format = "PNG"
    bpy.context.scene.render.image_settings.color_mode = "RGBA"
    bpy.context.scene.render.image_settings.color_depth = "8"
    bpy.context.scene.render.image_settings.compression = 15


def restore_compositor_settings(original: dict[str, Any]) -> None:
    """Restore original compositor settings

    Args:
        original: Dict of original settings from configure_compositor_for_packing
    """
    bpy.context.scene.view_settings.view_transform = original["view_transform"]
    bpy.context.scene.render.image_settings.file_format = original["file_format"]
    bpy.context.scene.render.image_settings.color_mode = original["color_mode"]
    bpy.context.scene.render.image_settings.color_depth = original["color_depth"]
    bpy.context.scene.render.image_settings.compression = original["compression"]


def pack_metallic_roughness_if_needed(obj_names: list[str] | None) -> None:
    """Pack metallic-roughness textures if configured

    Args:
        obj_names: List of object names (separate mode) or None (combined mode)
    """
    if not (
        settings.get("export_metallic_roughness_packed")
        and texture_maps.get("roughness")
        and texture_maps.get("metallic")
    ):
        return

    is_separate = obj_names is not None
    print(
        f"\n--- Creating Metallic-Roughness Packed Texture{'s' if is_separate else ''} ---"
    )

    original_settings = configure_compositor_for_packing()
    apply_packing_settings()

    if is_separate:
        for obj_name in obj_names:
            pack_metallic_roughness_for_object(obj_name)
    else:
        pack_metallic_roughness_for_object(None)

    restore_compositor_settings(original_settings)


# === LAYER 5: POST-PROCESSING ===


def export_glb() -> None:
    """Export selected objects as GLB"""
    print("\n--- Exporting GLB ---")

    # Select objects
    bpy.ops.object.select_all(action="DESELECT")
    for obj_name in object_names:
        obj = bpy.data.objects[obj_name]
        obj.select_set(True)
        if obj.type == "EMPTY":

            def select_children(parent: Any) -> None:
                for child in parent.children:
                    child.select_set(True)
                    select_children(child)

            select_children(obj)

    glb_path = str(output_path / f"{output_name}.glb")

    bpy.ops.export_scene.gltf(
        filepath=glb_path,
        export_format="GLB",
        use_selection=True,
        export_texcoords=True,
        export_normals=True,
        export_tangents=True,
        export_materials="EXPORT",
        export_image_format="AUTO",
    )

    print(f"  Exported: {glb_path}")


def generate_manifest() -> None:
    """Generate manifest file listing all baked outputs"""
    manifest_path = output_path / "bake_manifest.txt"
    with open(manifest_path, "w") as f:
        _ = f.write("PBR Texture Baking Manifest\n")
        _ = f.write("===========================\n\n")
        _ = f.write(f"Source: {blend_file}\n")
        _ = f.write(f"Objects: {', '.join(object_names)}\n")
        _ = f.write(f"Resolution: {resolution}x{resolution}\n")
        _ = f.write(f"Bake Margin: {settings['bake_margin']}px\n")
        _ = f.write(
            f"Mode: {'Separate per object' if bake_separate_per_object else 'Combined'}\n\n"
        )
        _ = f.write("Generated Files:\n")

        for file in sorted(output_path.rglob("*")):
            if file.is_file() and file != manifest_path:
                _ = f.write(f"  - {file.relative_to(output_path)}\n")

    print("\n=== Baking Complete ===")
    print(f"Output directory: {output_path}")
    print(f"Manifest: {manifest_path}")


# === LAYER 6: MAIN EXECUTION ===


def main() -> None:
    """Main execution: set up environment, bake textures, post-process"""
    # Setup
    setup_blender_environment()

    # Create images based on mode
    print("\n=== Creating Bake Images ===")

    if bake_separate_per_object:
        for obj in selected_objects:
            print(f"\nImages for '{obj.name}':")
            create_images_for_maps(obj.name)

        total_images = sum(len(images) for images in created_images.values())
        print(f"\nCreated {total_images} images for {len(created_images)} objects")
    else:
        print("\nShared images for all objects:")
        create_images_for_maps(None)
        print(f"\nCreated {len(created_images)} shared images")

    # Execute baking based on mode (SINGLE DECISION POINT)
    print("\n=== Starting Texture Baking ===")

    if bake_separate_per_object:
        # SEPARATE MODE: Process each object independently
        for obj in selected_objects:
            process_object_separate(obj)

        # Post-processing per object
        pack_metallic_roughness_if_needed([obj.name for obj in selected_objects])

        # Create materials per object
        print("\n--- Creating Baked Materials ---")
        for obj in selected_objects:
            create_and_apply_material(obj.name, [obj])

    else:
        # COMBINED MODE: Process all objects together
        process_all_objects_combined(selected_objects)

        # Post-processing combined
        pack_metallic_roughness_if_needed(None)

        # Create shared material
        print("\n--- Creating Baked Material ---")
        create_and_apply_material(None, selected_objects)

    # GLB export (mode-independent)
    if settings.get("export_glb"):
        export_glb()

    # Generate manifest
    generate_manifest()


# Execute
if __name__ == "__main__":
    main()
