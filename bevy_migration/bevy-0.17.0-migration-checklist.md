# Bevy 0.17.0 Migration Checklist

Generated from official Bevy migration guides.
Total migration items: 114

---

## "`LightVisibilityClass` renamed to `ClusterVisibilityClass`"

**Pull Requests:** #19986

- [ ] "`LightVisibilityClass` renamed to `ClusterVisibilityClass`"

---

## Make `ScrollPosition` newtype `Vec2`

**Pull Requests:** #19881

- [ ] Make `ScrollPosition` newtype `Vec2`

---

## "RenderTargetInfo's default `scale_factor` has been changed to `1.`"

**Pull Requests:** #21802

- [ ] "RenderTargetInfo's default `scale_factor` has been changed to `1.`"

---

## "`ScrollPosition` now uses logical pixel units and is no longer overwritten during layout updates"

**Pull Requests:** #20093

- [ ] "`ScrollPosition` now uses logical pixel units and is no longer overwritten during layout updates"

---

## "`Anchor` is now a required component on `Sprite`"

**Pull Requests:** #18393

The `anchor` field has been removed from `Sprite`. Instead the `Anchor` component is now a required component on `Sprite`.

- [ ] "`Anchor` is now a required component on `Sprite`"

**Search Patterns:** `Anchor`, `Sprite`, `anchor`

---

## "`AnimationGraph` no longer supports raw AssetIds"

**Pull Requests:** #19615

- [ ] "`AnimationGraph` no longer supports raw AssetIds"

---

## "`Assets::insert` and `Assets::get_or_insert_with` now return `Result`"

**Pull Requests:** #20439

- [ ] "`Assets::insert` and `Assets::get_or_insert_with` now return `Result`"

---

## "`bevy_render` reorganization"

**Pull Requests:** #20485, #20330, #18703, #20587, #20502, #19997, #19991, #20000, #19949, #19943, #19953, #20498, #20496, #20493, #20492, #20491, #20488, #20487, #20486, #20483, #20480, #20479, #20478, #20477, #20473, #20472, #20471, #20470, #20392, #20390, #20388, #20345, #20344, #20051, #19985, #19973, #19965, #19963, #19962, #19960, #19959, #19958, #19957, #19956, #19955, #19954, #16620, #16619, #15700, #15666, #15650, #20778, #20857, #18323

- [ ] "`bevy_render` reorganization"

---

## "`CheckChangeTicks` parameter in `System::check_change_tick`"

**Pull Requests:** #19274, #19600

`System::check_change_tick` took a `Tick` parameter to update internal ticks. This is needed to keep queried components filtered by their change tick reliably not be matched if their last change or add and the system's last run was very long ago. This is also needed for similar methods involving the system's ticks for the same reason.

- [ ] "`CheckChangeTicks` parameter in `System::check_change_tick`"

**Search Patterns:** `System::check_change_tick`, `Tick`

---

## ChromaticAberration LUT is now Option

**Pull Requests:** #19408

- [ ] ChromaticAberration LUT is now Option

---

## "`CloneBehavior` is no longer `PartialEq` or `Eq`"

**Pull Requests:** #18393

- [ ] "`CloneBehavior` is no longer `PartialEq` or `Eq`"

---

## Combine now takes an extra parameter

**Pull Requests:** #20689

- [ ] Combine now takes an extra parameter

---

## Component lifecycle reorganization

**Pull Requests:** #19543

To improve documentation, discoverability and internal organization, we've gathered all of the component lifecycle-related code we could and moved it into a dedicated `lifecycle` module.

- [ ] Component lifecycle reorganization

**Search Patterns:** `lifecycle`

---

## "`Entry` enum is now `ComponentEntry`"

**Pull Requests:** #19517

The `Entry` enum in `bevy::ecs::world` has been renamed to `ComponentEntry`, to avoid name clashes with `hash_map`, `hash_table` and `hash_set` `Entry` types.

- [ ] "`Entry` enum is now `ComponentEntry`"
- [ ] Rename `Entry` to `ComponentEntry`

**Search Patterns:** `ComponentEntry`, `Entry`, `bevy::ecs::world`, `hash_map`, `hash_set`, `hash_table`

---

## "`ComponentsRegistrator` no longer implements `DerefMut`"

**Pull Requests:** #14791, #15458, #15269

- [ ] "`ComponentsRegistrator` no longer implements `DerefMut`"

---

## Composable Specialization

**Pull Requests:** #17373

- [ ] Composable Specialization

**Search Patterns:** `AssetServer`, `BindGroupLayout`, `Clone`, `ColorTargetState`, `ColorWrites`, `Copy`, `FragmentState`, `FromWorld`, `Handle`, `Hash`, `Key`, `Msaa`, `MultisampleState`, `MyPipeline`, `MyPipelineKey`, `MySpecializer`, `None`, `PartialEq`, `RenderDevice`, `RenderPipelineDescriptor`

**Official Example:**

```rust
// Old
#[derive(Resource)]
pub struct MyPipeline {
    layout: BindGroupLayout,
    layout_msaa: BindGroupLayout,
    vertex: Handle<Shader>,
    fragment: Handle<Shader>,
}

// before
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub struct MyPipelineKey {
    msaa: Msaa,
}

impl FromWorld for MyPipeline {
    fn from_world(world: &mut World) -> Self {
        let render_device = world.resource::<RenderDevice>();
        let asset_server = world.resource::<AssetServer>();

        let layout = render_device.create_bind_group_layout(...);
        let layout_msaa = render_device.create_bind_group_layout(...);

        let vertex = asset_server.load("vertex.wgsl");
        let fragment = asset_server.load("fragment.wgsl");
        
        Self {
            layout,
            layout_msaa,
            vertex,
            fragment,
        }
    }
}

impl SpecializedRenderPipeline for MyPipeline {
    type Key = MyPipelineKey;

    fn specialize(&self, key: Self::Key) -> RenderPipelineDescriptor {
        RenderPipelineDescriptor {
            label: Some("my_pipeline".into()),
            layout: vec![
                if key.msaa.samples() > 1 {
                    self.layout_msaa.clone()
                } else { 
                    self.layout.clone() 
                }
            ],
            vertex: VertexState {
                shader: self.vertex.clone(),
                ..default()
            },
            multisample: MultisampleState {
                count: key.msaa.samples(),
                ..default()
            },
            fragment: Some(FragmentState {
                shader: self.fragment.clone(),
                targets: vec![Some(ColorTargetState {
                    format: TextureFormat::Rgba8Unorm,
                    blend: None,
                    write_mask: ColorWrites::all(),
                })],
                ..default()
            }),
            ..default()
        },
    }
}

render_app
    .init_resource::<MyPipeline>();
    .init_resource::<SpecializedRenderPipelines<MySpecializer>>();
```

```rust
// New
#[derive(Resource)]
pub struct MyPipeline {
    // the base_descriptor and specializer each hold onto the static
    // wgpu resources (layout, shader handles), so we don't need
    // explicit fields for them here. However, real-world cases
    // may still need to expose them as fields to create bind groups
    // from, for example.
    variants: Variants<RenderPipeline, MySpecializer>,
}

pub struct MySpecializer {
    layout: BindGroupLayout,
    layout_msaa: BindGroupLayout,
}

#[derive(Clone, Copy, PartialEq, Eq, Hash, SpecializerKey)]
pub struct MyPipelineKey {
    msaa: Msaa,
}

impl FromWorld for MyPipeline {
    fn from_world(world: &mut World) -> Self {
        let render_device = world.resource::<RenderDevice>();
        let asset_server = world.resource::<AssetServer>();

        let layout = render_device.create_bind_group_layout(...);
        let layout_msaa = render_device.create_bind_group_layout(...);

        let vertex = asset_server.load("vertex.wgsl");
        let fragment = asset_server.load("fragment.wgsl");

        let base_descriptor = RenderPipelineDescriptor {
            label: Some("my_pipeline".into()),
            vertex: VertexState {
                shader: vertex.clone(),
                ..default()
            },
            fragment: Some(FragmentState {
                shader: fragment.clone(),
                ..default()
            }),
            ..default()
        },

        let variants = Variants::new(
            MySpecializer {
                layout: layout.clone(),
                layout_msaa: layout_msaa.clone(),
            },
            base_descriptor,
        );
        
        Self { variants }
    }
}

impl Specializer<RenderPipeline> for MySpecializer {
    type Key = MyKey;

    fn specialize(
        &self,
        key: Self::Key,
        descriptor: &mut RenderPipeline,
    ) -> Result<Canonical<Self::Key>, BevyError> {
        descriptor.multisample.count = key.msaa.samples();

        let layout = if key.msaa.samples() > 1 { 
            self.layout_msaa.clone()
        } else {
            self.layout.clone()
        };

        descriptor.set_layout(0, layout);

        Ok(key)
    }
}

render_app.init_resource::<MyPipeline>();
```

---

## Compressed image saver feature

**Pull Requests:** #19789

- [ ] Compressed image saver feature

---

## Move cursor-related types from `bevy_winit` to `bevy_window`

**Pull Requests:** #20427

- [ ] Move cursor-related types from `bevy_winit` to `bevy_window`

---

## Deprecate `iter_entities` and `iter_entities_mut`.

**Pull Requests:** #20260

- [ ] Deprecate `iter_entities` and `iter_entities_mut`.

---

## "`DragEnter` now includes the dragged entity"

**Pull Requests:** #19179

`DragEnter` events are now triggered when entering any entity, even the originally dragged one. This makes the behavior more consistent.

- [ ] "`DragEnter` now includes the dragged entity"

**Search Patterns:** `DragEnter`

---

## `DynamicBundle`

**Pull Requests:** #20772, #20877

In order to reduce the stack size taken up by spawning and inserting large bundles, the way the (mostly internal) trait `DynamicBundle` gets called has changed significantly:

- [ ] `DynamicBundle`

**Search Patterns:** `DynamicBundle`

---

## "`Entities` API changes"

**Pull Requests:** #19350, #19433

- [ ] "`Entities` API changes"

---

## EntityClonerBuilder Split

**Pull Requests:** #19649, #19977

`EntityClonerBuilder` is now generic and has different methods depending on the generic.

- [ ] EntityClonerBuilder Split

**Search Patterns:** `EntityClonerBuilder`

---

## Manual Entity Creation and Representation

**Pull Requests:** #18704, #19121

An entity is made of two parts: and index and a generation. Both have changes:

- [ ] Manual Entity Creation and Representation

---

## "`Event` trait split / Rename"

**Pull Requests:** #19647

- [ ] "`Event` trait split / Rename"

---

## Extract `PickingPlugin` members into `PickingSettings`

**Pull Requests:** #19078

- [ ] Extract `PickingPlugin` members into `PickingSettings`

---

## Extract `PointerInputPlugin` members into `PointerInputSettings`

**Pull Requests:** #19078

- [ ] Extract `PointerInputPlugin` members into `PointerInputSettings`

---

## "`take_extract` now returns `dyn FnMut` instead of `dyn Fn`"

**Pull Requests:** #19926

- [ ] "`take_extract` now returns `dyn FnMut` instead of `dyn Fn`"

---

## Extract UI text colors per glyph

**Pull Requests:** #20245

- [ ] Extract UI text colors per glyph

---

## "`ExtractedUiNode`'s `stack_index` has been renamed to `z_order` and is now an `f32`."

**Pull Requests:** #19691

- [ ] "`ExtractedUiNode`'s `stack_index` has been renamed to `z_order` and is now an `f32`."

---

## "`FULLSCREEN_SHADER_HANDLE` replaced with `FullscreenShader`"

**Pull Requests:** #19426

- [ ] "`FULLSCREEN_SHADER_HANDLE` replaced with `FullscreenShader`"

---

## "`GatedReader` and `GatedOpener` are now private."

**Pull Requests:** #18473

- [ ] "`GatedReader` and `GatedOpener` are now private."

---

## Generic `Option` Parameter

**Pull Requests:** #18766

- [ ] Generic `Option` Parameter

---

## Updated `glam`, `rand` and `getrandom` versions with new failures when building for web

**Pull Requests:** #18047

- [ ] Updated `glam`, `rand` and `getrandom` versions with new failures when building for web

---

## "OpenGL ES `wgpu` backend is no longer supported by default"

**Pull Requests:** #20793

- [ ] "OpenGL ES `wgpu` backend is no longer supported by default"

---

## glTF animation loading is now optional

**Pull Requests:** #20750

- [ ] glTF animation loading is now optional

---

## "`Handle::Weak` has been replaced by `Handle::Uuid`."

**Pull Requests:** #19896

- [ ] "`Handle::Weak` has been replaced by `Handle::Uuid`."

---

## Split `Hdr` from `Camera`

**Pull Requests:** #18873

`Camera.hdr` has been split out into a new marker component, `Hdr`, which can be found at `bevy::render::view::Hdr`.

- [ ] Split `Hdr` from `Camera`

**Search Patterns:** `Camera.hdr`, `Hdr`, `bevy::render::view::Hdr`

---

## Improve error when using `run_system` command with a `SystemId` of wrong type

**Pull Requests:** #19011

- [ ] Improve error when using `run_system` command with a `SystemId` of wrong type

---

## Observers and one-shot systems are now marked as `Internal`

**Pull Requests:** #20204

Bevy 0.17 introduces internal entities. Entities tagged by the `Internal` component that are hidden from most queries using [`DefaultQueryFilters`](https://docs.rs/bevy/latest/bevy/ecs/entity_disabling/index.html).

- [ ] Observers and one-shot systems are now marked as `Internal`

**Search Patterns:** `DefaultQueryFilters`, `Internal`

---

## Interned labels and `DynEq`

**Pull Requests:** #18984

- [ ] Interned labels and `DynEq`

---

## "`labeled_asset_scope` can now return errors"

**Pull Requests:** #19449

- [ ] "`labeled_asset_scope` can now return errors"

---

## Change filters container of `LogDiagnosticsState` to `HashSet`

**Pull Requests:** #19323

- [ ] Change filters container of `LogDiagnosticsState` to `HashSet`

---

## "Reflection-based maps are now unordered"

**Pull Requests:** #19802

`DynamicMap` is now unordered, and the `Map` trait no longer assumes implementors to be ordered. If you previously relied on them being ordered, you should now store a list of keys (`Vec<Box<dyn PartialReflect>>`) separately.

- [ ] "Reflection-based maps are now unordered"

**Search Patterns:** `DynamicMap`, `Map`, `Vec<Box<dyn PartialReflect>>`

---

## Unify `ObserverState` and `Observer` components

**Pull Requests:** #18728

- [ ] Unify `ObserverState` and `Observer` components

---

## Smooth normals implementation changed

**Pull Requests:** #18552

- [ ] Smooth normals implementation changed

---

## Non-generic `Access`

**Pull Requests:** #20288

- [ ] Non-generic `Access`

---

## Observer / Event API Changes

**Pull Requests:** #20731, #19440, #19596

The observer "trigger" API has changed a bit to improve clarity and type-safety.

- [ ] Observer / Event API Changes

---

## Exclusive systems may not be used as observers

**Pull Requests:** #19033

- [ ] Exclusive systems may not be used as observers

---

## "`OverflowClipBox`'s default is now `PaddingBox`"

**Pull Requests:** #18935

- [ ] "`OverflowClipBox`'s default is now `PaddingBox`"

---

## Changes to Bevy's system parallelism strategy

**Pull Requests:** #16885

- [ ] Changes to Bevy's system parallelism strategy

---

## Changes to the default error handler mechanism

**Pull Requests:** #18810

- [ ] Changes to the default error handler mechanism

---

## "`Location` is no longer a `Component`"

**Pull Requests:** #19306

- [ ] "`Location` is no longer a `Component`"

---

## Original target of `Pointer` picking events is now stored on observers

**Pull Requests:** #19663

- [ ] Original target of `Pointer` picking events is now stored on observers

---

## Polylines and Polygons are no longer const-generic

**Pull Requests:** #20250

- [ ] Polylines and Polygons are no longer const-generic

---

## Query items can borrow from query state

**Pull Requests:** #15396, #19720

- [ ] Query items can borrow from query state

**Search Patterns:** `ItemQuery`, `Option`, `Param`, `ROQueryItem`, `RenderCommandResult`, `SystemParamItem`, `TrackedRenderPass`, `ViewQuery`

---

## "`ViewRangefinder3d::from_world_from_view` now takes `Affine3A` instead of `Mat4`"

**Pull Requests:** #20707

- [ ] "`ViewRangefinder3d::from_world_from_view` now takes `Affine3A` instead of `Mat4`"

---

## "`ReflectAsset` now uses `UntypedAssetId` instead of `UntypedHandle`"

**Pull Requests:** #19606

- [ ] "`ReflectAsset` now uses `UntypedAssetId` instead of `UntypedHandle`"

---

## Changes to type registration for reflection

**Pull Requests:** #15030, #20435, #20893

- [ ] Changes to type registration for reflection

---

## Relationship method set_risky

**Pull Requests:** #19601

- [ ] Relationship method set_risky

---

## "`RelativeCursorPosition` is now object-centered"

**Pull Requests:** #16615

- [ ] "`RelativeCursorPosition` is now object-centered"

---

## Remove `ArchetypeComponentId`

**Pull Requests:** #19143

- [ ] Remove `ArchetypeComponentId`

---

## Remove `Bundle::register_required_components`

**Pull Requests:** #19967

- [ ] Remove `Bundle::register_required_components`

---

## "Removed `cosmic_text` re-exports"

**Pull Requests:** #19516

- [ ] "Removed `cosmic_text` re-exports"

---

## Remove default implementation of `extend_from_iter` from `RelationshipSourceCollection`

**Pull Requests:** #20255

The `extend_from_iter` method in the `RelationshipSourceCollection` trait no longer has a default implementation. If you have implemented a custom relationship source collection, you must now provide your own implementation of this method.

- [ ] Remove default implementation of `extend_from_iter` from `RelationshipSourceCollection`

**Search Patterns:** `RelationshipSourceCollection`, `extend_from_iter`

---

## Removed Deprecated Batch Spawning Methods

**Pull Requests:** #18148

The following deprecated functions have been removed:

- [ ] Removed Deprecated Batch Spawning Methods

---

## Remove `scale_value`

**Pull Requests:** #19143

- [ ] Remove `scale_value`

---

## Replaced `TextFont` constructor methods with `From` impls

**Pull Requests:** #20335, #20450

The `TextFont::from_font` and `TextFont::from_line_height` constructor methods have been removed in favor of `From` trait implementations.

- [ ] Replaced `TextFont` constructor methods with `From` impls

**Search Patterns:** `From`, `TextFont::from_font`, `TextFont::from_line_height`

---

## Remove the `Add`/`Sub` impls on `Volume`

**Pull Requests:** #19423

- [ ] Remove the `Add`/`Sub` impls on `Volume`

---

## "`RemovedComponents` methods renamed to match `Event` to `Message` rename"

**Pull Requests:** #20953, #20954

- [ ] "`RemovedComponents` methods renamed to match `Event` to `Message` rename"

---

## Renamed `JustifyText` to `Justify`

**Pull Requests:** #19522

`JustifyText` has been renamed to `Justify`.

- [ ] Renamed `JustifyText` to `Justify`
- [ ] Rename `JustifyText` to `Justify`

**Search Patterns:** `Justify`, `JustifyText`

---

## Renamed `Condition` to `SystemCondition`

**Pull Requests:** #19328

The `Condition` trait is now called `SystemCondition`. Replace all references and imports.

- [ ] Renamed `Condition` to `SystemCondition`

**Search Patterns:** `Condition`, `SystemCondition`

---

## Rename `Pointer<Pressed>` and `Pointer<Released>` to `Pointer<Press>` and `Pointer<Release>`

**Pull Requests:** #19179

- [ ] Rename `Pointer<Pressed>` and `Pointer<Released>` to `Pointer<Press>` and `Pointer<Release>`

---

## Use glTF material names for spawned primitive entities

**Pull Requests:** #19287

When loading a glTF scene in Bevy, each mesh primitive will generate an entity and store a `GltfMaterialName` component and `Name` component.

- [ ] Use glTF material names for spawned primitive entities

**Search Patterns:** `GltfMaterialName`, `Name`

---

## Renamed state scoped entities and events

**Pull Requests:** #18818, #19435, #20872

- [ ] Renamed state scoped entities and events

---

## Renamed `Timer::paused` to `Timer::is_paused` and `Timer::finished` to `Timer::is_finished`

**Pull Requests:** #19386

The following changes were made:

- [ ] Renamed `Timer::paused` to `Timer::is_paused` and `Timer::finished` to `Timer::is_finished`

---

## Transform and GlobalTransform::compute_matrix rename

**Pull Requests:** #19643, #19646

- [ ] Transform and GlobalTransform::compute_matrix rename

---

## Renamed BRP methods

**Pull Requests:** #19377

- [ ] Renamed BRP methods

---

## Renamed `ComputedNodeTarget` and `update_ui_context_system`

**Pull Requests:** #20519, #20532

`ComputedNodeTarget` has been renamed to `ComputedUiTargetCamera`. New name chosen because the component's value is derived from `UiTargetCamera`.

- [ ] Renamed `ComputedNodeTarget` and `update_ui_context_system`
- [ ] Rename `ComputedNodeTarget` to `ComputedUiTargetCamera`

**Search Patterns:** `ComputedNodeTarget`, `ComputedUiTargetCamera`, `UiTargetCamera`

---

## "`RenderGraphApp` renamed to `RenderGraphExt`"

**Pull Requests:** #19912

- [ ] "`RenderGraphApp` renamed to `RenderGraphExt`"

---

## Many render resources now initialized in `RenderStartup`

**Pull Requests:** #19841, #19885, #19886, #19897, #19898, #19901, #19912, #19926, #19999, #20002, #20024, #20124, #20147, #20184, #20194, #20195, #20208, #20209, #20210

- [ ] Many render resources now initialized in `RenderStartup`

---

## "RenderTarget error handling"

**Pull Requests:** #20503

- [ ] "RenderTarget error handling"

---

## Replace `Gilrs`, `AccessKitAdapters`, and `WinitWindows` non-send resources

**Pull Requests:** #18386, #17730, #19575

- [ ] Replace `Gilrs`, `AccessKitAdapters`, and `WinitWindows` non-send resources

---

## Required components refactor

**Pull Requests:** #20110

The required components feature has been reworked to be more consistent around the priority of the required components and fix some soundness issues. In particular:

- [ ] Required components refactor

---

## Rework `MergeMeshError`

**Pull Requests:** #18561

`MergeMeshError` was reworked to account for the possibility of the meshes being merged having two different `PrimitiveTopology`'s, and was renamed to `MeshMergeError` to align with the naming of other mesh errors.

- [ ] Rework `MergeMeshError`
- [ ] Rename `MergeMeshError` to `PrimitiveTopology`

**Search Patterns:** `MergeMeshError`, `MeshMergeError`, `PrimitiveTopology`

---

## "Fix `From<Rot2>` implementation for `Mat2`"

**Pull Requests:** #20522

Past releases had an incorrect `From<Rot2>` implementation for `Mat2`, constructing a rotation matrix in the following form:

- [ ] "Fix `From<Rot2>` implementation for `Mat2`"

**Search Patterns:** `From<Rot2>`, `Mat2`, `The`, `This`

---

## "`VectorSpace` implementations"

**Pull Requests:** #19194

Previously, implementing `VectorSpace` for a type required your type to use or at least interface with `f32`. This made implementing `VectorSpace` for double-precision types (like `DVec3`) less meaningful and useful, requiring lots of casting. `VectorSpace` has a new required associated type `Scalar` that's bounded by a new trait `ScalarField`. `bevy_math` implements this trait for `f64` and `f32` out of the box, and `VectorSpace` is now implemented for `DVec[N]` types.

- [ ] "`VectorSpace` implementations"

**Search Patterns:** `DVec3`, `DVec[N]`, `Scalar`, `ScalarField`, `VectorSpace`, `bevy_math`, `f32`, `f64`

---

## "`SceneSpawner` methods have been renamed and replaced"

**Pull Requests:** #18358

- [ ] "`SceneSpawner` methods have been renamed and replaced"

---

## Schedule API Cleanup

**Pull Requests:** #19352, #20119, #20172, #20256

- [ ] Schedule API Cleanup

---

## Rename `send_event` and similar methods to `write_message`

**Pull Requests:** #20017, #20953

Following up on the `EventWriter::send` being renamed to `EventWriter::write` in 0.16, many similar methods have been renamed. Note that "buffered events" are now known as `Messages`, and the naming reflects that here.

- [ ] Rename `send_event` and similar methods to `write_message`
- [ ] Rename `EventWriter::send` to `EventWriter::write`

**Search Patterns:** `EventWriter::send`, `EventWriter::write`, `Messages`

---

## Separate Border Colors

**Pull Requests:** #18682

- [ ] Separate Border Colors

---

## Deprecated Simple Executor

**Pull Requests:** #18753

Bevy has deprecated `SimpleExecutor`, one of the `SystemExecutor`s in Bevy alongside `SingleThreadedExecutor` and `MultiThreadedExecutor` (which aren't going anywhere any time soon).

- [ ] Deprecated Simple Executor

**Search Patterns:** `MultiThreadedExecutor`, `SimpleExecutor`, `SingleThreadedExecutor`, `SystemExecutor`

---

## `SpawnableList`

**Pull Requests:** #20772, #20877

In order to reduce the stack size taken up by spawning and inserting large bundles, `SpawnableList` now takes a `MovingPtr<T>` as the self-type for the `spawn` function:

- [ ] `SpawnableList`

**Search Patterns:** `MovingPtr<T>`, `SpawnableList`, `spawn`

---

## Specialized UI transform

**Pull Requests:** #16615

Bevy UI now uses specialized 2D UI transform components `UiTransform` and `UiGlobalTransform` in place of `Transform` and `GlobalTransform`.

- [ ] Specialized UI transform

**Search Patterns:** `GlobalTransform`, `Transform`, `UiGlobalTransform`, `UiTransform`

---

## Window is now split into multiple components

**Pull Requests:** #19668

- [ ] Window is now split into multiple components

---

## The render target info from `ComputedUiTargetCamera` has been removed.

**Pull Requests:** #20535

- [ ] The render target info from `ComputedUiTargetCamera` has been removed.

---

## Fixed UI draw order and `stack_z_offsets` changes

**Pull Requests:** #19691

- [ ] Fixed UI draw order and `stack_z_offsets` changes

---

## State-scoped entities are now always enabled implicitly

**Pull Requests:** #19354, #20883

- [ ] State-scoped entities are now always enabled implicitly

---

## Stop exposing mp3 support through minimp3

**Pull Requests:** #20183

The `minimp3` feature is no longer exposed from Bevy. Bevy still supports mp3 through the `mp3` feature.

- [ ] Stop exposing mp3 support through minimp3

**Search Patterns:** `minimp3`, `mp3`

---

## Stop storing access in systems

**Pull Requests:** #19496, #19477

- [ ] Stop storing access in systems

---

## "`SyncCell` and `SyncUnsafeCell` moved to bevy_platform"

**Pull Requests:** #19305

- [ ] "`SyncCell` and `SyncUnsafeCell` moved to bevy_platform"

---

## "`System::run` returns `Result`"

**Pull Requests:** #19145

In order to support fallible systems and parameter-based system skipping like `Single` and `If<T>` in more places, `System::run` and related methods now return a `Result` instead of a plain value.

- [ ] "`System::run` returns `Result`"

**Search Patterns:** `If<T>`, `Result`, `Single`, `System::run`

---

## Consistent `*Systems` naming convention for system sets

**Pull Requests:** #18900

- [ ] Consistent `*Systems` naming convention for system sets

---

## TAA is no longer experimental

**Pull Requests:** #18349

TAA is no longer experimental.

- [ ] TAA is no longer experimental

---

## "`Text2d` moved to `bevy_sprite`"

**Pull Requests:** #20594

- [ ] "`Text2d` moved to `bevy_sprite`"

---

## "`TextShadow` has been moved to `bevy::ui::widget::text`"

- [ ] "`TextShadow` has been moved to `bevy::ui::widget::text`"

---

## TextureFormat::pixel_size now returns a Result

**Pull Requests:** #20574

The `TextureFormat::pixel_size()` method now returns a `Result<usize, TextureAccessError>` instead of `usize`.

- [ ] TextureFormat::pixel_size now returns a Result

**Search Patterns:** `Result<usize, TextureAccessError>`, `TextureFormat::pixel_size()`, `usize`

---

## Move UI Debug Options from `bevy_ui` to `bevy_ui_render`

**Pull Requests:** #18703

The `UiDebugOptions` resource used for controlling the UI Debug Overlay has been moved from the internal `bevy_ui` crate to the `bevy_ui_render` crate, and is now accessible from the prelude of `bevy_ui_render` and, as before, from the prelude of `bevy`:

- [ ] Move UI Debug Options from `bevy_ui` to `bevy_ui_render`

**Search Patterns:** `UiDebugOptions`, `bevy`, `bevy::prelude::`, `bevy::ui::`, `bevy_ui`, `bevy_ui_render`, `bevy_ui_render::prelude::`

---

## Unified system state flag

**Pull Requests:** #19506

Now the system have a unified `SystemStateFlags` to represent its different states.

- [ ] Unified system state flag

**Search Patterns:** `SystemStateFlags`

---

## view_transformations.wgsl deprecated in favor of view.wgsl

**Pull Requests:** #20313

All functions in view_transformations.wgsl have been replaced and deprecated.

- [ ] view_transformations.wgsl deprecated in favor of view.wgsl

---

## Enable Wayland by default

**Pull Requests:** #19232

Wayland has now been added to the default features of the `bevy` crate.

- [ ] Enable Wayland by default

**Search Patterns:** `bevy`

---

## "`wgpu` 25"

**Pull Requests:** #19563

- [ ] "`wgpu` 25"

---

## Window Resolution Constructors

**Pull Requests:** #20582

- [ ] Window Resolution Constructors

---

## New `zstd` backend

**Pull Requests:** #19793

- [ ] New `zstd` backend

---
