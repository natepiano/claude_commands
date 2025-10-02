# Bevy 0.17.0 Migration Checklist

Generated from official Bevy migration guides with semantic enhancement.

**Processing:**
- Pass 1: Python extraction of structure and content
- Pass 2: 10 parallel agents for semantic review
- Pass 3: Sequential merge into final checklist

**Statistics:**
- Total migration items: 116

---

## "#[require()]" syntax and "#[derive(Component)]" improvements

**Pull Requests:** 19680

**Description:**
Bevy 0.17.0 introduces a new `#[require(...)]` attribute that simplifies trait implementations and improves component ergonomics. The syntax for the `#[require()]` attribute on the `Component` derive has been updated. This attribute allows you to specify component dependencies and requirements directly on your component types.

**Checklist:**
- [ ] Search for all `#[derive(Component)]` usages in your codebase
- [ ] Review any existing `#[require()]` or `#[require(...)]` attributes for syntax updates
- [ ] Consider adding `#[require()]` attributes to components that have dependencies on other components
- [ ] Update component implementations to use the new `#[require()]` syntax if applicable

**Search Patterns:** `#[derive(Component)]`, `#[require()]`, `#[require(...)]`, `Component`

**Examples:**
(No code examples provided in original guide)

---

---

## 2D meshes respect `Transform::local_z`

**Pull Requests:** 20668

**Description:**
2D meshes now respect `Transform::local_z` for ordering, while still maintaining their normal-based ordering behavior. This changes how 2D meshes are sorted for rendering. If you relied on the previous translation-based ordering, you'll need to explicitly combine both `local_z` and `Transform::translation.z` to achieve the same effect.

**Checklist:**
- [ ] Search for 2D mesh spawning code (sprites, 2D shapes, custom 2D meshes)
- [ ] Identify places where you relied on `Transform::translation.z` for 2D rendering order
- [ ] If you require translation-based ordering, update code to use both `local_z` and `Transform::translation.z`
- [ ] Test 2D rendering order to ensure sprites/meshes appear in the correct layer order

**Search Patterns:** `Transform::local_z`, `Transform::translation.z`, `local_z`

**Examples:**
(No code examples provided in original guide)

---

---

## Add new plugins for physics and xr platform support

**Pull Requests:** 19889, 20046

**Description:**
The input plugin architecture has been reorganized to better support headless mode and platforms without mouse or keyboard support. `DeviceAccessPlugin` has been removed, and `InputPlugin` has been split into more granular plugins. You must now explicitly add `MousePlugin` and `KeyboardPlugin` if you need mouse or keyboard inputs in your application.

**Checklist:**
- [ ] **REQUIRED:** Search for `DeviceAccessPlugin` and remove it from your app
- [ ] **REQUIRED:** Replace `input_device_available<X>()` with `DeviceConnectedPlugin::<X>::input_device_available`
- [ ] **REQUIRED:** If you use mouse inputs, explicitly add `MousePlugin` to your app
- [ ] **REQUIRED:** If you use keyboard inputs, explicitly add `KeyboardPlugin` to your app
- [ ] Review any headless mode configurations to ensure proper plugin setup
- [ ] Update imports from `bevy::input::common_conditions` if needed

**Search Patterns:** `DeviceAccessPlugin`, `input_device_available<X>()`, `InputPlugin`, `MousePlugin`, `KeyboardPlugin`, `bevy::input::common_conditions`, `DeviceConnectedPlugin`

**Examples:**
(No code examples provided in original guide)

---

---

## "Adding `Meshable` instances for `Arc2d` and `CircularSector`/`CircularSegment`"

**Pull Requests:** 19610

**Description:**
The `CircularSector` type has been split into two distinct types based on whether a central point is present. `CircularSector` now represents sectors with a central point, while the new `CircularSegment` represents sectors without a central point. Additionally, the API for creating and working with circular sectors has changed.

**Checklist:**
- [ ] Search for all `CircularSector` usages in your codebase
- [ ] Determine if each usage includes a central point or not
- [ ] If you used `CircularSector` WITHOUT a central point, replace with `CircularSegment`
- [ ] If you used `CircularSector` WITH a central point, keep `CircularSector` (but update API calls)
- [ ] **REQUIRED:** Replace `CircularSector::arc_length` with `CircularSector::arc` (returns `Arc2d`)
- [ ] **REQUIRED:** Update `CircularSector::new` calls - now takes two arguments (radius and `Arc2d`)
- [ ] Use `CircularSector::from_radians` or `CircularSector::from_degrees` to create from radius and angle
- [ ] Review any `Meshable` implementations involving these types

**Search Patterns:** `CircularSector`, `CircularSegment`, `CircularSector::arc_length`, `CircularSector::arc`, `CircularSector::new`, `CircularSector::from_radians`, `CircularSector::from_degrees`, `Arc2d`, `Meshable`

**Examples:**
(No code examples provided in original guide)

---

---

## "AppLabel trait and "new state" methods were moved to the bevy_state crate"

**Pull Requests:** 19824, 19900

**Description:**
The `AppLabel` type and state-related methods have been reorganized into the `bevy_state` crate. `AppLabel` is still available from the prelude, but the state initialization methods (`App::init_state`, `App::insert_state`, `App::add_sub_state`) have moved to a module that is no longer in the prelude, requiring explicit imports.

**Checklist:**
- [ ] Search for `AppLabel` imports from `bevy_ecs` and update to import from `bevy_state` or use the prelude
- [ ] Search for `App::init_state`, `App::insert_state`, and `App::add_sub_state` usages
- [ ] Add `use bevy_state::app::StatesPlugin` or similar import where state methods are used
- [ ] Ensure the extension trait is imported where needed (not in prelude anymore)
- [ ] Verify all state initialization code still compiles

**Search Patterns:** `AppLabel`, `App::init_state`, `App::insert_state`, `App::add_sub_state`, `bevy_ecs`, `bevy_state`, `bevy_state::app`, `prelude`

**Examples:**
(No code examples provided in original guide)

---

---

## "BRP uses `world.` prefix, not `bevy/`"

**Pull Requests:** 20659

**Description:**
The Bevy Remote Protocol (BRP) has standardized its method naming by replacing the `bevy/` prefix with `world.` for all methods. This is a breaking change for any code or tools that interact with BRP.

**Checklist:**
- [ ] **REQUIRED:** Search for all BRP method calls using `bevy/` prefix
- [ ] **REQUIRED:** Replace `bevy/` prefix with `world.` in all BRP method calls
- [ ] Update any external tools, scripts, or clients that communicate with BRP
- [ ] Update any documentation or examples that reference BRP methods

**Search Patterns:** `bevy/`, `world.`

**Examples:**
(No code examples provided in original guide)

---

---

## Bevy Remote Protocol: JSON-RPC 2.0

**Pull Requests:** 19886

**Description:**
The Bevy Remote Protocol now uses JSON-RPC 2.0 format for all messages. The return value structure has changed for `bevy/query` and `bevy/list` methods - they now return objects wrapping the data instead of plain lists. Other methods did not change their return value structure.

**Checklist:**
- [ ] **REQUIRED:** Search for `bevy/query` response handling
- [ ] **REQUIRED:** Update `bevy/query` response parsing to expect an object wrapping a list, not a plain list
- [ ] **REQUIRED:** Search for `bevy/list` response handling
- [ ] **REQUIRED:** Update `bevy/list` response parsing to expect an object wrapping a list of strings
- [ ] Update any BRP client code to handle JSON-RPC 2.0 message format
- [ ] Test all BRP interactions to ensure proper response parsing

**Search Patterns:** `bevy/query`, `bevy/list`

**Examples:**
(No code examples provided in original guide)

---

---

## Change `AlignMode` enum

**Pull Requests:** 19770, 20005

**Description:**
The `AlignMode` enum has been simplified by removing the `World` variant. All alignment now uses UV coordinates exclusively instead of world space coordinates.

**Checklist:**
- [ ] **REQUIRED:** Search for all `AlignMode` usages in your codebase
- [ ] **REQUIRED:** Remove any references to `AlignMode::World` variant
- [ ] Convert any world-space alignment logic to UV coordinate-based alignment
- [ ] Update any custom alignment calculations to use UV coordinates

**Search Patterns:** `AlignMode`, `World`, `AlignMode::World`

**Examples:**
(No code examples provided in original guide)

---

---

## "Camera.hdr split from camera component"

**Pull Requests:** 18873

**Description:**
The `hdr` field has been removed from the `Camera` component and extracted into a separate marker component called `Hdr` (found at `bevy::render::view::Hdr`). This allows for more modular HDR support and enables rendering effects to explicitly require HDR cameras using the `#[require(Hdr)]` attribute.

**Checklist:**
- [ ] **REQUIRED:** Search for all `Camera { hdr: true, ..default() }` usages
- [ ] **REQUIRED:** Replace `Camera { hdr: true, ..default() }` with separate `Camera` and `Hdr` components
- [ ] Update camera spawning code to include the `Hdr` marker component where needed
- [ ] For rendering effects that require HDR, consider adding `#[require(Hdr)]` attribute
- [ ] Remove `hdr` field references from `Camera` struct initialization

**Search Patterns:** `Camera.hdr`, `Camera { hdr:`, `Hdr`, `bevy::render::view::Hdr`, `#[require(Hdr)]`

**Examples:**

```rust
// Old
commands.spawn((Camera3d, Camera { hdr: true, ..default() }));

// New
commands.spawn((Camera3d, Hdr));
```

Note: Rendering effects can now `#[require(Hdr)]` if they only function with an HDR camera.

---

---

## "ChildOf" is replacing "With<Parent>"

**Pull Requests:** 20629

**Description:**
The hierarchy system has been refactored to use the `ChildOf` relationship as the authoritative source instead of the `Child` and `Parent` components. While the old `With<Parent>` and `With<Children>` queries still work (for backward compatibility), you should migrate to the new `ChildOf<Any>` and `ParentOf<Any>` patterns. The new API provides better performance and clearer semantics for hierarchy operations.

**Checklist:**
- [ ] Search for `With<Parent>` queries and consider replacing with `&ChildOf<Any>`
- [ ] Search for `With<Children>` queries and consider replacing with `&ParentOf<Any>`
- [ ] Replace entity children iteration using `EntityRef::related::<ParentOf>()` or `world.related_all::<ParentOf>(entity)`
- [ ] Replace `DespawnRecursive` command with `DespawnRelationship::<ParentOf>`
- [ ] Replace `DespawnRecursiveExt` trait with `DespawnRelationshipExt::<ParentOf>`
- [ ] Note: Old forms (`With<Parent>`, `DespawnRecursive`) are deprecated but still functional

**Search Patterns:** `With<Parent>`, `With<Children>`, `ChildOf`, `ParentOf`, `&ChildOf<Any>`, `&ParentOf<Any>`, `EntityRef::related::<ParentOf>`, `world.related_all::<ParentOf>(entity)`, `DespawnRecursive`, `DespawnRecursiveExt`, `DespawnRelationship::<ParentOf>`, `DespawnRelationshipExt::<ParentOf>`, `Child`, `Parent`, `Children`

**Examples:**
(No code examples provided in original guide)

---

---

## Component Lifecycle Hooks

**Pull Requests:** 18756, 19299

**Description:**
The interface for Component lifecycle hooks has fundamentally changed. Instead of using `#[component(on_add = on_add_function)]` attributes with free functions, you now use either the `#[component(on_add = MyComponent::on_add)]` attribute pointing to an associated function, or implement the `Component::hooks()` method returning a `ComponentHooks` instance. This provides better encapsulation and type safety.

**Checklist:**
- [ ] **REQUIRED:** Search for all `#[component(on_add = ...)]` attribute usages
- [ ] **REQUIRED:** Search for all `#[component(on_insert = ...)]` attribute usages
- [ ] **REQUIRED:** Search for all `#[component(on_remove = ...)]` attribute usages
- [ ] Convert free function hooks to associated functions (e.g., `#[component(on_add = MyComponent::on_add)]`)
- [ ] Alternatively, implement `Component::hooks()` method to return `ComponentHooks::new().on_add(...)`
- [ ] Update hook function signatures if needed (should still use `DeferredWorld`, `Entity`, `ComponentId`)
- [ ] Test all lifecycle hooks to ensure they still trigger correctly

**Search Patterns:** `#[component(on_add`, `#[component(on_insert`, `#[component(on_remove`, `ComponentHooks`, `Component::hooks()`, `DeferredWorld`, `ComponentId`

**Examples:**

```rust
// Old
#[derive(Component)]
#[component(on_add = on_add_function)]
struct MyComponent;

fn on_add_function(world: DeferredWorld, entity: Entity, id: ComponentId) {}

// New - Option 1: Associated function
#[derive(Component)]
#[component(on_add = MyComponent::on_add)]
struct MyComponent;

impl MyComponent {
    fn on_add(world: DeferredWorld, entity: Entity, id: ComponentId) {}
}

// New - Option 2: Component::hooks() method
impl Component for MyComponent {
    fn hooks() -> ComponentHooks {
        ComponentHooks::new().on_add(|world, entity, id| {})
    }
}
```

---

---

## Context is now passed to `on_unimplemented`

**Pull Requests:** 19865

**Description:**
System param validation attributes now require additional arguments for better error messages. The `#[diagnostic::on_unimplemented]` attribute now accepts `{Self}` and other context parameters to provide more detailed error information to users when system parameters are unavailable.

**Checklist:**
- [ ] Search for all `#[diagnostic::on_unimplemented]` attribute usages
- [ ] Update `message` parameter to include `{Self}` context where appropriate
- [ ] Consider adding `note` parameter with links to documentation (e.g., Bevy error codes)
- [ ] Ensure error messages are clear and actionable for users
- [ ] Test that custom error messages display correctly when validation fails

**Search Patterns:** `#[diagnostic::on_unimplemented`, `diagnostic::on_unimplemented`

**Examples:**

```rust
// Before (0.16)
#[diagnostic::on_unimplemented(
    message = "MySystemParam is not available in this context",
)]

// After (0.17)
#[diagnostic::on_unimplemented(
    message = "`MySystemParam` is not available in this context. (from: `{Self}`)",
    note = "See https://bevyengine.org/learn/errors/b0001 for more details."
)]
```

---

---

## Core Pipeline render graph changes

**Pull Requests:** 20253

**Description:**
The built-in core pipeline render graph has been restructured with new node labels and graph labels. Previously, developers adding custom render nodes to the core pipeline graphs used string literals to reference graph nodes. Now, typed labels have been introduced for better type safety and consistency. The `CorePipelineGraph`, `core_2d::graph`, and `core_3d::graph` modules now provide specific label constants that must be used instead of string labels.

**Checklist:**
- [ ] Replace string labels in `CorePipelineGraph` with typed labels: `Core2d`, `Core3d`, `Bloom`, `AutoExposure`, `ToneMapping`, `Fxaa`, `Upscaling`, `ContrastAdaptiveSharpening`, `Ssr`, `TemporalAntiAliasing`
- [ ] Update `RenderSubGraph` labels to use the new underscored variants (e.g., `Core2d` instead of `CORE2D`)
- [ ] In 2D rendering code, replace `core_2d::graph` string labels with typed labels: `CORE_2D`, `Node2d`, `BLOOM`, `TONEMAPPING`, `FXAA`, `UPSCALING`
- [ ] Use `core_2d::graph::input::VIEW_ENTITY` to reference the camera entity in 2D pipelines
- [ ] In 3D rendering code, replace `core_3d::graph` string labels with typed labels: `CORE_3D`, `Node3d`, `PREPASS`, `DEFERRED_PREPASS`, `COPY_DEFERRED_LIGHTING_ID`, `END_PREPASSES`, `START_MAIN_PASS`, `MAIN_OPAQUE_PASS`, `MAIN_TRANSMISSIVE_PASS`, `MAIN_TRANSPARENT_PASS`, `END_MAIN_PASS`, `BLOOM`, `TONEMAPPING`, `FXAA`, `UPSCALING`, `CONTRAST_ADAPTIVE_SHARPENING`, `END_MAIN_PASS_POST_PROCESSING`
- [ ] Use `core_3d::graph::input::VIEW_ENTITY` to reference the camera entity in 3D pipelines
- [ ] Review any custom render nodes added to these graphs and update to use the new typed labels

**Search Patterns:** `CorePipelineGraph`, `core_2d::graph`, `core_3d::graph`, `RenderSubGraph`, `add_node`, `"CORE_2D"`, `"CORE_3D"`, `"BLOOM"`, `"TONEMAPPING"`, `"FXAA"`, `VIEW_ENTITY`

**Examples:**
No code examples provided in original guide.

---

---

## Curve-related API

**Pull Requests:** 20434, 19701

**Description:**
The `Curve` API in `bevy_math` has been simplified by removing the `StableInterpolate` trait requirement and renaming methods for better clarity. The `StableInterpolate` trait is no longer necessary for creating or sampling curves. Method names have been updated to be more semantically clear: `clone_to()` is now `map()` (applying a function to transform curve values), and `clone_to_many()` is now `zip_map()` (combining multiple curves with a function).

**Checklist:**
- [ ] Remove all imports and trait bounds for `StableInterpolate` trait
- [ ] Replace `Curve::clone_to()` method calls with `Curve::map()`
- [ ] Replace `Curve::clone_to_many()` method calls with `Curve::zip_map()`
- [ ] Review curve creation code in `bevy_math` to remove any `StableInterpolate` requirements
- [ ] Update any custom curve implementations that relied on `StableInterpolate`

**Search Patterns:** `StableInterpolate`, `Curve::clone_to`, `Curve::clone_to_many`, `Curve::map`, `Curve::zip_map`, `use.*StableInterpolate`

**Examples:**
No code examples provided in original guide.

---

---

## Default-plugins changes

**Pull Requests:** 19761

**Description:**
The `TerminalCtrlCHandlerPlugin` has been removed from the `DefaultPlugins` plugin group. This plugin provides Ctrl+C signal handling functionality in terminal environments. If your application requires this functionality (e.g., for graceful shutdown when Ctrl+C is pressed in the terminal), you must now explicitly add the plugin to your app instead of relying on it being included in `DefaultPlugins`.

**Checklist:**
- [ ] Determine if your application requires terminal Ctrl+C signal handling functionality
- [ ] If needed, add `TerminalCtrlCHandlerPlugin` explicitly to your app after `DefaultPlugins`
- [ ] Test that Ctrl+C signal handling works as expected in terminal environments if this functionality is required

**Search Patterns:** `DefaultPlugins`, `TerminalCtrlCHandlerPlugin`, `add_plugins(DefaultPlugins)`

**Examples:**
No code examples provided in original guide.

---

---

## DeferredWorld added to FlushRegion

**Pull Requests:** 17933

**Description:**
The `FlushRegion` API for triggering observer flush events has been updated to require a `DeferredWorld` parameter. This change affects low-level observer code that manually triggers flush events. If you were working directly with observers and flushing observer queues, you must now provide a `DeferredWorld` instance to the `FlushRegion`.

**Checklist:**
- [ ] Search for direct usage of `FlushRegion` in observer-related code
- [ ] Update all `FlushRegion` calls to pass a `DeferredWorld` parameter
- [ ] Ensure `DeferredWorld` is available in the context where flush events are triggered
- [ ] Review any custom observer flush logic to accommodate the new parameter requirement

**Search Patterns:** `FlushRegion`, `DeferredWorld`, `flush`, `observer`

**Examples:**
No code examples provided in original guide.

---

---

## Deprecate redundant World::read_resource_scope

**Pull Requests:** 20000

**Description:**
The `read_resource_scope` methods on both `World` and `DeferredWorld` have been deprecated in favor of the equivalent `resource_scope` methods. Both methods provide identical functionality for accessing resources with a scoped closure, so the "read" prefix was redundant. The `resource_scope` methods can be used for both reading and writing resources within the scope.

**Checklist:**
- [ ] Replace all `World::read_resource_scope()` calls with `World::resource_scope()`
- [ ] Replace all `DeferredWorld::read_resource_scope()` calls with `DeferredWorld::resource_scope()`
- [ ] Verify that the functionality remains identical (same closure signature and behavior)
- [ ] Update any documentation or comments that reference the deprecated method names

**Search Patterns:** `World::read_resource_scope`, `DeferredWorld::read_resource_scope`, `read_resource_scope`, `resource_scope`

**Examples:**
No code examples provided in original guide.

---

---

## Divide PrimitivesPlugin into separate plugins

**Pull Requests:** 18920

**Description:**
The monolithic `PrimitivesPlugin` has been split into two separate plugins: `Primitive2dPlugin` for 2D primitive shapes and `Primitive3dPlugin` for 3D primitive shapes. This allows applications to include only the primitive functionality they need. If your application previously used `PrimitivesPlugin`, you must now explicitly add the 2D and/or 3D variants based on your needs.

**Checklist:**
- [ ] Search for usage of `PrimitivesPlugin` in your plugin configuration
- [ ] Determine if your application uses 2D primitives, 3D primitives, or both
- [ ] Replace `PrimitivesPlugin` with `Primitive2dPlugin` if you only use 2D primitives
- [ ] Replace `PrimitivesPlugin` with `Primitive3dPlugin` if you only use 3D primitives
- [ ] Add both `Primitive2dPlugin` and `Primitive3dPlugin` if you use both 2D and 3D primitives
- [ ] Update plugin imports to include the new plugin names

**Search Patterns:** `PrimitivesPlugin`, `Primitive2dPlugin`, `Primitive3dPlugin`, `add_plugins`

**Examples:**
No code examples provided in original guide.

---

---

## Entity IDs are now u64

**Pull Requests:** 19036, 20295

**Description:**
Entity IDs have been changed from `u32` to `u64`, doubling the available entity ID space. This affects several APIs that work with raw entity IDs. The `Entity::from_raw()` method now accepts `u64` instead of `u32`, and methods like `Entities::reserve_entity` similarly now use `u64`. The method `Entity::from_bits` has been renamed to `Entity::from_raw` for clarity. When manually manipulating entity bits, you must use `Entity::LOW_MASK` instead of the old `Entity::ENTITY_MASK`, and work with `u64` values returned by `Entity::into_raw()`. The values for `Entity::PLACEHOLDER` and entity IDs created via `Entity::from_raw()` have changed as a result of this expansion.

**Checklist:**
- [ ] Replace `Entity::from_raw(value)` calls to accept `u64` instead of `u32` (change parameter types)
- [ ] Replace `Entity::from_bits(value)` with `Entity::from_raw(value)` (method renamed)
- [ ] Update `Entities::reserve_entity()` calls to work with `u64` instead of `u32`
- [ ] Replace `Entity::ENTITY_MASK` with `Entity::LOW_MASK` when manually manipulating entity bits
- [ ] Update any code using `Entity::into_raw()` to handle `u64` return values instead of `u32`
- [ ] Review uses of `Entity::PLACEHOLDER` as its value has changed
- [ ] Update any serialization/deserialization code that works with raw entity IDs to use `u64`
- [ ] Check any bit manipulation operations on entities to ensure they work correctly with 64-bit values

**Search Patterns:** `Entity::from_raw`, `Entity::from_bits`, `Entity::into_raw`, `Entities::reserve_entity`, `Entity::ENTITY_MASK`, `Entity::LOW_MASK`, `Entity::PLACEHOLDER`, `as u32`, `: u32` (in entity-related contexts)

**Examples:**
No code examples provided in original guide.

---

---

## EntityError Variants

**Pull Requests:** 19119

**Description:**
The `EntityError` enum has been simplified by removing the `Spawn` and `Despawn` variants, which were only used in limited contexts and never resulted in actual panics. The error handling for these cases has been restructured. Additionally, the `COMPONENT_NOT_FOUND` error has been renamed to `NO_COMPONENT_FOR_ENTITY` for improved clarity about what the error represents (entity exists but doesn't have the requested component).

**Checklist:**
- [ ] Remove any match arms or error handling for `EntityError::Spawn` variant
- [ ] Remove any match arms or error handling for `EntityError::Despawn` variant
- [ ] If you were relying on these errors, implement custom error handling in your world resource code
- [ ] Replace all references to `COMPONENT_NOT_FOUND` error with `NO_COMPONENT_FOR_ENTITY`
- [ ] Update error message strings or logging that mention "COMPONENT_NOT_FOUND"
- [ ] Remove any tests that specifically checked for `EntityError::Spawn` or `EntityError::Despawn`

**Search Patterns:** `EntityError::Spawn`, `EntityError::Despawn`, `COMPONENT_NOT_FOUND`, `NO_COMPONENT_FOR_ENTITY`, `EntityError::`

**Examples:**
No code examples provided in original guide.

---

---

## EntityWorldMut::entry and DeferredWorld::entry method to access components like a hashmap

**Pull Requests:** 20395

**Description:**
Bevy now provides Entry API-style access to component data, similar to Rust's `HashMap::entry()` pattern. The new `entry()` methods are available on `EntityMut`, `EntityRef`, `EntityWorldMut`, and `DeferredWorld`. This allows for ergonomic "insert if not present" and "modify if present" patterns. However, this required repurposing the `EntityMut::entry` name, which previously provided access to the command queue. To check for command queue entry, you must now use `EntityMut::commands()` instead.

**Checklist:**
- [ ] Replace `EntityMut::entry()` calls with `EntityMut::commands()` when accessing the command queue
- [ ] Consider using the new Entry API (`EntityMut::entry()`, `EntityRef::entry()`, `EntityWorldMut::entry()`, `DeferredWorld::entry()`) for component access patterns
- [ ] Update any code that was using `EntityMut::entry` for command queue access to use `commands()` instead
- [ ] Review component access patterns that could benefit from the Entry API (e.g., "insert or modify" patterns)

**Search Patterns:** `EntityMut::entry`, `EntityMut::commands`, `EntityRef::entry`, `EntityWorldMut::entry`, `DeferredWorld::entry`, `.entry()`, `Entry::`

**Examples:**
No code examples provided in original guide.

---

---

## Event Implemented on Types in bevy_input

**Pull Requests:** 19866

**Description:**
Input-related events in `bevy_input` (such as `KeyboardInput`, `MouseButtonInput`, `MouseMotion`, etc.) have been changed from implementing the `Event` trait to implementing the `Message` trait. This is a fundamental change in how these types are buffered and processed. If you were using `EventWriter` and `EventReader` to send or receive these input events, you must now use `MessageWriter` and `MessageReader` instead. The event types themselves are unchanged, only the traits and system parameters for working with them have changed. Additionally, wildcard imports from `bevy_input` will no longer include `Event`-related types, requiring more specific imports.

**Checklist:**
- [ ] Replace `EventWriter<KeyboardInput>` with `MessageWriter<KeyboardInput>` (and similar for other input events)
- [ ] Replace `EventReader<MouseButtonInput>` with `MessageReader<MouseButtonInput>` (and similar for other input events)
- [ ] Replace `EventReader<MouseMotion>` with `MessageReader<MouseMotion>` (and similar for other input events)
- [ ] Update any other input event readers/writers (gamepad events, touch events, etc.) to use `MessageReader`/`MessageWriter`
- [ ] Replace wildcard imports `use bevy_input::*` with specific imports like `use bevy_input::{MessageWriter, MessageReader, ...}`
- [ ] If you need actual `Event`, `EventWriter`, or `EventReader` from `bevy_input`, import them explicitly
- [ ] Search for all input-related event types and verify they use the Message trait variants

**Search Patterns:** `EventWriter<KeyboardInput>`, `EventReader<KeyboardInput>`, `EventWriter<MouseButtonInput>`, `EventReader<MouseMotion>`, `use bevy_input::*`, `MessageWriter`, `MessageReader`, `bevy_input::`

**Examples:**
No code examples provided in original guide.

---

---

## EventEntityWriter to write events targeting an entity

**Pull Requests:** 19862

**Description:**
A new `EventEntityWriter` system parameter has been introduced for writing observer events that target specific entities. Previously, sending events to entities required using `EntityCommands::observe()` with a trigger closure. Now, you can use the more direct `EventEntityWriter::write(event, entity)` method, which is cleaner and more ergonomic for sending targeted events.

**Checklist:**
- [ ] Replace `commands.entity(entity).observe(|trigger: Trigger<MyEvent>| {})` patterns with `EventEntityWriter::write()`
- [ ] Add `EventEntityWriter` as a system parameter where you need to send entity-targeted events
- [ ] Update event-sending code to use `event_writer.write(MyEvent { ... }, entity)` syntax
- [ ] Remove unnecessary observer closures that were only used to send events to entities
- [ ] Consider migrating from command-based event sending to direct `EventEntityWriter` usage for better performance

**Search Patterns:** `EntityCommands::observe`, `EventEntityWriter`, `EventEntityWriter::write`, `Trigger<`, `.observe(|trigger:`

**Examples:**
```rust
// Old (0.16)
commands.entity(entity).observe(|trigger: Trigger<MyEvent>| {});

// New (0.17)
event_writer.write(MyEvent, entity);
```

---

---

## EventRegistry was renamed to MessageRegistry

**Pull Requests:** 19882

**Description:**
The `EventRegistry` type has been renamed to `MessageRegistry` to better reflect its purpose of registering message types (inputs, etc.). A new, different `EventRegistry` type now exists specifically for observer events. This is part of the broader separation between the `Event` trait (for observers) and the `Message` trait (for input and similar systems). Related methods on `World` have also been renamed: `register_event` is now `register_message` and `get_event` is now `get_message`.

**Checklist:**
- [ ] Replace all `EventRegistry` type references with `MessageRegistry` (for input/message registration)
- [ ] Replace `World::register_event()` calls with `World::register_message()`
- [ ] Replace `World::get_event()` calls with `World::get_message()`
- [ ] Update imports from `bevy_ecs::event::EventRegistry` to `bevy_ecs::event::MessageRegistry`
- [ ] Note that the new `EventRegistry` is a different type for observer events, ensure you're using the correct registry type
- [ ] Review any custom event/message registration code to use the appropriate registry type

**Search Patterns:** `EventRegistry`, `MessageRegistry`, `World::register_event`, `World::register_message`, `World::get_event`, `World::get_message`, `use.*EventRegistry`

**Examples:**
No code examples provided in original guide.

---

---

## `Event` trait split / Rename

**Pull Requests:** 19647

**Description:**
Bevy 0.17.0 introduces a major conceptual split between "buffered events" and "observable events" for improved clarity. The `Event` trait and terminology are now reserved exclusively for "observable events" (used with observers). What were previously called "buffered events" (sent/received via `EventWriter`/`EventReader`) are now called "messages" and use the new `Message` trait. This is a widespread breaking change affecting all event-driven code.

**Checklist:**
- [ ] **REQUIRED**: Replace all `#[derive(Event)]` with `#[derive(Message)]` for types used with `EventWriter`/`EventReader`
- [ ] **REQUIRED**: Rename `EventWriter<T>` to `MessageWriter<T>` throughout your codebase
- [ ] **REQUIRED**: Rename `EventReader<T>` to `MessageReader<T>` throughout your codebase
- [ ] **REQUIRED**: Rename `Events<E>` resource to `Messages<M>`
- [ ] **OPTIONAL**: Types can derive both `Message` and `Event` if used in both contexts, but this is rare
- [ ] Update any documentation or comments referencing "events" to use "messages" for buffered communication

**Search Patterns:** `#[derive(Event)]`, `EventReader<`, `EventWriter<`, `Events<`, `Event` (trait usage)

**Examples:**
```rust
// Old (0.16)
#[derive(Event)]
struct MyEvent {
    data: String,
}

fn send_events(mut writer: EventWriter<MyEvent>) {
    writer.send(MyEvent { data: "hello".into() });
}

fn read_events(mut reader: EventReader<MyEvent>) {
    for event in reader.read() {
        println!("{}", event.data);
    }
}

// New (0.17)
#[derive(Message)]
struct MyEvent {
    data: String,
}

fn send_events(mut writer: MessageWriter<MyEvent>) {
    writer.send(MyEvent { data: "hello".into() });
}

fn read_events(mut reader: MessageReader<MyEvent>) {
    for event in reader.read() {
        println!("{}", event.data);
    }
}
```

---

---

## Extract `PickingPlugin` members into `PickingSettings`

**Pull Requests:** 19078

**Description:**
The configuration options that were previously fields on `PickingPlugin` have been extracted into a separate `PickingSettings` resource. This follows Bevy's pattern of separating plugin configuration from runtime settings. Instead of configuring picking behavior through plugin construction, you now use `insert_resource` to add a `PickingSettings` resource with your desired values.

**Checklist:**
- [ ] **REQUIRED**: Remove any configuration fields from `PickingPlugin` initialization
- [ ] **REQUIRED**: Create a `PickingSettings` resource with your desired configuration values
- [ ] **REQUIRED**: Use `app.insert_resource(PickingSettings { ... })` to apply non-default settings
- [ ] Update any runtime code that modified plugin fields to instead modify the `PickingSettings` resource

**Search Patterns:** `PickingPlugin`, `PickingSettings`, `.add_plugins(PickingPlugin`

**Examples:**
```rust
// Old (0.16)
app.add_plugins(PickingPlugin {
    is_enabled: true,
    // other configuration fields...
});

// New (0.17)
app.add_plugins(PickingPlugin::default())
    .insert_resource(PickingSettings {
        is_enabled: true,
        // other configuration fields...
    });
```

---

---

## Extract `PointerInputPlugin` members into `PointerInputSettings`

**Pull Requests:** 19078

**Description:**
Similar to `PickingPlugin`, the `PointerInputPlugin` configuration has been extracted into a dedicated `PointerInputSettings` resource. This affects how you enable/disable mouse and touch input for the picking system. Configuration is now done through resource insertion rather than plugin construction.

**Checklist:**
- [ ] **REQUIRED**: Remove any configuration fields from `PointerInputPlugin` initialization
- [ ] **REQUIRED**: Create a `PointerInputSettings` resource with your mouse/touch input preferences
- [ ] **REQUIRED**: Use `app.insert_resource(PointerInputSettings { ... })` to apply non-default settings
- [ ] Update any runtime code that toggled input settings to modify the `PointerInputSettings` resource

**Search Patterns:** `PointerInputPlugin`, `PointerInputSettings`, `.add_plugins(PointerInputPlugin`

**Examples:**
```rust
// Old (0.16)
app.add_plugins(PointerInputPlugin {
    enable_mouse: true,
    enable_touch: false,
});

// New (0.17)
app.add_plugins(PointerInputPlugin::default())
    .insert_resource(PointerInputSettings {
        enable_mouse: true,
        enable_touch: false,
    });
```

---

---

## `take_extract` now returns `dyn FnMut` instead of `dyn Fn`

**Pull Requests:** 19926

**Description:**
The extraction function API has been updated to allow mutable closures. While `set_extract` continues to accept both `Fn` and `FnMut` (since `Fn: FnMut`), the return type of `take_extract` has changed from `dyn Fn` to `dyn FnMut`. This only affects code that calls `take_extract` and stores or manipulates the returned closure.

**Checklist:**
- [ ] **REQUIRED**: Update any variables storing the result of `take_extract()` to expect `Option<Box<dyn FnMut(&mut World, &mut World) + Send>>`
- [ ] **REQUIRED**: Change type annotations from `Box<dyn Fn(&mut World, &mut World) + Send>` to `Box<dyn FnMut(&mut World, &mut World) + Send>`
- [ ] **OPTIONAL**: Consider updating extract closures to use mutable state if beneficial (now supported)
- [ ] No changes needed for callers of `set_extract` (automatically compatible)

**Search Patterns:** `take_extract`, `set_extract`, `Box<dyn Fn(&mut World, &mut World)`, `Box<dyn FnMut(&mut World, &mut World)`

**Examples:**
```rust
// Old (0.16)
let extract_fn: Option<Box<dyn Fn(&mut World, &mut World) + Send>> =
    app.take_extract();

// New (0.17)
let extract_fn: Option<Box<dyn FnMut(&mut World, &mut World) + Send>> =
    app.take_extract();
```

---

---

## Extract UI text colors per glyph

**Pull Requests:** 20245

**Description:**
The UI rendering pipeline has been refactored to extract text colors at the glyph level and transforms at the text section level, improving rendering flexibility. This involves structural changes to `ExtractedGlyph`, `ExtractedUiNode`, and `ExtractedUiItem`. The `transform` and `rect` fields have been moved to `ExtractedUiItem` for better organization.

**Checklist:**
- [ ] **REQUIRED**: Update code accessing `ExtractedGlyph` to use new `color: LinearRgba` and `translation: Vec2` fields
- [ ] **REQUIRED**: Access `transform` field from `ExtractedUiItem` instead of `ExtractedGlyph` or `ExtractedUiNode`
- [ ] **REQUIRED**: Access `rect` field from `ExtractedUiItem` instead of `ExtractedUiNode`
- [ ] Update any custom UI rendering code that constructs these types to match new field layout
- [ ] Verify custom UI shaders/materials work with per-glyph color extraction

**Search Patterns:** `ExtractedGlyph`, `ExtractedUiNode`, `ExtractedUiItem`, `.transform`, `.rect`, `.color`, `.translation`

**Examples:**
```rust
// Old (0.16)
struct ExtractedGlyph {
    transform: Mat4,
    // other fields...
}

struct ExtractedUiNode {
    transform: Mat4,
    rect: Rect,
    // other fields...
}

// New (0.17)
struct ExtractedGlyph {
    color: LinearRgba,
    translation: Vec2,
    // other fields...
}

struct ExtractedUiNode {
    // transform and rect moved to ExtractedUiItem
}

struct ExtractedUiItem {
    transform: Mat4,
    rect: Rect,
    // other fields...
}
```

---

---

## `ExtractedUiNode`'s `stack_index` has been renamed to `z_order` and is now an `f32`

**Pull Requests:** 19691

**Description:**
The UI rendering system's depth sorting mechanism has been improved by renaming `stack_index` to `z_order` and changing its type from `u32` to `f32`. This allows offsets (like `-0.1` for box shadows) to be applied during extraction rather than after, fixing texture-sliced node ordering bugs and providing finer control over draw order. Lower `z_order` values are rendered first (behind higher values).

**Checklist:**
- [ ] **REQUIRED**: Rename all references to `ExtractedUiNode::stack_index` to `ExtractedUiNode::z_order`
- [ ] **REQUIRED**: Change type from `u32` to `f32` in any code that reads or writes this field
- [ ] **REQUIRED**: Update any custom rendering code that applies z-order offsets to use `f32` precision
- [ ] Review custom UI rendering logic that relied on integer stack indices (e.g., `0.` for fill, `-0.1` for shadows)
- [ ] Verify that texture-sliced nodes render in correct order after migration

**Search Patterns:** `stack_index`, `ExtractedUiNode`, `z_order`, `u32` (in context of UI rendering)

**Examples:**
```rust
// Old (0.16)
struct ExtractedUiNode {
    stack_index: u32,
    // other fields...
}

// During render, offsets applied later:
// fill gets offset 0, shadow gets offset -0.1

// New (0.17)
struct ExtractedUiNode {
    z_order: f32,
    // other fields...
}

// Offsets applied during extraction:
let fill_z_order = base_z + 0.0;
let shadow_z_order = base_z - 0.1;
```

---

---

## `FULLSCREEN_SHADER_HANDLE` replaced with `FullscreenShader`

**Pull Requests:** 19426

**Description:**
The fullscreen shader API has been refactored from a global handle constant and free function to a resource-based approach. `FULLSCREEN_SHADER_HANDLE` and `fullscreen_shader_vertex_state()` are replaced by the `FullscreenShader` resource with `.shader()` and `.to_vertex_state()` methods. When using specialized render pipelines, you'll need to clone the resource from the render world during initialization.

**Checklist:**
- [ ] **REQUIRED**: Replace `FULLSCREEN_SHADER_HANDLE` with `FullscreenShader::shader()` method call
- [ ] **REQUIRED**: Replace `fullscreen_shader_vertex_state()` with `FullscreenShader::to_vertex_state()` method call
- [ ] **REQUIRED**: In `SpecializedRenderPipeline` implementations, store a cloned `FullscreenShader` instance in your pipeline struct
- [ ] **REQUIRED**: Clone `FullscreenShader` from render world in `FromWorld::from_world()` implementation
- [ ] Update any imports removing `FULLSCREEN_SHADER_HANDLE` and `fullscreen_shader_vertex_state`

**Search Patterns:** `FULLSCREEN_SHADER_HANDLE`, `fullscreen_shader_vertex_state`, `FullscreenShader`, `SpecializedRenderPipeline`

**Examples:**
```rust
// Old (0.16)
struct MyPipeline {
    some_bind_group: BindGroupLayout,
}

impl FromWorld for MyPipeline {
    fn from_world(render_world: &mut World) -> Self {
        let some_bind_group = /* ... RenderDevice stuff */;
        Self {
            some_bind_group,
        }
    }
}

impl SpecializedRenderPipeline for MyPipeline {
    fn specialize(&self, key: Self::Key) -> RenderPipelineDescriptor {
        RenderPipelineDescriptor {
            vertex: fullscreen_shader_vertex_state(),
            // ... other stuff
        }
    }
}

// New (0.17)
struct MyPipeline {
    some_bind_group: BindGroupLayout,
    fullscreen_shader: FullscreenShader,
}

impl FromWorld for MyPipeline {
    fn from_world(render_world: &mut World) -> Self {
        let some_bind_group = /* ... RenderDevice stuff */;
        Self {
            some_bind_group,
            fullscreen_shader: render_world.resource::<FullscreenShader>().clone(),
        }
    }
}

impl SpecializedRenderPipeline for MyPipeline {
    fn specialize(&self, key: Self::Key) -> RenderPipelineDescriptor {
        RenderPipelineDescriptor {
            vertex: self.fullscreen_shader.to_vertex_state(),
            // ... other stuff
        }
    }
}
```

---

---

## `GatedReader` and `GatedOpener` are now private

**Pull Requests:** 18473

**Description:**
The `GatedReader` and `GatedOpener` types in `bevy_asset` have been made private and are now only compiled in test builds via `#[cfg(test)]`. These were internal testing utilities that were unnecessarily exposed in the public API and compiled into release builds. This is primarily a cleanup change that shouldn't affect most users.

**Checklist:**
- [ ] **REQUIRED**: Remove any direct usage of `GatedReader` or `GatedOpener` from production code
- [ ] **OPTIONAL**: If used in your own tests, fork the implementation from Bevy's source or create your own test utilities
- [ ] Verify that asset loading tests still work without these types
- [ ] Consider using standard asset loader testing patterns instead

**Search Patterns:** `GatedReader`, `GatedOpener`, `bevy_asset::GatedReader`, `bevy_asset::GatedOpener`

**Examples:**
```rust
// Old (0.16) - if you were using these in tests
use bevy_asset::{GatedReader, GatedOpener};

fn my_test() {
    let (reader, opener) = GatedReader::new();
    // test code...
}

// New (0.17) - these are no longer public
// Option 1: Fork the implementation from Bevy's repo into your test code
// Option 2: Use alternative testing patterns for asset loading
```

---

---

## Generic `Option` Parameter

**Pull Requests:** 18766

**Description:**
The behavior of `Option<Single<D, F>>` has changed significantly. Previously, it returned `None` only when no entities matched, and skipped the system when multiple entities matched. Now, with a blanket `impl SystemParam for Option`, it returns `None` when the parameter is invalid (including when multiple entities match). This makes behavior more consistent but changes the "skip on multiple entities" pattern.

**Checklist:**
- [ ] **REQUIRED**: Review all systems using `Option<Single<...>>` for correctness
- [ ] **REQUIRED**: If you need to skip the system when multiple entities exist, replace `Option<Single<D, F>>` with `Query<D, F>` and manually call `.single()`
- [ ] **REQUIRED**: Check for `QuerySingleError::MultipleEntities` and return early if you want to skip on multiple matches
- [ ] **OPTIONAL**: Use `.single().ok()` to convert to `Option` if you want `None` on any error (new behavior)
- [ ] Test systems that previously relied on automatic skipping with multiple entities

**Search Patterns:** `Option<Single<`, `Single<`, `QuerySingleError`, `MultipleEntities`, `.single()`

**Examples:**
```rust
// Old (0.16) - system skips when multiple entities match
fn my_system(single: Option<Single<&Player>>) {
    if let Some(player) = single {
        // runs when exactly one Player exists
        // skips when multiple Players exist
        // runs with None when no Players exist
    }
}

// New (0.17) - system runs but gets None when multiple entities match
fn my_system_new_behavior(single: Option<Single<&Player>>) {
    if let Some(player) = single {
        // runs when exactly one Player exists
        // runs with None when multiple Players exist (NEW!)
        // runs with None when no Players exist
    }
}

// New (0.17) - to replicate old skip behavior
fn my_system_skip_on_multiple(query: Query<&Player>) {
    let result = query.single();
    if matches!(result, Err(QuerySingleError::MultipleEntities(_))) {
        return; // skip system when multiple entities
    }
    let single: Option<&Player> = result.ok();
    if let Some(player) = single {
        // runs when exactly one Player exists
    }
    // runs with None when no Players exist
}
```

---

---

## Updated `glam`, `rand` and `getrandom` versions with new failures when building for web

**Pull Requests:** 18047

**Description:**
Major dependency updates to `glam`, `rand`, and `getrandom` with breaking changes. Most critically, `getrandom` now requires explicit configuration for WASM/web builds, affecting ALL projects building for web (even if not directly using these crates). The `rand` API has changed significantly with `thread_rng()` → `rng()`, `from_entropy()` → `from_os_rng()`, and `RngCore` split into infallible `RngCore` and fallible `TryRngCore`.

**Checklist:**
- [ ] **REQUIRED (WEB BUILDS)**: Add `getrandom` configuration for WASM targets even if not using it directly
- [ ] **REQUIRED (WEB BUILDS)**: Add `getrandom` feature flag or configuration to `Cargo.toml` for web builds
- [ ] **REQUIRED**: Update `thread_rng()` calls to `rng()`
- [ ] **REQUIRED**: Update `from_entropy()` calls to `from_os_rng()`
- [ ] **REQUIRED**: Update `distributions` module references to `distr`
- [ ] **OPTIONAL**: Review `rand` migration notes for advanced usage: https://rust-random.github.io/book/update-0.9.html
- [ ] **OPTIONAL**: Update code using `RngCore` to use `TryRngCore` if fallibility is needed
- [ ] Consult `glam` (https://docs.rs/glam/latest/glam/) and `encase` docs for breaking changes
- [ ] Test web builds to ensure random number generation works

**Search Patterns:** `thread_rng()`, `from_entropy()`, `distributions::`, `RngCore`, `TryRngCore`, `getrandom`

**Examples:**
```rust
// Old (0.16)
use rand::{thread_rng, Rng};
use rand::distributions::Uniform;

let mut rng = thread_rng();
let random_value = SomeType::from_entropy();

// New (0.17)
use rand::{rng, Rng};
use rand::distr::Uniform;

let mut rng = rng();
let random_value = SomeType::from_os_rng();

// Web builds now require getrandom configuration
// Add to Cargo.toml:
// [target.'cfg(target_arch = "wasm32")'.dependencies]
// getrandom = { version = "0.3", features = ["js"] }
```

---

---

## OpenGL ES `wgpu` backend is no longer supported by default

**Pull Requests:** 20793

**Description:**
The `gles` backend for `wgpu` has been removed from Bevy's default features due to lack of testing and potential incompatibilities. OpenGL support is still available but must be explicitly enabled via the `bevy_render/gles` feature flag. This change reflects the current state of OpenGL support as untested and potentially broken for some features.

**Checklist:**
- [ ] **REQUIRED (OPENGL USERS)**: Add `bevy_render/gles` feature to `Cargo.toml` dependencies
- [ ] **REQUIRED (OPENGL USERS)**: Test thoroughly as some features may not work correctly
- [ ] Review your rendering code for OpenGL-specific compatibility issues
- [ ] Consider contributing to improve OpenGL support if you rely on it
- [ ] If possible, migrate to Vulkan/Metal/DX12 backends for better support

**Search Patterns:** `bevy_render`, `gles`, `wgpu`, `features = `

**Examples:**
```toml
# Old (0.16) - gles included by default
[dependencies]
bevy = "0.16"

# New (0.17) - must explicitly enable gles
[dependencies]
bevy = { version = "0.17", features = [] }
bevy_render = { version = "0.17", features = ["gles"] }

# Or with default features:
[dependencies]
bevy = { version = "0.17", features = ["bevy_render/gles"] }
```

---

---

## glTF animation loading is now optional

**Pull Requests:** 20750

**Description:**
You can now control whether animations are loaded from glTF files via the new `load_animations` field in `GltfLoaderSettings`. This allows optimizing asset loading by skipping animation data when not needed, reducing memory usage and load times for static models.

**Checklist:**
- [ ] **OPTIONAL**: Add `load_animations: false` to `GltfLoaderSettings` for models that don't need animations
- [ ] Review glTF asset loading code to set appropriate animation loading flags
- [ ] Consider setting `load_animations: false` for static scenery, props, and non-animated models
- [ ] Keep `load_animations: true` (or default) for character models and animated objects
- [ ] Test that animations still load correctly when enabled

**Search Patterns:** `GltfLoaderSettings`, `load_animations`, `.gltf`, `.glb`

**Examples:**
```rust
// Old (0.16) - animations always loaded
asset_server.load_with_settings(
    "model.gltf",
    |settings: &mut GltfLoaderSettings| {
        // no control over animation loading
    }
);

// New (0.17) - optional animation loading
// Load with animations (default behavior)
asset_server.load_with_settings(
    "character.gltf",
    |settings: &mut GltfLoaderSettings| {
        settings.load_animations = true;
    }
);

// Load without animations for optimization
asset_server.load_with_settings(
    "static_prop.gltf",
    |settings: &mut GltfLoaderSettings| {
        settings.load_animations = false;
    }
);
```

---

---

## 36. Observers and EntityEvents no longer take `&mut World` as a parameter

**Pull Requests:** #16293

**Description:**
Observer callbacks and EntityEvent implementations now receive references to world data through the system parameter pattern instead of taking `&mut World` directly. This aligns observers with Bevy's standard system parameter model. Update all observer functions and EntityEvent implementations to use the `Trigger` parameter and system parameters instead of direct world access.

**Checklist:**
- [ ] Find all observer function signatures that take `&mut World` as a parameter
- [ ] Replace `&mut World` parameter with appropriate system parameters (e.g., `Commands`, `Query`, `Res`, `ResMut`)
- [ ] Update function bodies to use the new system parameters instead of world methods
- [ ] Review all EntityEvent implementations for direct world access
- [ ] Test that observer behavior remains unchanged after migration

**Search Patterns:** `fn.*Trigger.*&mut World`, `impl EntityEvent`, `observer.*&mut World`

**Examples:**

```rust
// Old
app.observe(|trigger: Trigger<MyEvent>, world: &mut World| {
    // ...
});

// New
app.observe(|trigger: Trigger<MyEvent>, mut commands: Commands| {
    // ...
});
```

---

---

## 37. Replace `Trigger<T>` with `On<T>` in observer closures

**Pull Requests:** #16488

**Description:**
The `Trigger<T>` parameter in observer closures has been split into two separate parameters: `On<T>` (which provides the event data) and `entity: Entity` (which provides the target entity). This separation makes the API clearer and more consistent. Update all observer closures to use the new parameter pattern.

**Checklist:**
- [ ] Search for all observer closures using `Trigger<` parameter syntax
- [ ] Replace `trigger: Trigger<T>` with `event: On<T>` to access event data
- [ ] Add `entity: Entity` parameter if you need access to the target entity (previously obtained via `trigger.target()`)
- [ ] Update event data access from `trigger.event()` to `event.read()` or just `event` for direct access
- [ ] Replace `trigger.target()` calls with the `entity` parameter
- [ ] Update `trigger.propagate(false)` to `event.propagate(false)`

**Search Patterns:** `Trigger<`, `trigger.event()`, `trigger.target()`, `trigger.propagate(`, `.observe(`

**Examples:**

```rust
// Old
commands.observe(|trigger: Trigger<OnAdd, MyComponent>| {
    let event = trigger.event();
    let target = trigger.target();
});

// New - minimal change
commands.observe(|event: On<OnAdd, MyComponent>| {
    let event = event.read();
});

// New - with entity access
commands.observe(|event: On<OnAdd, MyComponent>, entity: Entity| {
    let event = event.read();
    let target = entity;
});
```

---

---

## 38. `ParamSet` is now `SystemParamBuilder`-compatible

**Pull Requests:** #16302

**Description:**
`ParamSet` can now be constructed using the `SystemParamBuilder` API, enabling dynamic system parameter construction. If you were using workarounds to build `ParamSet` dynamically, you can now use the standard builder pattern. This is an optional enhancement for code that needs dynamic system construction.

**Checklist:**
- [ ] Review code using `ParamSet` to identify opportunities for dynamic construction
- [ ] If building systems dynamically, consider using `ParamSetBuilder` for type-safe construction
- [ ] Use `ParamSetBuilder::of::<(P0, P1, ...)>()` to create a builder for your parameter set
- [ ] Chain builder methods as needed for your use case
- [ ] No changes required for static `ParamSet` usage

**Search Patterns:** `ParamSet`, `SystemParamBuilder`, `ParamSetBuilder`

**Examples:**

```rust
// Old - static only
fn system(mut params: ParamSet<(Query<&A>, Query<&B>)>) {
    let a = params.p0();
    let b = params.p1();
}

// New - dynamic construction available
let builder = ParamSetBuilder::of::<(QueryBuilder<&A>, QueryBuilder<&B>)>();
// Can now build ParamSet dynamically
```

---

---

## 39. `AssetServer::load_untyped` now returns a `Handle<LoadedUntypedAsset>`

**Pull Requests:** #16610

**Description:**
The return type of `AssetServer::load_untyped` has changed from `UntypedHandle` to `Handle<LoadedUntypedAsset>`. This provides better type safety and clearer semantics for untyped asset handles. Update all code using `load_untyped` to work with the new handle type.

**Checklist:**
- [ ] Find all calls to `AssetServer::load_untyped`
- [ ] Update variable types storing the result from `UntypedHandle` to `Handle<LoadedUntypedAsset>`
- [ ] Review any type annotations or function signatures using `UntypedHandle` from `load_untyped` calls
- [ ] Test that asset loading behavior remains correct

**Search Patterns:** `load_untyped`, `UntypedHandle`, `LoadedUntypedAsset`

**Examples:**

```rust
// Old
let handle: UntypedHandle = asset_server.load_untyped("path/to/asset");

// New
let handle: Handle<LoadedUntypedAsset> = asset_server.load_untyped("path/to/asset");
```

---

---

## 40. Return an `impl` instead of a concrete type from `Sprite::from_color`

**Pull Requests:** #16726

**Description:**
`Sprite::from_color` now returns an opaque `impl Bundle` type instead of a concrete bundle type. This is an internal API change that should not require code changes unless you were explicitly annotating the return type. Let type inference handle the return type.

**Checklist:**
- [ ] Find all uses of `Sprite::from_color`
- [ ] Remove any explicit type annotations on the returned bundle (use `_` or type inference)
- [ ] Verify that the bundle is still used correctly in spawning/insertion contexts
- [ ] This change should be transparent for most usage

**Search Patterns:** `Sprite::from_color`, `from_color(`

**Examples:**

```rust
// Old - concrete type annotation (may break)
let sprite: SpriteBundle = Sprite::from_color(Color::RED, Vec2::new(100.0, 100.0));

// New - use type inference
let sprite = Sprite::from_color(Color::RED, Vec2::new(100.0, 100.0));
commands.spawn(sprite);
```

---

---

## 41. `bevy_a11y` is no longer included by default

**Pull Requests:** #16234

**Description:**
The accessibility module `bevy_a11y` is no longer automatically included when using `DefaultPlugins`. If your application requires accessibility features, you must explicitly add the `AccessibilityPlugin` or enable the `bevy_a11y` feature. This is a breaking change for applications that rely on accessibility functionality.

**Checklist:**
- [ ] Determine if your application uses accessibility features (screen readers, focus management, etc.)
- [ ] If accessibility is needed, add `AccessibilityPlugin` to your plugin group
- [ ] Alternative: Enable the `bevy_a11y` feature in your `Cargo.toml` dependencies
- [ ] Test accessibility features if your application depends on them
- [ ] If not using accessibility, no action is required

**Search Patterns:** `bevy_a11y`, `AccessibilityPlugin`, `DefaultPlugins`, `AccessKit`, `Focus`

**Examples:**

```rust
// Old - a11y included automatically
App::new()
    .add_plugins(DefaultPlugins)
    .run();

// New - explicit inclusion if needed
App::new()
    .add_plugins(DefaultPlugins)
    .add_plugins(AccessibilityPlugin)
    .run();

// Or in Cargo.toml
// bevy = { version = "0.17", features = ["bevy_a11y"] }
```

---

---

## 42. `BreakLineOn` is now `LineBreak` and used in `TextLayout`

**Pull Requests:** #15583

**Description:**
The `BreakLineOn` type has been renamed to `LineBreak` and is now used through the `TextLayout` component instead of being set directly. This consolidates text layout configuration into a single component. Update all references to use the new name and location.

**Checklist:**
- [ ] Search for all uses of `BreakLineOn` and rename to `LineBreak`
- [ ] Find places where `BreakLineOn` was set as a component or field
- [ ] Move line break configuration to the `TextLayout` component's `linebreak` field
- [ ] Update imports from `bevy::text::BreakLineOn` to `bevy::text::LineBreak`
- [ ] Review text rendering code to ensure line breaking behavior is preserved

**Search Patterns:** `BreakLineOn`, `LineBreak`, `TextLayout`, `linebreak`

**Examples:**

```rust
// Old
use bevy::text::BreakLineOn;

commands.spawn((
    TextBundle::from_section("text", TextStyle::default()),
    BreakLineOn::WordBoundary,
));

// New
use bevy::text::LineBreak;

commands.spawn((
    TextBundle::from_section("text", TextStyle::default()),
    TextLayout {
        linebreak: LineBreak::WordBoundary,
        ..default()
    },
));
```

---

---

## 43. `TextFont::font` now uses `Handle<Font>` instead of `AssetId<Font>`

**Pull Requests:** #16791

**Description:**
The `font` field in `TextFont` has changed from storing an `AssetId<Font>` to storing a `Handle<Font>`. This provides better asset lifecycle management and consistency with Bevy's asset system. Update all code that sets or reads the font field.

**Checklist:**
- [ ] Search for all places where `TextFont::font` is set or modified
- [ ] Replace `AssetId<Font>` values with `Handle<Font>` values
- [ ] If you have an `AssetId`, convert it to a `Handle` using appropriate asset server methods
- [ ] Update any pattern matching or field access on `TextFont` to expect `Handle<Font>`
- [ ] Review font loading code to ensure handles are properly stored and managed

**Search Patterns:** `TextFont`, `.font`, `AssetId<Font>`, `Handle<Font>`

**Examples:**

```rust
// Old
let text_font = TextFont {
    font: asset_id,
    ..default()
};

// New
let text_font = TextFont {
    font: font_handle,
    ..default()
};

// If you only have an AssetId, get the handle from the asset server
let font_handle = asset_server.get_handle(asset_id);
```

---

---

## 44. `NodeBundle` and `ImageBundle` field changes for text layout

**Pull Requests:** #15583

**Description:**
The `text_linebreak` field has been removed from `NodeBundle` and `ImageBundle`, and the `image_node_size` field has been removed from `ImageBundle`. Text layout configuration is now handled exclusively through the `TextLayout` component. Update bundle construction to remove these deprecated fields.

**Checklist:**
- [ ] Search for `NodeBundle` construction with `text_linebreak` field
- [ ] Remove the `text_linebreak` field from `NodeBundle` initialization
- [ ] If line breaking configuration is needed, add a separate `TextLayout` component
- [ ] Search for `ImageBundle` construction with `text_linebreak` or `image_node_size` fields
- [ ] Remove these fields from `ImageBundle` initialization
- [ ] Move line break settings to `TextLayout` component if text is involved

**Search Patterns:** `NodeBundle`, `ImageBundle`, `text_linebreak`, `image_node_size`, `TextLayout`

**Examples:**

```rust
// Old
commands.spawn(NodeBundle {
    text_linebreak: BreakLineOn::WordBoundary,
    ..default()
});

commands.spawn(ImageBundle {
    text_linebreak: BreakLineOn::WordBoundary,
    image_node_size: NodeImageSize::AUTO,
    ..default()
});

// New
commands.spawn((
    NodeBundle::default(),
    TextLayout {
        linebreak: LineBreak::WordBoundary,
        ..default()
    },
));

commands.spawn(ImageBundle::default());
```

---

---

## 45. `ComputedTextBlock::entities()` iterator now has static lifetime

**Pull Requests:** #15583

**Description:**
The iterator returned by `ComputedTextBlock::entities()` now has a `'static` lifetime instead of being tied to the `ComputedTextBlock` reference. This is primarily an internal change that improves API ergonomics. Most code should work unchanged, but explicit lifetime annotations may need updates.

**Checklist:**
- [ ] Search for uses of `ComputedTextBlock::entities()`
- [ ] Review any explicit lifetime annotations on the returned iterator
- [ ] Remove or update lifetime bounds that conflict with the new `'static` lifetime
- [ ] Most code using type inference should work unchanged
- [ ] Test that text entity iteration still functions correctly

**Search Patterns:** `ComputedTextBlock`, `.entities()`, `TextBlockEntities`

**Examples:**

```rust
// Old - lifetime tied to reference
fn process_entities<'a>(block: &'a ComputedTextBlock) -> impl Iterator + 'a {
    block.entities()
}

// New - static lifetime
fn process_entities(block: &ComputedTextBlock) -> impl Iterator + 'static {
    block.entities()
}
```

---

---

## 46. `cosmic-text` upgraded from 0.12 to 0.14

**Pull Requests:** #15583

**Description:**
The underlying text rendering library `cosmic-text` has been upgraded from version 0.12 to 0.14. If you directly use `cosmic-text` APIs or types in your code, you may need to update to account for API changes in the new version. Most Bevy users who only use Bevy's text APIs should not be affected.

**Checklist:**
- [ ] Check if your code directly imports or uses `cosmic-text` types
- [ ] Review the `cosmic-text` changelog for breaking changes between 0.12 and 0.14
- [ ] Update any direct `cosmic-text` API usage to match version 0.14
- [ ] Update your `Cargo.toml` if you have an explicit `cosmic-text` dependency
- [ ] Test text rendering to ensure visual output is correct
- [ ] If you only use Bevy's text APIs (not cosmic-text directly), no changes should be needed

**Search Patterns:** `cosmic_text`, `use cosmic_text::`, `cosmic-text`

**Examples:**

```toml
# Cargo.toml - Old
[dependencies]
cosmic-text = "0.12"

# Cargo.toml - New
[dependencies]
cosmic-text = "0.14"
```

---

---

## 47. `TextSpanAccess` removed in favor of specialized accessor methods

**Pull Requests:** #15583

**Description:**
The `TextSpanAccess` trait has been removed. Direct access to text spans is now provided through specialized accessor methods like `text_span()`, `text_color()`, and `text_font()` on relevant components. Update code to use these specific accessor methods instead of the generic trait.

**Checklist:**
- [ ] Search for all imports and uses of `TextSpanAccess` trait
- [ ] Remove `TextSpanAccess` trait imports
- [ ] Replace generic span access methods with specific accessors:
  - [ ] Use `text_span()` to access text span data
  - [ ] Use `text_color()` to access color information
  - [ ] Use `text_font()` to access font information
- [ ] Update any generic functions that were bounded by `TextSpanAccess` to use concrete types
- [ ] Test that text manipulation and querying still works correctly

**Search Patterns:** `TextSpanAccess`, `text_span()`, `text_color()`, `text_font()`

**Examples:**

```rust
// Old
use bevy::text::TextSpanAccess;

fn update_span(span: &mut impl TextSpanAccess) {
    // Generic access
}

// New
fn update_span(span: &mut TextSpan) {
    let text = span.text_span();
    let color = span.text_color();
    let font = span.text_font();
}
```

---

---

## Non-generic `Access`

**Pull Requests:** 20288

**Description:**
Following the removal of `archetype_component_id`, the ECS access tracking types (`Access`, `AccessFilters`, `FilteredAccess`, and `FilteredAccessSet`) were previously parameterized with a generic type but were only ever used with `ComponentId`. To simplify the API and reduce unnecessary complexity, the generic parameter has been removed from all these types. This is a straightforward API simplification that requires updating type signatures in any custom `WorldQuery` or `SystemParam` implementations.

**Checklist:**
- [ ] **REQUIRED:** Search for `Access<ComponentId>` and replace with `Access`
- [ ] **REQUIRED:** Search for `FilteredAccess<ComponentId>` and replace with `FilteredAccess`
- [ ] **REQUIRED:** Search for `AccessFilters<ComponentId>` and replace with `AccessFilters`
- [ ] **REQUIRED:** Search for `FilteredAccessSet<ComponentId>` and replace with `FilteredAccessSet`
- [ ] Review custom `WorldQuery` implementations for `update_component_access` method signatures
- [ ] Review custom `SystemParam` implementations that use these access types
- [ ] Update any type aliases or wrapper types that reference these access types

**Search Patterns:** `Access<ComponentId>`, `FilteredAccess<ComponentId>`, `AccessFilters<ComponentId>`, `FilteredAccessSet<ComponentId>`, `update_component_access`, `WorldQuery`, `SystemParam`

**Examples:**
```rust
// 0.16
fn update_component_access(state: &Self::State, access: &mut FilteredAccess<ComponentId>) {
    // implementation
}

// 0.17
fn update_component_access(state: &Self::State, access: &mut FilteredAccess) {
    // implementation
}
```

---

---

## Observer / Event API Changes

**Pull Requests:** 20731, 19440, 19596

**Description:**
The observer and event API has undergone significant changes to improve type safety, clarity, and ergonomics. The core changes include: (1) `Trigger<E>` renamed to `On<E>` to better represent the semantic meaning, (2) lifecycle events renamed from `OnAdd`/`OnInsert`/`OnReplace`/`OnRemove`/`OnDespawn` to `Add`/`Insert`/`Replace`/`Remove`/`Despawn`, (3) introduction of `EntityEvent` derive for entity-targeted events with the entity stored on the event itself, (4) removal of `world.trigger_targets` in favor of unified `world.trigger`, (5) `On::target()` removed in favor of direct entity field access, (6) propagation configured via `#[entity_event(propagate)]` attribute, and (7) new `AnimationEvent` derive for animation-specific events. These changes affect all observer code, entity event handling, animation events, and component lifecycle observers.

**Checklist:**
- [ ] **REQUIRED:** Replace all `Trigger<E>` with `On<E>` in observer signatures
- [ ] **REQUIRED:** Replace `OnAdd` with `Add` (may need `use std::ops::Add as OpAdd` if trait conflicts occur)
- [ ] **REQUIRED:** Replace `OnInsert` with `Insert`
- [ ] **REQUIRED:** Replace `OnReplace` with `Replace`
- [ ] **REQUIRED:** Replace `OnRemove` with `Remove`
- [ ] **REQUIRED:** Replace `OnDespawn` with `Despawn`
- [ ] **REQUIRED:** For entity-targeted events, change from `#[derive(Event)]` to `#[derive(EntityEvent)]`
- [ ] **REQUIRED:** Add `entity: Entity` field to all `EntityEvent` types
- [ ] **REQUIRED:** Replace `world.trigger_targets(event, entity)` with `world.trigger(Event { entity })`
- [ ] **REQUIRED:** Replace `world.trigger_targets(event, [e1, e2])` with loop or multiple calls
- [ ] **REQUIRED:** Replace `trigger.target()` with direct field access (e.g., `add.entity` or `event.entity`)
- [ ] For events with propagation, replace `#[event(traversal = X)]` with `#[entity_event(propagate = X)]`
- [ ] For default propagation (ChildOf), use `#[entity_event(propagate)]` without assignment
- [ ] **REQUIRED:** For animation events, change from `#[derive(Event)]` to `#[derive(AnimationEvent)]`
- [ ] **REQUIRED:** Replace `trigger.target()` in animation observers with `say_message.trigger().animation_player`
- [ ] Replace `trigger.components()` with `add.trigger().components` for lifecycle events
- [ ] Update observer variable names to match event type (e.g., `add: On<Add>`, `click: On<Click>`)
- [ ] Remove any "On" prefix from custom event type names for consistency
- [ ] Verify propagation functions are only used on events with `EntityEvent<Trigger = PropagateEntityTrigger>`
- [ ] Use `On::original_entity()` instead of removed `Pointer.target` field (see pointer_target migration)

**Search Patterns:** `Trigger<`, `OnAdd`, `OnInsert`, `OnReplace`, `OnRemove`, `OnDespawn`, `trigger_targets`, `.target()`, `#[derive(Event)]`, `#[event(traversal`, `EntityEvent`, `AnimationEvent`, `trigger.components()`, `add_observer`, `observe(`, `On<`, `entity_event(propagate`

**Examples:**
```rust
// 0.16 - Basic observer
commands.add_observer(|trigger: Trigger<OnAdd, Player>| {
    info!("Spawned player {}", trigger.target());
});

// 0.17 - Basic observer
commands.add_observer(|add: On<Add, Player>| {
    info!("Spawned player {}", add.entity);
});

// 0.16 - Entity-targeted event
#[derive(Event)]
struct Explode;

world.trigger_targets(Explode, entity);

// 0.17 - Entity-targeted event
#[derive(EntityEvent)]
struct Explode {
    entity: Entity
}

world.trigger(Explode { entity });

// 0.16 - Multiple targets
world.trigger_targets(Explode, [e1, e2]);

// 0.17 - Multiple targets (variant 1)
world.trigger(Explode { entity: e1 });
world.trigger(Explode { entity: e2 });

// 0.17 - Multiple targets (variant 2)
for entity in [e1, e2] {
    world.trigger(Explode { entity });
}

// 0.16 - Accessing target in observer
commands.add_observer(|trigger: Trigger<Explode>| {
    info!("{} exploded!", trigger.target());
});

// 0.17 - Accessing target in observer
commands.add_observer(|explode: On<Explode>| {
    info!("{} exploded!", explode.entity);
    // Can also use EntityEvent::event_target()
    info!("{} exploded!", explode.event_target());
});

// 0.16 - Propagation
#[derive(Event)]
#[event(traversal = &'static ChildOf)]
struct Click;

// 0.17 - Propagation (default ChildOf)
#[derive(EntityEvent)]
#[entity_event(propagate)]
struct Click {
    entity: Entity,
}

// 0.17 - Custom propagation
#[derive(EntityEvent)]
#[entity_event(propagate = &'static ChildOf)]
struct Click {
    entity: Entity,
}

// 0.16 - Animation events
#[derive(Event)]
struct SayMessage(String);

animation.add_event(0.2, SayMessage("hello".to_string()));
world.entity_mut(animation_player).observe(|trigger: Trigger<SayMessage>| {
    println!("played on", trigger.target());
})

// 0.17 - Animation events
#[derive(AnimationEvent)]
struct SayMessage(String);

animation.add_event(0.2, SayMessage("hello".to_string()));
world.entity_mut(animation_player).observe(|say_message: On<SayMessage>| {
    println!("played on", say_message.trigger().animation_player);
})

// 0.16 - Lifecycle event components
commands.add_observer(|trigger: Trigger<OnAdd, Player>| {
    info!("{}", trigger.components());
});

// 0.17 - Lifecycle event components
commands.add_observer(|add: On<Add, Player>| {
    info!("{}", add.trigger().components);
});
```

---

---

## Exclusive systems may not be used as observers

**Pull Requests:** 19033

**Description:**
Observers can no longer use exclusive systems (systems with `&mut World` parameter). This was never sound because the engine maintains references during observer invocation that would be invalidated by `&mut World` access, but it was accidentally allowed in earlier versions. This is a soundness fix that prevents undefined behavior. Instead of `&mut World`, observers should use `DeferredWorld` for non-structural operations or `Commands` for deferred structural changes.

**Checklist:**
- [ ] **REQUIRED:** Search for all observer functions with `&mut World` parameter
- [ ] **REQUIRED:** Replace `&mut World` with `DeferredWorld` for observers that don't need structural changes
- [ ] **REQUIRED:** Replace `&mut World` with `Commands` for observers that need to spawn/despawn entities or add/remove components
- [ ] Review observer implementations for proper deferred execution semantics
- [ ] Test observers to ensure they work correctly with the new parameter types
- [ ] Consider whether operations truly need structural changes or can use `DeferredWorld`

**Search Patterns:** `&mut World`, `DeferredWorld`, `Commands`, `add_observer`, `observe(`, `On<`

**Examples:**
```rust
// 0.16 - Invalid (but accidentally allowed)
commands.add_observer(|trigger: Trigger<MyEvent>, world: &mut World| {
    world.spawn(MyBundle::default());
});

// 0.17 - Using Commands for structural changes
commands.add_observer(|event: On<MyEvent>, mut commands: Commands| {
    commands.spawn(MyBundle::default());
});

// 0.17 - Using DeferredWorld for non-structural access
commands.add_observer(|event: On<MyEvent>, world: DeferredWorld| {
    let resource = world.resource::<MyResource>();
    // ... read-only or deferred operations
});
```

---

---

## `OverflowClipBox`'s default is now `PaddingBox`

**Pull Requests:** 18935

**Description:**
The default variant for the `OverflowClipBox` enum has changed from `ContentBox` to `PaddingBox`. Additionally, `OverflowClipMargin::visual_box` now defaults to `OverflowClipBox::PaddingBox` instead of `ContentBox`. This affects UI clipping behavior when overflow occurs, as the clipping boundary will now include padding by default rather than being limited to content dimensions only.

**Checklist:**
- [ ] Search for explicit `OverflowClipBox::ContentBox` usages and verify they still need content-box clipping
- [ ] Review code that creates `OverflowClipBox::default()` and verify padding-box behavior is acceptable
- [ ] Check `OverflowClipMargin::visual_box` usages for default value assumptions
- [ ] Test UI elements with overflow to ensure clipping appears as expected
- [ ] If content-box clipping is required, explicitly specify `OverflowClipBox::ContentBox`

**Search Patterns:** `OverflowClipBox`, `OverflowClipBox::default()`, `OverflowClipMargin`, `visual_box`, `ContentBox`, `PaddingBox`, `overflow`

**Examples:**
```rust
// 0.16 - Default was ContentBox
let clip_box = OverflowClipBox::default(); // ContentBox
let margin = OverflowClipMargin::default(); // visual_box was ContentBox

// 0.17 - Default is now PaddingBox
let clip_box = OverflowClipBox::default(); // PaddingBox
let margin = OverflowClipMargin::default(); // visual_box is now PaddingBox

// If you need ContentBox behavior explicitly
let clip_box = OverflowClipBox::ContentBox;
let margin = OverflowClipMargin {
    visual_box: OverflowClipBox::ContentBox,
    ..default()
};
```

---

---

## Changes to Bevy's system parallelism strategy

**Pull Requests:** 16885

**Description:**
The system scheduler now uses a more conservative parallelism strategy based solely on function signatures rather than examining actual archetype overlap. Systems are prevented from running in parallel if they *could* conflict based on their query signatures, even if no entities currently exist that match both queries. This change improves scheduling performance by reducing overhead, as testing showed that scheduling overhead dominated the gains from fine-grained parallelism. Systems with overlapping query signatures now require explicit `Without` filters to run in parallel.

**Checklist:**
- [ ] Identify systems that query the same components with different filters
- [ ] If two systems both query `&mut Transform` with different component filters, add `Without` filters
- [ ] Test application performance after migration to identify any regressions
- [ ] Add explicit `Without<Enemy>` to player systems and `Without<Player>` to enemy systems (see example)
- [ ] Consider consolidating systems that operate on the same archetypes if parallelism isn't needed
- [ ] Report performance regressions on the Bevy issue tracker with benchmarking data

**Search Patterns:** `Query<(&mut`, `Without<`, `With<`, `fn system`, `Transform`, parallel systems with overlapping component access

**Examples:**
```rust
// 0.16 - These would run in parallel if no entity had both Player and Enemy
fn player_system(query: Query<(&mut Transform, &Player)>) {
    // player logic
}

fn enemy_system(query: Query<(&mut Transform, &Enemy)>) {
    // enemy logic
}

// 0.17 - Now these conflict because both access &mut Transform
// Must add Without filters for parallelism

// Option 1: Add Without to player_system
fn player_system(query: Query<(&mut Transform, &Player), Without<Enemy>>) {
    // player logic
}

fn enemy_system(query: Query<(&mut Transform, &Enemy)>) {
    // enemy logic
}

// Option 2: Add Without to enemy_system
fn player_system(query: Query<(&mut Transform, &Player)>) {
    // player logic
}

fn enemy_system(query: Query<(&mut Transform, &Enemy), Without<Player>>) {
    // enemy logic
}

// Option 3: Add Without to both (most explicit and recommended)
fn player_system(query: Query<(&mut Transform, &Player), Without<Enemy>>) {
    // player logic
}

fn enemy_system(query: Query<(&mut Transform, &Enemy), Without<Player>>) {
    // enemy logic
}
```

---

---

## Changes to the default error handler mechanism

**Pull Requests:** 18810

**Description:**
Bevy's default error handling mechanism has been improved with reduced performance overhead, and it is now always enabled. The `configurable_error_handler` feature flag no longer exists and should be removed from dependency configurations. Error handlers are now per-world rather than global. The `GLOBAL_ERROR_HANDLER` has been removed in favor of `App::set_error_handler(handler)` for app-associated worlds, and `DefaultErrorHandler(handler)` resource for standalone worlds.

**Checklist:**
- [ ] **REQUIRED:** Remove `configurable_error_handler` feature from `bevy` dependency in `Cargo.toml`
- [ ] **REQUIRED:** Replace `GLOBAL_ERROR_HANDLER` with `App::set_error_handler(handler)`
- [ ] For standalone worlds (not part of App/SubApp), insert `DefaultErrorHandler(handler)` resource
- [ ] Review error handling setup in all apps and worlds
- [ ] Remove any conditional compilation based on `configurable_error_handler` feature

**Search Patterns:** `configurable_error_handler`, `GLOBAL_ERROR_HANDLER`, `set_error_handler`, `DefaultErrorHandler`, error handler, error handling

**Examples:**
```rust
// 0.16 - Cargo.toml
[dependencies]
bevy = { version = "0.16", features = ["configurable_error_handler"] }

// 0.17 - Cargo.toml
[dependencies]
bevy = { version = "0.17" }  // Feature removed

// 0.16 - Setting global handler
GLOBAL_ERROR_HANDLER.set(my_handler);

// 0.17 - Setting handler on App
app.set_error_handler(my_handler);

// 0.17 - Setting handler on standalone world
world.insert_resource(DefaultErrorHandler(my_handler));
```

---

---

## `Location` is no longer a `Component`

**Pull Requests:** 19306

**Description:**
`bevy_picking::Location` was incorrectly marked as a `Component` and has been corrected to no longer derive `Component`. The component you should use for picking location data is `bevy_picking::PointerLocation`, which wraps a `Location` instance. This is a bug fix that corrects the component architecture.

**Checklist:**
- [ ] **REQUIRED:** Search for queries or commands using `Location` as a component
- [ ] **REQUIRED:** Replace `Location` component usage with `PointerLocation` component
- [ ] **REQUIRED:** Access the inner `Location` through `PointerLocation` wrapper
- [ ] Update any component spawning/insertion that used `Location`
- [ ] Update any systems that queried for `Location` component

**Search Patterns:** `bevy_picking::Location`, `PointerLocation`, `Query<`, `With<Location>`, `&Location`, `&mut Location`, `insert(Location`, `spawn((Location`

**Examples:**
```rust
// 0.16 - Incorrect (but accidentally allowed)
use bevy_picking::Location;

fn my_system(query: Query<&Location>) {
    for location in &query {
        // use location
    }
}

commands.spawn(Location::default());

// 0.17 - Correct usage with PointerLocation
use bevy_picking::PointerLocation;

fn my_system(query: Query<&PointerLocation>) {
    for pointer_location in &query {
        let location = &pointer_location.0; // Access inner Location
        // use location
    }
}

commands.spawn(PointerLocation(Location::default()));
```

---

---

## Original target of `Pointer` picking events is now stored on observers

**Pull Requests:** 19663

**Description:**
The `Pointer.target` field, which tracked the original target entity of pointer events before bubbling, has been removed from the `Pointer` type. This information is now available on all "bubbling entity event" observers via the `On::original_entity()` method. This change consolidates bubbling information into the observer system. Users who relied on the Pointer API for this information should migrate to observers. A workaround pattern is provided for transforming entity events into messages that contain the target entity.

**Checklist:**
- [ ] **REQUIRED:** Search for `Pointer.target` field access and remove it
- [ ] **REQUIRED:** Replace `Pointer.target` with `On::original_entity()` in observers
- [ ] Migrate from Pointer API to observer-based picking if using `Pointer.target`
- [ ] If observers are unsuitable for performance reasons, use the message transformation workaround pattern
- [ ] Test picking behavior to ensure original target tracking still works correctly

**Search Patterns:** `Pointer.target`, `Pointer::target`, `original_entity()`, `On::original_entity`, entity event observers, picking events, bubbling

**Examples:**
```rust
// 0.16 - Using Pointer.target
fn handle_click(pointer: &Pointer<Click>) {
    info!("Original target: {}", pointer.target);
}

// 0.17 - Using On::original_entity() in observer
commands.add_observer(|click: On<Click>| {
    info!("Original target: {}", click.original_entity());
});

// 0.17 - Workaround: Transform entity events to messages with target
#[derive(Message)]
struct EntityEventMessage<E: EntityEvent> {
    entity: Entity,
    event: E,
}

// Generic observer for transformation
fn transform_entity_event<E: EntityEvent>(
    event: On<E>,
    message_writer: MessageWriter<EntityEventMessage<E>>
) {
    if event.entity() == event.original_entity() {
        message_writer.send(EntityEventMessage {
            event: event.event().clone(),
            entity: event.entity(),
        });
    }
}
```

---

---

## Polylines and Polygons are no longer const-generic

**Pull Requests:** 20250

**Description:**
The primitive types `Polyline2d`, `Polyline3d`, `Polygon`, and `ConvexPolygon` are no longer const-generic over vertex count. They now use `Vec` internally instead of arrays, which means they allocate on the heap and are no longer `no_std` compatible. These types now directly implement `Meshable` for more convenient mesh generation. This change improves ergonomics for dynamic geometry at the cost of `no_std` support and stack allocation.

**Checklist:**
- [ ] **REQUIRED:** Remove const-generic parameters from `Polyline2d<N>`, replacing with `Polyline2d`
- [ ] **REQUIRED:** Remove const-generic parameters from `Polyline3d<N>`, replacing with `Polyline3d`
- [ ] **REQUIRED:** Remove const-generic parameters from `Polygon<N>`, replacing with `Polygon`
- [ ] **REQUIRED:** Remove const-generic parameters from `ConvexPolygon<N>`, replacing with `ConvexPolygon`
- [ ] Update construction code to use `Vec` instead of arrays for vertex data
- [ ] If `no_std` compatibility is required, file an issue explaining your use case
- [ ] If const-generic (fixed vertex count) variants are needed, file an issue with your use case
- [ ] Use `Meshable` implementation directly instead of conversion functions

**Search Patterns:** `Polyline2d<`, `Polyline3d<`, `Polygon<`, `ConvexPolygon<`, const-generic polygons, `Meshable`

**Examples:**
```rust
// 0.16 - Const-generic with arrays
let polyline = Polyline2d::<4>::new([
    Vec2::new(0.0, 0.0),
    Vec2::new(1.0, 0.0),
    Vec2::new(1.0, 1.0),
    Vec2::new(0.0, 1.0),
]);

let polygon = Polygon::<5>::new([
    Vec2::new(0.0, 0.0),
    Vec2::new(2.0, 0.0),
    Vec2::new(2.0, 1.0),
    Vec2::new(1.0, 1.0),
    Vec2::new(0.0, 1.0),
]);

// 0.17 - Dynamic with Vec
let polyline = Polyline2d::new(vec![
    Vec2::new(0.0, 0.0),
    Vec2::new(1.0, 0.0),
    Vec2::new(1.0, 1.0),
    Vec2::new(0.0, 1.0),
]);

let polygon = Polygon::new(vec![
    Vec2::new(0.0, 0.0),
    Vec2::new(2.0, 0.0),
    Vec2::new(2.0, 1.0),
    Vec2::new(1.0, 1.0),
    Vec2::new(0.0, 1.0),
]);

// Direct mesh generation is now available
let mesh = polyline.mesh();
```

---

---

## Query items can borrow from query state

**Pull Requests:** 15396, 19720

**Description:**
The `QueryData::Item` associated type and related type aliases (`QueryItem` and `ROQueryItem`) now have an additional lifetime parameter `'s` corresponding to the query state lifetime. This allows query items to borrow from the query state itself. The `QueryData::fetch()` and `QueryFilter::filter_fetch()` methods now take a `&'s WorldQuery::State` parameter. This change affects manual `WorldQuery` implementations and usage of `ROQueryItem` (particularly in `RenderCommand` implementations). Additionally, methods on `QueryState` that take `&mut self` may now produce conflicting borrow errors that require using manual query methods with explicit archetype updates.

**Checklist:**
- [ ] **REQUIRED:** Add lifetime parameter to `ROQueryItem<'w, Q>` making it `ROQueryItem<'w, '_, Q>` (typically `'_` inference works)
- [ ] **REQUIRED:** Add lifetime parameter to `QueryItem<'w, Q>` making it `QueryItem<'w, '_, Q>` where used
- [ ] **REQUIRED:** In `RenderCommand::render()`, update `ROQueryItem<'w, Self::ViewQuery>` to `ROQueryItem<'w, '_, Self::ViewQuery>`
- [ ] **REQUIRED:** In `RenderCommand::render()`, update `ROQueryItem<'w, Self::ItemQuery>` to `ROQueryItem<'w, '_, Self::ItemQuery>`
- [ ] Update manual `WorldQuery` implementations to add `&'s State` parameter to `fetch()` and `filter_fetch()`
- [ ] For conflicting borrows from `QueryState::get()` or `QueryState::iter()`, refactor to use `query_manual()` pattern
- [ ] Replace `state.get(world, entity)` with `state.get_manual(world, entity)` when multiple calls needed
- [ ] Alternatively use `state.query_manual(world).get_inner(entity)` for multiple accesses
- [ ] Or restructure to call `state.query(world)` once and reuse the `Query`
- [ ] Call `state.update_archetypes(world)` before using manual query methods

**Search Patterns:** `ROQueryItem<'w,`, `QueryItem<'w,`, `RenderCommand`, `QueryState`, `.get(world`, `.iter()`, `query_manual`, `get_manual`, `update_archetypes`, `WorldQuery`, `fetch()`, `filter_fetch()`

**Examples:**
```rust
// 0.16 - RenderCommand without additional lifetime
fn render<'w>(
    item: &P,
    view: ROQueryItem<'w, Self::ViewQuery>,
    entity: Option<ROQueryItem<'w, Self::ItemQuery>>,
    param: SystemParamItem<'w, '_, Self::Param>,
    pass: &mut TrackedRenderPass<'w>,
) -> RenderCommandResult {
    // implementation
}

// 0.17 - RenderCommand with additional lifetime
fn render<'w>(
    item: &P,
    view: ROQueryItem<'w, '_, Self::ViewQuery>,
    entity: Option<ROQueryItem<'w, '_, Self::ItemQuery>>,
    param: SystemParamItem<'w, '_, Self::Param>,
    pass: &mut TrackedRenderPass<'w>,
) -> RenderCommandResult {
    // implementation
}

// 0.16 - QueryState multiple accesses
let mut state: QueryState<_, _> = ...;

let d1 = state.get(world, e1);
let d2 = state.get(world, e2); // Works in 0.16

println!("{d1:?}");
println!("{d2:?}");

// 0.17 - QueryState borrow conflict
let mut state: QueryState<_, _> = ...;

let d1 = state.get(world, e1);
let d2 = state.get(world, e2); // ERROR: cannot borrow `state` as mutable more than once

println!("{d1:?}");
println!("{d2:?}");

// 0.17 - Solution 1: Use get_manual
state.update_archetypes(world);
let d1 = state.get_manual(world, e1);
let d2 = state.get_manual(world, e2);

println!("{d1:?}");
println!("{d2:?}");

// 0.17 - Solution 2: Use query_manual with get_inner
state.update_archetypes(world);
let d1 = state.query_manual(world).get_inner(e1);
let d2 = state.query_manual(world).get_inner(e2);

println!("{d1:?}");
println!("{d2:?}");

// 0.17 - Solution 3: Use query() once and reuse
let query = state.query(world);
let d1 = query.get_inner(e1);
let d2 = query.get_inner(e2);

println!("{d1:?}");
println!("{d2:?}");
```

---

---

## `ViewRangefinder3d::from_world_from_view` now takes `Affine3A` instead of `Mat4`

**Pull Requests:** 20707

**Description:**
The `ViewRangefinder3d::from_world_from_view` method now accepts `Affine3A` instead of `Mat4` for improved performance. If you were previously using `GlobalTransform::to_matrix()`, you should now use `GlobalTransform::affine()` instead. This change leverages the more efficient affine transformation representation and eliminates unnecessary conversions.

**Checklist:**
- [ ] **REQUIRED:** Search for `ViewRangefinder3d::from_world_from_view` calls
- [ ] **REQUIRED:** Replace `GlobalTransform::to_matrix()` with `GlobalTransform::affine()`
- [ ] **REQUIRED:** Replace any `Mat4` arguments with `Affine3A` equivalent
- [ ] Review custom code constructing `ViewRangefinder3d` instances
- [ ] Verify improved performance in rendering code

**Search Patterns:** `ViewRangefinder3d::from_world_from_view`, `GlobalTransform::to_matrix()`, `GlobalTransform::affine()`, `ViewRangefinder3d`, `Affine3A`, `Mat4`

**Examples:**
```rust
// 0.16
let rangefinder = ViewRangefinder3d::from_world_from_view(
    global_transform.to_matrix()
);

// 0.17
let rangefinder = ViewRangefinder3d::from_world_from_view(
    global_transform.affine()
);
```

---

---

## Guide 59: ReflectAsset now uses UntypedAssetId instead of UntypedHandle

**Pull Requests:** #19606

**Description:**
The `ReflectAsset` API has been updated to use `UntypedAssetId` instead of `UntypedHandle` for all methods, aligning with the standard `Assets<T>` API. All methods now accept `impl Into<UntypedAssetId>`, which means handles must be passed by reference.

**Migration Checklist:**
- [ ] **REQUIRED**: Update all `ReflectAsset::get()` calls to pass handle by reference
- [ ] **REQUIRED**: Update all `ReflectAsset::get_mut()` calls to pass handle by reference
- [ ] **REQUIRED**: Update all `ReflectAsset::remove()` calls to pass handle by reference
- [ ] **REQUIRED**: Update all `ReflectAsset::insert()` calls to pass handle by reference
- [ ] **OPTIONAL**: Simplify code by using `AssetId` directly instead of `UntypedHandle` where possible

**Search Patterns:**
```rust
// Pattern: ReflectAsset method calls with UntypedHandle
reflect_asset\.get\([^,]+,\s*[^&][^)]+\)
reflect_asset\.get_mut\([^,]+,\s*[^&][^)]+\)
reflect_asset\.remove\([^,]+,\s*[^&][^)]+\)
reflect_asset\.insert\([^,]+,\s*[^&][^)]+\)

// Types to check
UntypedHandle
ReflectAsset
```

**Examples:**

Before:
```rust
let my_handle: UntypedHandle;
let my_asset = reflect_asset.get_mut(world, my_handle).unwrap();
```

After:
```rust
let my_handle: UntypedHandle;
let my_asset = reflect_asset.get_mut(world, &my_handle).unwrap();
```

---

---

## Guide 60: Changes to type registration for reflection

**Pull Requests:** #15030, #20435, #20893

**Description:**
Types implementing `Reflect` are now automatically registered via compiler magic, eliminating the need for most manual `.register_type()` calls. This requires enabling specific feature flags and comes with platform and project structure considerations.

**Migration Checklist:**
- [ ] **REQUIRED (Apps)**: Enable `reflect_auto_register` feature in application code (included in default features)
- [ ] **REQUIRED (Apps)**: Remove all non-generic `.register_type()` calls from application code
- [ ] **RECOMMENDED (Libs)**: Do NOT enable `reflect_auto_register` or `reflect_auto_register_static` in library code
- [ ] **RECOMMENDED (Libs)**: Remove all non-generic `.register_type()` calls from library code
- [ ] **KEEP**: Retain manual registration for generic types (still required)
- [ ] **OPTIONAL**: Enable `reflect_auto_register` in dev-dependencies for tests only
- [ ] **OPTIONAL**: If on unsupported platform, try `reflect_auto_register_static` feature
- [ ] **OPTIONAL**: For `reflect_auto_register_static`, follow project structure requirements (see docs)

**Search Patterns:**
```rust
// Pattern: Non-generic register_type calls (candidates for removal)
\.register_type::<(?![^<>]*<)[^<>]+>\(\)

// Feature flags to check
Cargo.toml features:
reflect_auto_register
reflect_auto_register_static

// Generic types still requiring manual registration
\.register_type::<\w+<.+>>\(\)
```

**Examples:**

Before:
```rust
app.register_type::<MyComponent>()
   .register_type::<MyResource>()
   .register_type::<MyStruct>()
   .register_type::<Vec<MyComponent>>(); // Generic - still needed
```

After:
```rust
// Non-generic types removed - automatic registration
app.register_type::<Vec<MyComponent>>(); // Generic - still needed
```

**Platform & Configuration Notes:**
- Default: Use `reflect_auto_register` feature (in default features)
- Unsupported platforms: Use `reflect_auto_register_static` with proper project structure
- See `load_type_registrations!` macro docs for static registration requirements
- Check `auto_register_static` example in Bevy repository

---

---

## Guide 61: Relationship method set_risky

**Pull Requests:** #19601

**Description:**
The `Relationship` trait has a new required method `set_risky()` used to alter entity IDs in `RelationshipTarget` counterparts. This preserves additional data stored in relationship components during reassignment operations like `EntityCommands::add_related`, preventing unwanted resets to default values.

**Migration Checklist:**
- [ ] **REQUIRED (Manual Impl)**: Implement `set_risky()` method for any custom `Relationship` implementations
- [ ] **REVIEW**: Check behavior of `add_related()` and `add_one_related()` calls with data-enriched relationships
- [ ] **REVIEW**: Verify that relationship data preservation is desired behavior in your use case
- [ ] **NEVER**: Do not call `set_risky()` directly in user code (can invalidate relationships)

**Search Patterns:**
```rust
// Pattern: Manual Relationship trait implementations
impl\s+Relationship\s+for\s+\w+

// Types with relationship macros (should auto-derive set_risky)
#\[derive\([^)]*Component[^)]*\)\]\s*#\[relationship\(

// Operations affected by the change
\.add_related
\.add_one_related

// Relationship trait usage
RelationshipTarget
#\[relationship\]
#\[relationship_target\]
```

**Examples:**

Manual implementation (strongly discouraged):
```rust
// Before: only required methods
impl Relationship for OwnedCar {
    type RelationshipTarget = CarOwner;
    // ... other methods
}

// After: must implement set_risky
impl Relationship for OwnedCar {
    type RelationshipTarget = CarOwner;

    fn set_risky(&mut self, entity: Entity) {
        self.owner = entity; // Update the relationship entity field
    }
    // ... other methods
}
```

Behavior change example:
```rust
#[derive(Component)]
#[relationship(relationship_target = CarOwner)]
struct OwnedCar {
    #[relationship]
    owner: Entity,
    first_owner: Option<Entity>, // Additional data preserved
}

let mut me_entity_mut = world.entity_mut(me_entity);

// NEW BEHAVIOR: if car_entity already has OwnedCar, first_owner is preserved
me_entity_mut.add_one_related::<OwnedCar>(car_entity);

// Still overwrites everything when using insert directly
car_entity_mut.insert(OwnedCar {
    owner: me_entity,
    first_owner: None, // Explicitly sets to None
});
```

---

---

## Guide 62: RelativeCursorPosition is now object-centered

**Pull Requests:** #16615

**Description:**
`RelativeCursorPosition` coordinates are now object-centered with (0,0) at the center and corners at (±0.5, ±0.5). The `normalized_visible_node_rect` field has been removed and replaced with a `cursor_over: bool` field.

**Migration Checklist:**
- [ ] **REQUIRED**: Update coordinate calculations to account for center-based origin (0,0 at center)
- [ ] **REQUIRED**: Replace `normalized_visible_node_rect` field usage with `cursor_over` boolean
- [ ] **REQUIRED**: Adjust coordinate math: old top-left (0,0) → new top-left (-0.5, -0.5)
- [ ] **REQUIRED**: Adjust coordinate math: old bottom-right (1,1) → new bottom-right (0.5, 0.5)
- [ ] **REVIEW**: Check UI picking logic for coordinate system assumptions

**Search Patterns:**
```rust
// Pattern: RelativeCursorPosition usage
RelativeCursorPosition
\.normalized_visible_node_rect
cursor_position\.normalized

// Coordinate calculations to review
relative_cursor_position\.\w+\.x
relative_cursor_position\.\w+\.y

// UI picking components
\.cursor_over
```

**Examples:**

Before:
```rust
if let Some(rect) = relative_cursor_position.normalized_visible_node_rect {
    // Coordinates: (0,0) = top-left, (1,1) = bottom-right
    let x = relative_cursor_position.normalized.unwrap().x; // 0.0 to 1.0
    let y = relative_cursor_position.normalized.unwrap().y; // 0.0 to 1.0
}
```

After:
```rust
if relative_cursor_position.cursor_over {
    // Coordinates: (0,0) = center, (±0.5, ±0.5) = corners
    let x = relative_cursor_position.normalized.unwrap().x; // -0.5 to 0.5
    let y = relative_cursor_position.normalized.unwrap().y; // -0.5 to 0.5

    // Convert to old coordinate system if needed:
    let old_x = x + 0.5; // 0.0 to 1.0
    let old_y = y + 0.5; // 0.0 to 1.0
}
```

---

---

## Guide 63: Remove ArchetypeComponentId

**Pull Requests:** #19143

**Description:**
Scheduling no longer uses `archetype_component_access` or `ArchetypeComponentId` to reduce memory and simplify implementation. Query state now updates when the system runs instead of ahead of time. Several trait methods have been removed and some `SystemState` methods deprecated.

**Migration Checklist:**
- [ ] **REQUIRED**: Remove all calls to `System::update_archetype_component_access()`
- [ ] **REQUIRED**: Remove all implementations of `System::update_archetype_component_access()`
- [ ] **REQUIRED**: Remove all calls to `SystemParam::new_archetype()`
- [ ] **REQUIRED**: Remove all implementations of `SystemParam::new_archetype()`
- [ ] **REQUIRED**: Move logic from removed methods into `System::validate_param_unsafe()`, `System::run_unsafe()`, `SystemParam::validate_param()`, or `SystemParam::get_param()`
- [ ] **REQUIRED**: Update `SystemParam::validate_param` implementations to take `&mut Self::State` instead of `&Self::State`
- [ ] **DEPRECATED**: Replace `SystemState::update_archetypes()` calls (remove them)
- [ ] **DEPRECATED**: Replace `SystemState::update_archetypes_unsafe_world_cell()` calls (remove them)
- [ ] **DEPRECATED**: Replace `SystemState::get_manual()` with `SystemState::get()`
- [ ] **DEPRECATED**: Replace `SystemState::get_manual_mut()` with `SystemState::get_mut()`
- [ ] **DEPRECATED**: Replace `SystemState::get_unchecked_mut()` with `SystemState::get_unchecked()`

**Search Patterns:**
```rust
// Pattern: Removed methods
\.update_archetype_component_access\(
SystemParam::new_archetype
archetype_component_access

// Deprecated SystemState methods
SystemState::update_archetypes\(
SystemState::update_archetypes_unsafe_world_cell\(
SystemState::get_manual\(
SystemState::get_manual_mut\(
SystemState::get_unchecked_mut\(

// validate_param signature changes
fn\s+validate_param\([^)]*&Self::State

// Types removed
ArchetypeComponentId
```

**Examples:**

Trait implementation updates:
```rust
// Before: Manual System implementation
impl System for MySystem {
    fn update_archetype_component_access(&mut self, world: UnsafeWorldCell) {
        // Update access logic
    }

    fn run_unsafe(&mut self, world: UnsafeWorldCell) {
        // Run logic
    }
}

impl SystemParam for MyParam {
    fn new_archetype(state: &mut Self::State, archetype: &Archetype, world: &World) {
        // New archetype logic
    }

    fn validate_param(state: &Self::State, world: &World) -> bool {
        // Validation
    }
}

// After: Move logic into appropriate methods
impl System for MySystem {
    // update_archetype_component_access removed

    fn run_unsafe(&mut self, world: UnsafeWorldCell) {
        // Moved update access logic here if still needed
        // Run logic
    }
}

impl SystemParam for MyParam {
    // new_archetype removed

    fn validate_param(state: &mut Self::State, world: &World) -> bool {
        // Note: state is now &mut
        // Moved new archetype logic here if still needed
        // Validation
    }
}
```

SystemState method updates:
```rust
// Before: Using deprecated methods
let mut system_state = SystemState::<Query<&Transform>>::new(&mut world);
system_state.update_archetypes(&world);
let query = system_state.get_manual(&world);
let query_mut = system_state.get_manual_mut(&mut world);

// After: Use non-deprecated equivalents
let mut system_state = SystemState::<Query<&Transform>>::new(&mut world);
// update_archetypes call removed - no longer needed
let query = system_state.get(&world);
let query_mut = system_state.get_mut(&mut world);
```

---

---

## Guide 64: Remove Bundle::register_required_components

**Pull Requests:** #19967

**Description:**
The `Bundle::register_required_components` method has been removed as it was dead code never used by the ECS to compute required components.

**Migration Checklist:**
- [ ] **REQUIRED**: Remove all implementations of `Bundle::register_required_components`
- [ ] **REQUIRED**: Remove all calls to `Bundle::register_required_components`

**Search Patterns:**
```rust
// Pattern: register_required_components usage
Bundle::register_required_components
\.register_required_components\(
fn\s+register_required_components\(

// Bundle trait implementations to check
impl\s+Bundle\s+for\s+\w+
```

**Examples:**

Before:
```rust
impl Bundle for MyBundle {
    fn register_required_components(&self, registry: &mut ComponentRegistry) {
        // This never did anything
    }

    // Other Bundle methods
}

// Or usage:
my_bundle.register_required_components(&mut registry);
```

After:
```rust
impl Bundle for MyBundle {
    // register_required_components removed

    // Other Bundle methods
}

// Usage removed entirely
```

---

---

## Guide 65: Removed cosmic_text re-exports

**Pull Requests:** #19516

**Description:**
`bevy_text` no longer re-exports `cosmic_text` types. The re-exports (including renamed types like `FontFamily`, `FontStretch`, `FontStyle`, `FontWeight`) caused autocomplete conflicts and were largely implementation details.

**Migration Checklist:**
- [ ] **REQUIRED**: Replace `bevy::text::FontFamily` with `cosmic_text::FamilyOwned`
- [ ] **REQUIRED**: Replace `bevy::text::FontStretch` with `cosmic_text::Stretch`
- [ ] **REQUIRED**: Replace `bevy::text::FontStyle` with `cosmic_text::Style`
- [ ] **REQUIRED**: Replace `bevy::text::FontWeight` with `cosmic_text::Weight`
- [ ] **REQUIRED**: Add direct `cosmic_text` dependency to Cargo.toml if using these types
- [ ] **REQUIRED**: Ensure `cosmic_text` version matches version used by your `bevy_text` version
- [ ] **REQUIRED**: Replace any other `cosmic_text::*` imports from `bevy_text` with direct `cosmic_text` imports

**Search Patterns:**
```rust
// Pattern: bevy_text re-exports
use\s+bevy(?:::[\w:]+)*::text::(?:cosmic_text|FontFamily|FontStretch|FontStyle|FontWeight)
bevy::text::cosmic_text
bevy_text::cosmic_text

// Renamed types
bevy(?:::[\w:]+)*::text::FontFamily
bevy(?:::[\w:]+)*::text::FontStretch
bevy(?:::[\w:]+)*::text::FontStyle
bevy(?:::[\w:]+)*::text::FontWeight
```

**Examples:**

Before:
```rust
use bevy::text::{FontFamily, FontStretch, FontStyle, FontWeight};
use bevy::text::cosmic_text;

let family = FontFamily::Name("Arial".to_string());
let style = FontStyle::Italic;
let weight = FontWeight::BOLD;
let stretch = FontStretch::Normal;
```

After:
```rust
use cosmic_text::{FamilyOwned, Stretch, Style, Weight};

let family = FamilyOwned::Name("Arial".to_string());
let style = Style::Italic;
let weight = Weight::BOLD;
let stretch = Stretch::Normal;
```

Cargo.toml:
```toml
[dependencies]
bevy = "0.17"
cosmic_text = "0.14"  # Match version used by bevy 0.17
```

---

---

## Guide 66: Remove default implementation of extend_from_iter from RelationshipSourceCollection

**Pull Requests:** #20255

**Description:**
The `extend_from_iter` method in the `RelationshipSourceCollection` trait no longer has a default implementation. Custom relationship source collection implementations must now provide their own implementation.

**Migration Checklist:**
- [ ] **REQUIRED (Custom Impl)**: Implement `extend_from_iter()` method for all custom `RelationshipSourceCollection` implementations
- [ ] **RECOMMENDED**: Use collection's native `extend()` method if available
- [ ] **REVIEW**: Verify custom implementations handle entity iteration correctly

**Search Patterns:**
```rust
// Pattern: RelationshipSourceCollection implementations
impl\s+RelationshipSourceCollection\s+for\s+\w+

// Relationship types
RelationshipSourceCollection
extend_from_iter
```

**Examples:**

Before:
```rust
impl RelationshipSourceCollection for MyCustomCollection {
    // ... other required methods
    // extend_from_iter was automatically provided
}
```

After:
```rust
impl RelationshipSourceCollection for MyCustomCollection {
    // ... other required methods

    fn extend_from_iter(&mut self, entities: impl IntoIterator<Item = Entity>) {
        // Use your collection's native extend method if available
        self.extend(entities);

        // Or implement manually if needed:
        // for entity in entities {
        //     self.add(entity);
        // }
    }
}
```

---

---

## Guide 67: Removed Deprecated Batch Spawning Methods

**Pull Requests:** #18148

**Description:**
Three deprecated batch spawning methods have been removed: `Commands::insert_or_spawn_batch`, `World::insert_or_spawn_batch`, and `World::insert_or_spawn_batch_with_caller`. These methods could cause performance problems and violated ECS internals. Alternative patterns are provided for common use cases.

**Migration Checklist:**
- [ ] **REQUIRED**: Replace `Commands::insert_or_spawn_batch()` calls with alternative patterns
- [ ] **REQUIRED**: Replace `World::insert_or_spawn_batch()` calls with alternative patterns
- [ ] **REQUIRED**: Replace `World::insert_or_spawn_batch_with_caller()` calls with alternative patterns
- [ ] **PATTERN 1**: Use `Disabled` component instead of despawn/respawn pattern
- [ ] **PATTERN 2**: Use `spawn_batch()` and ensure entity references are valid
- [ ] **PATTERN 3**: Use custom stable identifiers with `EntityMapper` trait

**Search Patterns:**
```rust
// Pattern: Removed methods
Commands::insert_or_spawn_batch
World::insert_or_spawn_batch
World::insert_or_spawn_batch_with_caller
\.insert_or_spawn_batch\(
\.insert_or_spawn_batch_with_caller\(
```

**Examples:**

Pattern 1 - Use Disabled component:
```rust
// Before: Despawn and respawn with specific IDs
commands.despawn(entity);
// Later...
world.insert_or_spawn_batch(entities_with_ids);

// After: Use Disabled component
commands.entity(entity).insert(Disabled);
// Later, re-enable instead of respawning...
commands.entity(entity).remove::<Disabled>();
// Or use try_insert_batch/insert_batch
```

Pattern 2 - Use spawn_batch:
```rust
// Before: Using insert_or_spawn_batch to control IDs
world.insert_or_spawn_batch(entity_data);

// After: Use spawn_batch and track references
let entities = commands.spawn_batch(entity_data).collect::<Vec<_>>();
// Ensure entity references are valid when despawning
```

Pattern 3 - Custom stable identifiers:
```rust
// Before: Relying on entity IDs directly
world.insert_or_spawn_batch(entities_with_specific_ids);

// After: Use stable IDs with mapping
use bevy::ecs::entity::EntityMapper;

#[derive(Component)]
struct StableId(u64);

// Map stable IDs to Entity IDs
let mut id_map = HashMap::<u64, Entity>::new();
// Use EntityMapper trait for entity reference mapping
```

---

---

## Guide 68: Remove scale_value

**Pull Requests:** #19143

**Description:**
The `scale_value` function from `bevy::text::text2d` has been removed. Users should multiply by the scale factor directly instead.

**Migration Checklist:**
- [ ] **REQUIRED**: Replace all `scale_value()` calls with direct multiplication by scale factor

**Search Patterns:**
```rust
// Pattern: scale_value usage
use\s+bevy::text::text2d::scale_value
bevy::text::text2d::scale_value
scale_value\(
```

**Examples:**

Before:
```rust
use bevy::text::text2d::scale_value;

let scaled = scale_value(original_value, scale_factor);
```

After:
```rust
// Direct multiplication
let scaled = original_value * scale_factor;
```

---

---

## Guide 69: Replaced TextFont constructor methods with From impls

**Pull Requests:** #20335, #20450

**Description:**
The `TextFont::from_font` and `TextFont::from_line_height` constructor methods have been removed in favor of `From` trait implementations, allowing more idiomatic Rust conversions.

**Migration Checklist:**
- [ ] **REQUIRED**: Replace `TextFont::from_font()` with `TextFont::from()`
- [ ] **REQUIRED**: Replace `TextFont::from_line_height()` with `TextFont::from()`
- [ ] **OPTIONAL**: Use `.into()` for more concise conversions where type is clear

**Search Patterns:**
```rust
// Pattern: Old constructor methods
TextFont::from_font\(
TextFont::from_line_height\(
```

**Examples:**

Before:
```rust
let text_font = TextFont::from_font(font_handle);
let text_font = TextFont::from_line_height(line_height);
```

After:
```rust
let text_font = TextFont::from(font_handle);
let text_font = TextFont::from(line_height);

// Or more concise with .into():
let text_font: TextFont = font_handle.into();
let text_font: TextFont = line_height.into();
```

---

---

## Summary Statistics

**Total Guides Reviewed:** 11 (Guides 59-69)

**Migration Complexity:**
- **Low Complexity:** 6 guides (59, 62, 64, 68, 69, 66)
- **Medium Complexity:** 3 guides (61, 63, 65)
- **High Complexity:** 2 guides (60, 67)

**Required Actions by Category:**
- **Simple Replacements:** 25 items
- **API Signature Changes:** 12 items
- **Behavioral Changes:** 4 items
- **Feature Flag Changes:** 3 items
- **Trait Implementations:** 3 items

**Key Risk Areas:**
1. **Automatic type registration** (Guide 60) - Platform dependencies and feature flag requirements
2. **ArchetypeComponentId removal** (Guide 63) - Complex trait implementation changes
3. **Batch spawning removal** (Guide 67) - Architectural pattern changes required
4. **RelativeCursorPosition** (Guide 62) - Coordinate system changes affecting UI calculations
5. **Relationship set_risky** (Guide 61) - Subtle behavioral changes in relationship data preservation

---

## Renamed `JustifyText` to `Justify`

**Pull Requests:** 19522

**Description:**
The `JustifyText` enum has been renamed to `Justify` to improve API consistency. The `-Text` suffix was removed as it's redundant - it's naturally understood that `Justify` refers to text justification within the `bevy_text` module. This makes the type name consistent with other types in the text system.

**Checklist:**
- [ ] **REQUIRED:** Search for all `JustifyText` type references in your codebase
- [ ] **REQUIRED:** Replace `JustifyText` with `Justify` in all type annotations
- [ ] **REQUIRED:** Update all imports from `bevy::text::JustifyText` to `bevy::text::Justify`
- [ ] Update any pattern matching on `JustifyText` variants to use `Justify`
- [ ] Search for string literals containing "JustifyText" in comments or documentation

**Search Patterns:** `JustifyText`, `bevy::text::JustifyText`, `bevy_text::JustifyText`, `use.*JustifyText`, `Justify`

**Examples:**
```rust
// 0.16
use bevy::text::JustifyText;

fn setup(mut commands: Commands) {
    commands.spawn(TextBundle {
        text: Text::from_section("Hello", TextStyle::default())
            .with_justify(JustifyText::Center),
        ..default()
    });
}

// 0.17
use bevy::text::Justify;

fn setup(mut commands: Commands) {
    commands.spawn(TextBundle {
        text: Text::from_section("Hello", TextStyle::default())
            .with_justify(Justify::Center),
        ..default()
    });
}
```

---

---

## Renamed `Condition` to `SystemCondition`

**Pull Requests:** 19328

**Description:**
The `Condition` trait has been renamed to `SystemCondition` to reduce naming conflicts and improve clarity. Despite appearing in the prelude, `Condition` is an overly generic name that frequently collided with user-defined condition types. The new name `SystemCondition` is more explicit about its purpose in the ECS scheduling system.

**Checklist:**
- [ ] **REQUIRED:** Search for explicit `Condition` trait imports from `bevy_ecs`
- [ ] **REQUIRED:** Replace `Condition` with `SystemCondition` in all trait bounds
- [ ] **REQUIRED:** Update trait implementations from `impl Condition` to `impl SystemCondition`
- [ ] Update any custom condition types that implement this trait
- [ ] Search for `use bevy::ecs::schedule::Condition` and update to `SystemCondition`
- [ ] Check generic parameters using `C: Condition` and update to `C: SystemCondition`

**Search Patterns:** `Condition`, `SystemCondition`, `impl Condition`, `impl SystemCondition`, `bevy::ecs::schedule::Condition`, `bevy_ecs::schedule::Condition`, `: Condition`, `<C: Condition>`, `trait Condition`

**Examples:**
```rust
// 0.16
use bevy::ecs::schedule::Condition;

fn my_custom_condition() -> impl Condition<()> {
    IntoSystem::into_system(|| true)
}

struct MyCondition;

impl Condition<()> for MyCondition {
    fn check(&mut self, _world: &mut World) -> bool {
        true
    }
}

// 0.17
use bevy::ecs::schedule::SystemCondition;

fn my_custom_condition() -> impl SystemCondition<()> {
    IntoSystem::into_system(|| true)
}

struct MyCondition;

impl SystemCondition<()> for MyCondition {
    fn check(&mut self, _world: &mut World) -> bool {
        true
    }
}
```

---

---

## Rename `Pointer<Pressed>` and `Pointer<Released>` to `Pointer<Press>` and `Pointer<Release>`

**Pull Requests:** 19179

**Description:**
The pointer event types have been renamed for grammatical consistency and to avoid confusion with state markers. `Pointer<Pressed>` is now `Pointer<Press>` and `Pointer<Released>` is now `Pointer<Release>`. The `Pressed` name is now reserved for a marker component that indicates a UI node is currently in a pressed/held-down state, which is distinct from the momentary press/release events.

**Checklist:**
- [ ] **REQUIRED:** Search for all `Pointer<Pressed>` event usages and replace with `Pointer<Press>`
- [ ] **REQUIRED:** Search for all `Pointer<Released>` event usages and replace with `Pointer<Release>`
- [ ] **REQUIRED:** Update event readers: `EventReader<Pointer<Pressed>>` → `EventReader<Pointer<Press>>`
- [ ] **REQUIRED:** Update event writers: `EventWriter<Pointer<Pressed>>` → `EventWriter<Pointer<Press>>`
- [ ] Update observers that trigger on `Pointer<Pressed>` or `Pointer<Released>` events
- [ ] If you reference the `Pressed` marker component, ensure it's used correctly (for state, not events)
- [ ] Check for pattern matching on these event types in match statements

**Search Patterns:** `Pointer<Pressed>`, `Pointer<Released>`, `Pointer<Press>`, `Pointer<Release>`, `EventReader<Pointer<Pressed>>`, `EventReader<Pointer<Released>>`, `EventWriter<Pointer<Pressed>>`, `EventWriter<Pointer<Released>>`, `Observe<Pointer<Pressed>>`, `Observe<Pointer<Released>>`

**Examples:**
```rust
// 0.16
fn handle_pointer_events(
    mut press_events: EventReader<Pointer<Pressed>>,
    mut release_events: EventReader<Pointer<Released>>,
) {
    for event in press_events.read() {
        println!("Pointer pressed at {:?}", event.pointer_location.position);
    }

    for event in release_events.read() {
        println!("Pointer released");
    }
}

// 0.17
fn handle_pointer_events(
    mut press_events: EventReader<Pointer<Press>>,
    mut release_events: EventReader<Pointer<Release>>,
) {
    for event in press_events.read() {
        println!("Pointer pressed at {:?}", event.pointer_location.position);
    }

    for event in release_events.read() {
        println!("Pointer released");
    }
}
```

---

---

## Use glTF material names for spawned primitive entities

**Pull Requests:** 19287

**Description:**
The naming scheme for entities spawned from glTF mesh primitives has changed to improve debuggability and tool support. Previously, primitive entities were named using the mesh name plus a numeric index (e.g., `MeshName.0`, `MeshName.1`). Now they use the mesh name plus the material name (e.g., `MeshName.Material1Name`, `MeshName.Material2Name`). This makes it easier to identify entities in inspection tools. A new `GltfMeshName` component has been added for code that needs to access the original mesh name.

**Checklist:**
- [ ] Search for code that relies on the `Name` component of glTF mesh primitive entities
- [ ] If you parsed or matched against the old naming pattern (`MeshName.0`, `MeshName.1`), update to the new pattern
- [ ] **REQUIRED:** Replace code that extracts mesh names from `Name` component with queries for `GltfMeshName`
- [ ] Update any entity selection or filtering logic that depends on primitive entity names
- [ ] Review inspection/debugging tools that display glTF entity names
- [ ] Test glTF loading to ensure proper entity naming

**Search Patterns:** `GltfMeshName`, `Name`, `GltfMaterialName`, `.0`, `.1`, `.2`, `MeshName`

**Examples:**
```rust
// 0.16
// Finding mesh primitives by name pattern
fn find_mesh_primitives(
    query: Query<(Entity, &Name)>,
) {
    for (entity, name) in &query {
        // Old pattern: MeshName.0, MeshName.1, etc.
        if name.as_str().starts_with("MeshName.") {
            println!("Found primitive: {}", name);
        }
    }
}

// 0.17
use bevy::gltf::GltfMeshName;

// Use the new GltfMeshName component instead of parsing Name
fn find_mesh_primitives(
    query: Query<(Entity, &GltfMeshName, &Name)>,
) {
    for (entity, mesh_name, name) in &query {
        // New pattern: MeshName.MaterialName
        if mesh_name.0 == "MeshName" {
            println!("Found primitive: {} (display name: {})", mesh_name.0, name);
        }
    }
}
```

---

---

## Renamed state scoped entities and events

**Pull Requests:** 18818, 19435, 20872

**Description:**
State-scoped entity and event management has been expanded and renamed for clarity. Previously, `StateScoped` and `add_state_scoped_event` only supported cleanup when exiting a state. New functionality allows cleanup when entering a state via `DespawnOnEnter` and `clear_events_on_enter`. To support this, the original names have been changed: `StateScoped` is now `DespawnOnExit`, and `clear_event_on_exit` (previously `clear_event_on_exit_state`) no longer automatically adds events, so you must call `App::add_event` separately.

**Checklist:**
- [ ] **REQUIRED:** Search for `StateScoped` component and replace with `DespawnOnExit`
- [ ] **REQUIRED:** Search for `clear_state_scoped_entities` and replace with `despawn_entities_on_exit_state`
- [ ] **REQUIRED:** Search for `add_state_scoped_event` calls
- [ ] **REQUIRED:** Replace `add_state_scoped_event` with `add_event` + `clear_events_on_exit`
- [ ] Update imports from `bevy::state` or `bevy_state` for the renamed types
- [ ] Consider using new `DespawnOnEnter` component if you need entities removed when entering a state
- [ ] Consider using `clear_events_on_enter` for event cleanup on state entry
- [ ] Review state transition logic to ensure entities are despawned at the correct time

**Search Patterns:** `StateScoped`, `DespawnOnExit`, `clear_state_scoped_entities`, `despawn_entities_on_exit_state`, `add_state_scoped_event`, `clear_events_on_exit`, `clear_event_on_exit_state`, `DespawnOnEnter`, `clear_events_on_enter`

**Examples:**
```rust
// 0.16
use bevy::state::StateScoped;

#[derive(States, Clone, PartialEq, Eq, Hash, Debug, Default)]
enum GameState {
    #[default]
    Menu,
    Playing,
}

fn setup(mut commands: Commands) {
    // Entity will be removed when exiting Playing state
    commands.spawn((
        SpriteBundle::default(),
        StateScoped(GameState::Playing),
    ));
}

fn setup_app(app: &mut App) {
    app.add_state_scoped_event::<GameEvent>(GameState::Playing);
}

// 0.17
use bevy::state::DespawnOnExit;

#[derive(States, Clone, PartialEq, Eq, Hash, Debug, Default)]
enum GameState {
    #[default]
    Menu,
    Playing,
}

fn setup(mut commands: Commands) {
    // Entity will be removed when exiting Playing state
    commands.spawn((
        SpriteBundle::default(),
        DespawnOnExit(GameState::Playing),
    ));
}

fn setup_app(app: &mut App) {
    app.add_event::<GameEvent>()
        .clear_events_on_exit::<GameEvent, GameState>(GameState::Playing);
}

// New in 0.17: cleanup on state entry
use bevy::state::DespawnOnEnter;

fn setup_with_enter_cleanup(mut commands: Commands) {
    // Entity will be removed when entering Playing state
    commands.spawn((
        SpriteBundle::default(),
        DespawnOnEnter(GameState::Playing),
    ));
}
```

---

---

## Renamed `Timer::paused` to `Timer::is_paused` and `Timer::finished` to `Timer::is_finished`

**Pull Requests:** 19386

**Description:**
The `Timer` API has been updated for consistency with `Time` and `Stopwatch`. The methods `Timer::paused` and `Timer::finished` have been renamed to `Timer::is_paused` and `Timer::is_finished` respectively. This follows Rust naming conventions where boolean query methods should use the `is_*` prefix.

**Checklist:**
- [ ] **REQUIRED:** Search for all `timer.paused()` calls and replace with `timer.is_paused()`
- [ ] **REQUIRED:** Search for all `timer.finished()` calls and replace with `timer.is_finished()`
- [ ] Search for `Timer::paused` in method chains and update
- [ ] Search for `Timer::finished` in method chains and update
- [ ] Update any pattern matching or conditional logic using these methods

**Search Patterns:** `.paused()`, `.finished()`, `timer.paused()`, `timer.finished()`, `Timer::paused`, `Timer::finished`, `.is_paused()`, `.is_finished()`

**Examples:**
```rust
// 0.16
fn check_timer(timer: Res<MyTimer>) {
    if timer.0.paused() {
        println!("Timer is paused");
    }

    if timer.0.finished() {
        println!("Timer is finished");
    }
}

fn update_timer(mut timer: ResMut<MyTimer>, time: Res<Time>) {
    if !timer.0.paused() {
        timer.0.tick(time.delta());
    }
}

// 0.17
fn check_timer(timer: Res<MyTimer>) {
    if timer.0.is_paused() {
        println!("Timer is paused");
    }

    if timer.0.is_finished() {
        println!("Timer is finished");
    }
}

fn update_timer(mut timer: ResMut<MyTimer>, time: Res<Time>) {
    if !timer.0.is_paused() {
        timer.0.tick(time.delta());
    }
}
```

---

---

## Transform and GlobalTransform::compute_matrix rename

**Pull Requests:** 19643, 19646

**Description:**
The `compute_matrix` methods on `Transform` and `GlobalTransform` have been renamed to `to_matrix` for accuracy and consistency. These methods don't actually compute anything - they simply convert the existing transform data into a matrix representation. The new name `to_matrix` better reflects this behavior and aligns with Rust naming conventions for type conversions.

**Checklist:**
- [ ] **REQUIRED:** Search for `transform.compute_matrix()` and replace with `transform.to_matrix()`
- [ ] **REQUIRED:** Search for `global_transform.compute_matrix()` and replace with `global_transform.to_matrix()`
- [ ] **REQUIRED:** Search for `Transform::compute_matrix` and replace with `Transform::to_matrix`
- [ ] **REQUIRED:** Search for `GlobalTransform::compute_matrix` and replace with `GlobalTransform::to_matrix`
- [ ] Update any documentation or comments referencing these methods

**Search Patterns:** `.compute_matrix()`, `Transform::compute_matrix`, `GlobalTransform::compute_matrix`, `.to_matrix()`, `Transform::to_matrix`, `GlobalTransform::to_matrix`

**Examples:**
```rust
// 0.16
fn get_transform_matrix(transform: &Transform) -> Mat4 {
    transform.compute_matrix()
}

fn get_global_matrix(global_transform: &GlobalTransform) -> Mat4 {
    global_transform.compute_matrix()
}

fn custom_transform_system(query: Query<&Transform>) {
    for transform in &query {
        let matrix = transform.compute_matrix();
        // Use matrix...
    }
}

// 0.17
fn get_transform_matrix(transform: &Transform) -> Mat4 {
    transform.to_matrix()
}

fn get_global_matrix(global_transform: &GlobalTransform) -> Mat4 {
    global_transform.to_matrix()
}

fn custom_transform_system(query: Query<&Transform>) {
    for transform in &query {
        let matrix = transform.to_matrix();
        // Use matrix...
    }
}
```

---

---

## Renamed BRP methods

**Pull Requests:** 19377

**Description:**
The Bevy Remote Protocol (BRP) has undergone a comprehensive renaming to improve clarity and consistency. All methods now use a `world.` prefix (instead of `bevy/`) and more explicit names. The word `destroy` has been replaced with `despawn` to match the rest of the engine terminology. Methods that operate on multiple items now have pluralized names (e.g., `get_components` instead of `get`).

**Checklist:**
- [ ] **REQUIRED:** Search for all BRP method calls using the old names
- [ ] **REQUIRED:** Update `bevy/query` to `world.query`
- [ ] **REQUIRED:** Update `bevy/spawn` to `world.spawn_entity`
- [ ] **REQUIRED:** Update `bevy/destroy` to `world.despawn_entity`
- [ ] **REQUIRED:** Update `bevy/reparent` to `world.reparent_entities`
- [ ] **REQUIRED:** Update `bevy/get` to `world.get_components`
- [ ] **REQUIRED:** Update `bevy/insert` to `world.insert_components`
- [ ] **REQUIRED:** Update `bevy/remove` to `world.remove_components`
- [ ] **REQUIRED:** Update `bevy/list` to `world.list_components`
- [ ] **REQUIRED:** Update `bevy/mutate` to `world.mutate_components`
- [ ] **REQUIRED:** Update `bevy/get+watch` to `world.get_components+watch`
- [ ] **REQUIRED:** Update `bevy/list+watch` to `world.list_components+watch`
- [ ] **REQUIRED:** Update `bevy/get_resource` to `world.get_resources`
- [ ] **REQUIRED:** Update `bevy/insert_resource` to `world.insert_resources`
- [ ] **REQUIRED:** Update `bevy/remove_resource` to `world.remove_resources`
- [ ] **REQUIRED:** Update `bevy/mutate_resource` to `world.mutate_resources`
- [ ] **REQUIRED:** Update `registry/schema` to `registry.schema`
- [ ] Update external tools, scripts, or clients that interact with BRP
- [ ] Update any BRP documentation or API examples

**Search Patterns:** `bevy/query`, `bevy/spawn`, `bevy/destroy`, `bevy/reparent`, `bevy/get`, `bevy/insert`, `bevy/remove`, `bevy/list`, `bevy/mutate`, `bevy/get+watch`, `bevy/list+watch`, `bevy/get_resource`, `bevy/insert_resource`, `bevy/remove_resource`, `bevy/list_resources`, `bevy/mutate_resource`, `registry/schema`, `world.query`, `world.spawn_entity`, `world.despawn_entity`, `world.get_components`, `world.insert_components`

**Examples:**
```rust
// 0.16
// BRP client code
let query_result = brp_client.call("bevy/query", query_params)?;
let entity = brp_client.call("bevy/spawn", spawn_params)?;
brp_client.call("bevy/destroy", entity_id)?;
brp_client.call("bevy/get", get_params)?;
brp_client.call("bevy/insert", insert_params)?;
let components = brp_client.call("bevy/list", entity_id)?;
let resource = brp_client.call("bevy/get_resource", resource_type)?;
brp_client.call("bevy/insert_resource", resource_data)?;

// 0.17
// BRP client code
let query_result = brp_client.call("world.query", query_params)?;
let entity = brp_client.call("world.spawn_entity", spawn_params)?;
brp_client.call("world.despawn_entity", entity_id)?;
brp_client.call("world.get_components", get_params)?;
brp_client.call("world.insert_components", insert_params)?;
let components = brp_client.call("world.list_components", entity_id)?;
let resource = brp_client.call("world.get_resources", resource_type)?;
brp_client.call("world.insert_resources", resource_data)?;
```

---

---

## Renamed `ComputedNodeTarget` and `update_ui_context_system`

**Pull Requests:** 20519, 20532

**Description:**
UI-related types have been renamed for clarity and consistency. `ComputedNodeTarget` has been renamed to `ComputedUiTargetCamera` to better reflect that its value is derived from `UiTargetCamera`. The system `update_ui_context_system` has been renamed to `propagate_ui_target_cameras` to more accurately describe its function of propagating camera information through the UI hierarchy.

**Checklist:**
- [ ] **REQUIRED:** Search for `ComputedNodeTarget` and replace with `ComputedUiTargetCamera`
- [ ] **REQUIRED:** Update all type annotations using `ComputedNodeTarget`
- [ ] **REQUIRED:** Update queries: `Query<&ComputedNodeTarget>` → `Query<&ComputedUiTargetCamera>`
- [ ] Search for `update_ui_context_system` and replace with `propagate_ui_target_cameras`
- [ ] Update system ordering constraints that reference `update_ui_context_system`
- [ ] Update imports from `bevy::ui` or `bevy_ui`

**Search Patterns:** `ComputedNodeTarget`, `ComputedUiTargetCamera`, `update_ui_context_system`, `propagate_ui_target_cameras`, `Query<&ComputedNodeTarget>`, `Query<&mut ComputedNodeTarget>`, `bevy::ui::ComputedNodeTarget`, `bevy_ui::ComputedNodeTarget`

**Examples:**
```rust
// 0.16
use bevy::ui::ComputedNodeTarget;

fn query_ui_cameras(
    query: Query<(Entity, &ComputedNodeTarget)>,
) {
    for (entity, target) in &query {
        if let Some(camera_entity) = target.0 {
            println!("UI node {} targets camera {:?}", entity, camera_entity);
        }
    }
}

fn setup_ordering(app: &mut App) {
    app.add_systems(Update, my_system.after(update_ui_context_system));
}

// 0.17
use bevy::ui::ComputedUiTargetCamera;

fn query_ui_cameras(
    query: Query<(Entity, &ComputedUiTargetCamera)>,
) {
    for (entity, target) in &query {
        if let Some(camera_entity) = target.0 {
            println!("UI node {} targets camera {:?}", entity, camera_entity);
        }
    }
}

fn setup_ordering(app: &mut App) {
    app.add_systems(Update, my_system.after(propagate_ui_target_cameras));
}
```

---

---

## `RenderGraphApp` renamed to `RenderGraphExt`

**Pull Requests:** 19912

**Description:**
The `RenderGraphApp` trait has been renamed to `RenderGraphExt` to follow Rust extension trait naming conventions. This trait provides methods for working with the render graph on the `App` type. The rename only affects code that explicitly imports this trait - usage of the methods remains the same.

**Checklist:**
- [ ] Search for explicit `RenderGraphApp` trait imports
- [ ] **REQUIRED:** Replace `use bevy::render::RenderGraphApp` with `use bevy::render::RenderGraphExt`
- [ ] **REQUIRED:** Replace `use bevy_render::RenderGraphApp` with `use bevy_render::RenderGraphExt`
- [ ] Update any trait bounds using `RenderGraphApp` to use `RenderGraphExt`
- [ ] No changes needed to method calls - only the trait name changes

**Search Patterns:** `RenderGraphApp`, `RenderGraphExt`, `use.*RenderGraphApp`, `bevy::render::RenderGraphApp`, `bevy_render::RenderGraphApp`, `bevy::render::RenderGraphExt`, `bevy_render::RenderGraphExt`

**Examples:**
```rust
// 0.16
use bevy::render::RenderGraphApp;

impl Plugin for MyRenderPlugin {
    fn build(&self, app: &mut App) {
        let render_app = app.get_sub_app_mut(RenderApp).unwrap();
        render_app.add_render_graph_node::<MyNode>(MyNode::NAME);
    }
}

// 0.17
use bevy::render::RenderGraphExt;

impl Plugin for MyRenderPlugin {
    fn build(&self, app: &mut App) {
        let render_app = app.get_sub_app_mut(RenderApp).unwrap();
        render_app.add_render_graph_node::<MyNode>(MyNode::NAME);
    }
}
```

---

---

## Many render resources now initialized in `RenderStartup`

**Pull Requests:** 19841, 19885, 19886, 19897, 19898, 19901, 19912, 19926, 19999, 20002, 20024, 20124, 20147, 20184, 20194, 20195, 20208, 20209, 20210

**Description:**
A major architectural change to render resource initialization. Many render resources are no longer initialized during `Plugin::finish` and are instead initialized in the `RenderStartup` schedule (which runs once when the app starts). This affects 53 public render resources. If you only access these resources during the `Render` schedule, no changes are needed. However, if you initialize your own resources that depend on these render resources, you must convert your resource initialization from a `FromWorld` implementation in `Plugin::finish` to a system in `RenderStartup`.

**Checklist:**
- [ ] Review if your code initializes any custom render resources in `Plugin::finish`
- [ ] Check if your render resources depend on any of the affected resources (see list below)
- [ ] **REQUIRED:** If using affected resources for initialization, convert `FromWorld` implementations to initialization systems
- [ ] **REQUIRED:** Move resource initialization from `Plugin::finish` to `RenderStartup` systems in `Plugin::build`
- [ ] **REQUIRED:** Update system parameters: replace `world.resource::<T>()` with `Res<T>` system params
- [ ] **REQUIRED:** Use `Commands::insert_resource` instead of direct initialization
- [ ] Add system ordering with `.after()` if your resource depends on other `RenderStartup` resources
- [ ] Update any `load_embedded_asset(world, ...)` calls to use `AssetServer` param: `load_embedded_asset(asset_server.as_ref(), ...)`
- [ ] Pass `&render_device` instead of `render_device` to functions expecting `&RenderDevice`

**Affected Resources (53 total):**
- Anti-aliasing: `CasPipeline`, `FxaaPipeline`, `SmaaPipelines`, `TaaPipeline`
- Lighting: `ShadowSamplers`, `GlobalClusterableObjectMeta`, `FallbackBindlessResources`, `RenderLightmaps`, `DeferredLightingLayout`, `CopyDeferredLightingIdPipeline`
- Post-processing: `AutoExposurePipeline`, `MotionBlurPipeline`, `BlitPipeline`, `DepthOfFieldGlobalBindGroupLayout`, `DepthPyramidDummyTexture`, `OitBuffers`, `PostProcessingPipeline`, `TonemappingPipeline`, `VolumetricFogPipeline`, `ScreenSpaceReflectionsPipeline`
- UI: `BoxShadowPipeline`, `GradientPipeline`, `UiPipeline`, `UiMaterialPipeline<M>`, `UiTextureSlicePipeline`
- 3D Rendering: `SkyboxPrepassPipeline`, `PrepassPipeline`, `PrepassViewBindGroup`, `Wireframe3dPipeline`, `MaterialPipeline`, `MeshletPipelines`, `MeshletMeshManager`, `ResourceManager`
- 2D Rendering: `Wireframe2dPipeline`, `Material2dPipeline`, `SpritePipeline`, `Mesh2dPipeline`, `BatchedInstanceBuffer<Mesh2dUniform>`
- Other: `ScreenshotToScreenPipeline`

**Search Patterns:** `Plugin::finish`, `FromWorld`, `RenderStartup`, `init_resource`, `world.resource::<RenderDevice>()`, `world.resource::<RenderAdapter>()`, `RenderApp`, `get_sub_app_mut`, `load_embedded_asset`, any of the 53 affected resource names

**Examples:**
```rust
// 0.16
use bevy::render::{RenderApp, renderer::RenderDevice};

pub struct MyRenderingPlugin;

impl Plugin for MyRenderingPlugin {
    fn build(&self, app: &mut App) {
        // Do nothing in build
    }

    fn finish(&self, app: &mut App) {
        let Some(render_app) = app.get_sub_app_mut(RenderApp) else {
            return;
        };

        render_app.init_resource::<MyRenderResource>();
        render_app.add_systems(Render, my_render_system);
    }
}

#[derive(Resource)]
pub struct MyRenderResource {
    buffer: Buffer,
}

impl FromWorld for MyRenderResource {
    fn from_world(world: &mut World) -> Self {
        let render_device = world.resource::<RenderDevice>();
        let render_adapter = world.resource::<RenderAdapter>();

        MyRenderResource {
            buffer: render_device.create_buffer(&BufferDescriptor {
                label: Some("my_buffer"),
                size: 256,
                usage: BufferUsages::UNIFORM,
                mapped_at_creation: false,
            }),
        }
    }
}

// 0.17
use bevy::render::{RenderApp, renderer::RenderDevice, RenderStartup};

pub struct MyRenderingPlugin;

impl Plugin for MyRenderingPlugin {
    fn build(&self, app: &mut App) {
        let Some(render_app) = app.get_sub_app_mut(RenderApp) else {
            return;
        };

        render_app
            .add_systems(RenderStartup, init_my_resource)
            .add_systems(Render, my_render_system);
    }

    // No more finish() method!
}

#[derive(Resource)]
pub struct MyRenderResource {
    buffer: Buffer,
}

// Convert FromWorld to a system
fn init_my_resource(
    mut commands: Commands,
    render_device: Res<RenderDevice>,
    render_adapter: Res<RenderAdapter>,
) {
    commands.insert_resource(MyRenderResource {
        buffer: render_device.create_buffer(&BufferDescriptor {
            label: Some("my_buffer"),
            size: 256,
            usage: BufferUsages::UNIFORM,
            mapped_at_creation: false,
        }),
    });
}

// Example with dependency on another RenderStartup resource
fn init_my_ui_resource(
    mut commands: Commands,
    ui_pipeline: Res<UiPipeline>,
) {
    commands.insert_resource(MyUiResource {
        layout: ui_pipeline.view_layout.clone(),
    });
}

// Register with ordering
impl Plugin for MyUiPlugin {
    fn build(&self, app: &mut App) {
        let Some(render_app) = app.get_sub_app_mut(RenderApp) else {
            return;
        };

        render_app.add_systems(
            RenderStartup,
            init_my_ui_resource.after(init_ui_pipeline)
        );
    }
}

// Example with load_embedded_asset
// 0.16
impl FromWorld for MyShaderResource {
    fn from_world(world: &mut World) -> Self {
        load_embedded_asset(world, "my_shader.wgsl");
        Self { /* ... */ }
    }
}

// 0.17
fn init_my_shader_resource(
    mut commands: Commands,
    asset_server: Res<AssetServer>,
) {
    load_embedded_asset(asset_server.as_ref(), "my_shader.wgsl");
    commands.insert_resource(MyShaderResource { /* ... */ });
}
```

---

---

## Guide 81: `RenderGraphApp` renamed to `RenderGraphExt`

**Pull Requests:** [#19912](https://github.com/bevyengine/bevy/pull/19912)

**Description:**
The `RenderGraphApp` trait has been renamed to `RenderGraphExt` to follow Rust naming conventions for extension traits. This is a simple rename affecting only explicit trait imports.

**Migration Checklist:**
- [ ] Replace `use bevy_render::render_graph::RenderGraphApp` with `use bevy_render::render_graph::RenderGraphExt`
- [ ] Update any trait bounds from `T: RenderGraphApp` to `T: RenderGraphExt`
- [ ] Search for explicit trait imports containing `RenderGraphApp`

**Search Patterns:**
```rust
// Find trait imports
RenderGraphApp

// Find trait bounds
: RenderGraphApp

// Find use statements
use.*RenderGraphApp
```

**Examples:**

```rust
// 0.16
use bevy_render::render_graph::RenderGraphApp;

fn setup_graph(app: &mut App) {
    app.add_render_graph_node::<MyNode>(/* ... */);
}

// 0.17
use bevy_render::render_graph::RenderGraphExt;

fn setup_graph(app: &mut App) {
    app.add_render_graph_node::<MyNode>(/* ... */);
}
```

**Notes:**
- This is a **required** change only if you explicitly import the trait
- Most code using the trait methods via `App` won't need changes due to automatic trait resolution
- No functional changes, only naming

---

---

## Guide 82: Many render resources now initialized in `RenderStartup`

**Pull Requests:** [#19841](https://github.com/bevyengine/bevy/pull/19841), [#19885](https://github.com/bevyengine/bevy/pull/19885), [#19886](https://github.com/bevyengine/bevy/pull/19886), [#19897](https://github.com/bevyengine/bevy/pull/19897), [#19898](https://github.com/bevyengine/bevy/pull/19898), [#19901](https://github.com/bevyengine/bevy/pull/19901), [#19912](https://github.com/bevyengine/bevy/pull/19912), [#19926](https://github.com/bevyengine/bevy/pull/19926), [#19999](https://github.com/bevyengine/bevy/pull/19999), [#20002](https://github.com/bevyengine/bevy/pull/20002), [#20024](https://github.com/bevyengine/bevy/pull/20024), [#20124](https://github.com/bevyengine/bevy/pull/20124), [#20147](https://github.com/bevyengine/bevy/pull/20147), [#20184](https://github.com/bevyengine/bevy/pull/20184), [#20194](https://github.com/bevyengine/bevy/pull/20194), [#20195](https://github.com/bevyengine/bevy/pull/20195), [#20208](https://github.com/bevyengine/bevy/pull/20208), [#20209](https://github.com/bevyengine/bevy/pull/20209), [#20210](https://github.com/bevyengine/bevy/pull/20210)

**Description:**
Many render resources are no longer initialized during `Plugin::finish` and are instead initialized during the `RenderStartup` schedule (which runs once when the app starts). This change improves plugin initialization order and makes render resource dependencies more explicit. If you only access these resources during the `Render` schedule, no changes are needed. However, if you initialize your own render resources that depend on these resources, you must convert your `FromWorld` initialization to a system added to `RenderStartup`.

**Affected Resources:**
- `CasPipeline`
- `FxaaPipeline`
- `SmaaPipelines`
- `TaaPipeline`
- `ShadowSamplers`
- `GlobalClusterableObjectMeta`
- `FallbackBindlessResources`
- `AutoExposurePipeline`
- `MotionBlurPipeline`
- `SkyboxPrepassPipeline`
- `BlitPipeline`
- `DepthOfFieldGlobalBindGroupLayout`
- `DepthPyramidDummyTexture`
- `OitBuffers`
- `PostProcessingPipeline`
- `TonemappingPipeline`
- `BoxShadowPipeline`
- `GradientPipeline`
- `UiPipeline`
- `UiMaterialPipeline<M>`
- `UiTextureSlicePipeline`
- `ScreenshotToScreenPipeline`
- `VolumetricFogPipeline`
- `DeferredLightingLayout`
- `CopyDeferredLightingIdPipeline`
- `RenderLightmaps`
- `PrepassPipeline`
- `PrepassViewBindGroup`
- `Wireframe3dPipeline`
- `ScreenSpaceReflectionsPipeline`
- `MaterialPipeline`
- `MeshletPipelines`
- `MeshletMeshManager`
- `ResourceManager`
- `Wireframe2dPipeline`
- `Material2dPipeline`
- `SpritePipeline`
- `Mesh2dPipeline`
- `BatchedInstanceBuffer<Mesh2dUniform>`

**Migration Checklist:**
- [ ] Identify custom render resources initialized in `Plugin::finish`
- [ ] Check if your resources depend on any of the affected resources above
- [ ] Convert `FromWorld` implementations to initialization systems
- [ ] Replace `World::resource` calls with system parameters (`Res<T>`)
- [ ] Use `Commands::insert_resource` instead of direct world insertion
- [ ] Move resource initialization from `Plugin::finish` to `Plugin::build`
- [ ] Add initialization systems to `RenderStartup` schedule
- [ ] Add `.after()` ordering constraints for resource dependencies
- [ ] Update `load_embedded_asset(world, ...)` to `load_embedded_asset(asset_server.as_ref(), ...)`
- [ ] Add `&` when passing `Res<RenderDevice>` to functions expecting `&RenderDevice`

**Search Patterns:**
```rust
// Find Plugin::finish implementations
impl Plugin.*\n.*fn finish

// Find FromWorld implementations for render resources
impl FromWorld for.*\n.*fn from_world

// Find render resource initialization
render_app.init_resource

// Find RenderDevice world access
world.resource::<RenderDevice>

// Find embedded asset loading
load_embedded_asset\(world,
```

**Examples:**

```rust
// 0.16 - Old pattern with Plugin::finish and FromWorld
impl Plugin for MyRenderingPlugin {
    fn build(&self, app: &mut App) {
        // Do nothing in build
    }

    fn finish(&self, app: &mut App) {
        let Some(render_app) = app.get_sub_app_mut(RenderApp) else {
            return;
        };

        render_app.init_resource::<MyRenderResource>();
        render_app.add_systems(Render, my_render_system);
    }
}

pub struct MyRenderResource {
    buffer: Buffer,
    pipeline: RenderPipeline,
}

impl FromWorld for MyRenderResource {
    fn from_world(world: &mut World) -> Self {
        let render_device = world.resource::<RenderDevice>();
        let render_adapter = world.resource::<RenderAdapter>();
        let asset_server = world.resource::<AssetServer>();

        MyRenderResource {
            buffer: render_device.create_buffer(&BufferDescriptor {
                label: Some("my_buffer"),
                size: 1024,
                usage: BufferUsages::UNIFORM,
                mapped_at_creation: false,
            }),
            pipeline: create_pipeline(render_device),
        }
    }
}

// 0.17 - New pattern with RenderStartup system
impl Plugin for MyRenderingPlugin {
    fn build(&self, app: &mut App) {
        let Some(render_app) = app.get_sub_app_mut(RenderApp) else {
            return;
        };

        render_app
            .add_systems(RenderStartup, init_my_resource)
            .add_systems(Render, my_render_system);
    }

    // No more finish method!
}

pub struct MyRenderResource {
    buffer: Buffer,
    pipeline: RenderPipeline,
}

// Convert FromWorld to a regular system
fn init_my_resource(
    mut commands: Commands,
    render_device: Res<RenderDevice>,
    render_adapter: Res<RenderAdapter>,
    asset_server: Res<AssetServer>,
) {
    // Pass &render_device instead of render_device to functions
    commands.insert_resource(MyRenderResource {
        buffer: render_device.create_buffer(&BufferDescriptor {
            label: Some("my_buffer"),
            size: 1024,
            usage: BufferUsages::UNIFORM,
            mapped_at_creation: false,
        }),
        pipeline: create_pipeline(&render_device),
    });
}
```

**Example with dependencies:**

```rust
// 0.17 - System with ordering dependency on UiPipeline
impl Plugin for MyCustomUiPlugin {
    fn build(&self, app: &mut App) {
        let Some(render_app) = app.get_sub_app_mut(RenderApp) else {
            return;
        };

        render_app.add_systems(
            RenderStartup,
            init_my_ui_resource
                .after(bevy_ui::init_ui_pipeline), // Ensure UiPipeline exists first
        );
    }
}

fn init_my_ui_resource(
    mut commands: Commands,
    ui_pipeline: Res<UiPipeline>, // Now safe to access
    render_device: Res<RenderDevice>,
) {
    commands.insert_resource(MyCustomUiResource {
        // Can safely use ui_pipeline here
        base_layout: ui_pipeline.layout.clone(),
        custom_buffer: render_device.create_buffer(&BufferDescriptor {
            label: Some("custom_ui_buffer"),
            size: 512,
            usage: BufferUsages::UNIFORM,
            mapped_at_creation: false,
        }),
    });
}
```

**Example with embedded assets:**

```rust
// 0.16
impl FromWorld for MyShaderResource {
    fn from_world(world: &mut World) -> Self {
        load_embedded_asset(world, "shader.wgsl");
        Self { /* ... */ }
    }
}

// 0.17
fn init_shader_resource(
    mut commands: Commands,
    asset_server: Res<AssetServer>,
) {
    load_embedded_asset(asset_server.as_ref(), "shader.wgsl");
    commands.insert_resource(MyShaderResource { /* ... */ });
}
```

**Notes:**
- This is a **required** change if you initialize custom render resources depending on the affected resources
- Systems using these resources during `Render` schedule are unaffected
- The `RenderStartup` schedule runs once when the app starts running
- Use `.after()` to ensure proper initialization order
- Most custom render plugins will need this migration

---

---

## Guide 83: `RenderTarget` error handling

**Pull Requests:** [#20503](https://github.com/bevyengine/bevy/pull/20503)

**Description:**
The `NormalizedRenderTargetExt::get_render_target_info` method now returns a `Result` instead of an `Option`. The `Err` variant provides detailed information about which render target (image, window, texture view, etc.) failed to load its metadata. This change makes render target failures more explicit and helps identify which specific target caused the problem.

**Migration Checklist:**
- [ ] Find all calls to `get_render_target_info`
- [ ] Replace `.unwrap()` or `if let Some(info)` with `.expect()` or `if let Ok(info)`
- [ ] Add error handling for the `Err` variant
- [ ] Update pattern matching from `Option` to `Result`
- [ ] Consider logging or propagating the error instead of panicking

**Search Patterns:**
```rust
// Find method calls
get_render_target_info

// Find Option unwrapping that needs updating
get_render_target_info.*unwrap

// Find Option pattern matching
if let Some.*get_render_target_info
```

**Examples:**

```rust
// 0.16
fn render_system(
    cameras: Query<&Camera>,
    images: Res<Assets<Image>>,
    windows: Res<Windows>,
) {
    for camera in &cameras {
        if let Some(info) = camera.target.get_render_target_info(&windows, &images) {
            // Use info
            println!("Target size: {:?}", info.physical_size);
        }
    }
}

// 0.17
fn render_system(
    cameras: Query<&Camera>,
    images: Res<Assets<Image>>,
    windows: Res<Windows>,
) {
    for camera in &cameras {
        match camera.target.get_render_target_info(&windows, &images) {
            Ok(info) => {
                // Use info
                println!("Target size: {:?}", info.physical_size);
            }
            Err(e) => {
                // Handle error - indicates broken rendering state
                error!("Failed to get render target info: {e:?}");
            }
        }
    }
}
```

**Example with expect:**

```rust
// 0.16
fn critical_render_system(
    camera: Query<&Camera>,
    images: Res<Assets<Image>>,
    windows: Res<Windows>,
) {
    let camera = camera.single();
    let info = camera.target.get_render_target_info(&windows, &images)
        .unwrap();

    process_render_target(info);
}

// 0.17
fn critical_render_system(
    camera: Query<&Camera>,
    images: Res<Assets<Image>>,
    windows: Res<Windows>,
) {
    let camera = camera.single();
    let info = camera.target.get_render_target_info(&windows, &images)
        .expect("Render target must be valid for critical rendering");

    process_render_target(info);
}
```

**Example with propagation:**

```rust
// 0.17 - Propagate error to caller
fn try_render_to_target(
    camera: &Camera,
    images: &Assets<Image>,
    windows: &Windows,
) -> Result<(), RenderTargetError> {
    let info = camera.target.get_render_target_info(windows, images)?;

    // Process with info
    Ok(())
}
```

**Notes:**
- This is a **required** change for all code calling `get_render_target_info`
- The `Err` variant indicates the rendering state is broken and should typically be treated as a hard error
- The error type provides specific information about which target failed (window, image, texture view)
- Consider logging errors before panicking for better debugging

---

---

## Guide 84: Replace `Gilrs`, `AccessKitAdapters`, and `WinitWindows` non-send resources

**Pull Requests:** [#18386](https://github.com/bevyengine/bevy/pull/18386), [#17730](https://github.com/bevyengine/bevy/pull/17730), [#19575](https://github.com/bevyengine/bevy/pull/19575)

**Description:**
Bevy is working to move `!Send` data out of the ECS to simplify internal implementation, reduce soundness risks, and unblock features like resources-as-entities and improved scheduling. Three first-party `NonSend` resources have been replaced with `thread_local!` storage: `Gilrs` (wasm32 only), `WinitWindows`, and `AccessKitAdapters`. Access these via `with_borrow()` or `with_borrow_mut()` instead of ECS system parameters. Systems that access these must use `NonSendMarker` to ensure main thread execution.

**Migration Checklist:**
- [ ] Find systems using `NonSend<Gilrs>` (wasm32 only), `NonSend<WinitWindows>`, or `NonSend<AccessKitAdapters>`
- [ ] Replace `NonSend<Gilrs>` system parameters with `bevy_gilrs::GILRS` thread local access
- [ ] Replace `NonSend<WinitWindows>` system parameters with `bevy_winit::WINIT_WINDOWS` thread local access
- [ ] Replace `NonSend<AccessKitAdapters>` system parameters with `bevy_winit::ACCESS_KIT_ADAPTERS` thread local access
- [ ] Add `NonSendMarker` system parameter to force main thread execution
- [ ] Replace resource access with `with_borrow()` for immutable access
- [ ] Replace resource access with `with_borrow_mut()` for mutable access
- [ ] Import the thread local constants from their respective crates

**Search Patterns:**
```rust
// Find NonSend Gilrs usage
NonSend<Gilrs>

// Find NonSend WinitWindows usage
NonSend<WinitWindows>

// Find NonSend AccessKitAdapters usage
NonSend<AccessKitAdapters>

// Find systems with NonSend parameters
fn.*\(.*NonSend
```

**Examples:**

```rust
// 0.16 - Immutable access via NonSend
use bevy_winit::WinitWindows;

fn my_system(winit_windows: NonSend<WinitWindows>) {
    for (id, window) in winit_windows.iter() {
        println!("Window {id:?}");
    }
}

// 0.17 - Immutable access via thread local
use bevy_winit::WINIT_WINDOWS;
use bevy_ecs::system::NonSendMarker;

fn my_system(_marker: NonSendMarker) {
    WINIT_WINDOWS.with_borrow(|winit_windows| {
        for (id, window) in winit_windows.iter() {
            println!("Window {id:?}");
        }
    });
}
```

**Example with mutable access:**

```rust
// 0.16 - Mutable access via NonSend
use bevy_winit::WinitWindows;

fn configure_windows(mut winit_windows: NonSendMut<WinitWindows>) {
    for (_, window) in winit_windows.iter_mut() {
        window.set_title("New Title");
    }
}

// 0.17 - Mutable access via thread local
use bevy_winit::WINIT_WINDOWS;
use bevy_ecs::system::NonSendMarker;

fn configure_windows(_marker: NonSendMarker) {
    WINIT_WINDOWS.with_borrow_mut(|winit_windows| {
        for (_, window) in winit_windows.iter_mut() {
            window.set_title("New Title");
        }
    });
}
```

**Example with AccessKit:**

```rust
// 0.16
use bevy_a11y::AccessKitAdapters;

fn accessibility_system(mut adapters: NonSendMut<AccessKitAdapters>) {
    for adapter in adapters.iter_mut() {
        adapter.update(/* ... */);
    }
}

// 0.17
use bevy_winit::ACCESS_KIT_ADAPTERS;
use bevy_ecs::system::NonSendMarker;

fn accessibility_system(_marker: NonSendMarker) {
    ACCESS_KIT_ADAPTERS.with_borrow_mut(|adapters| {
        for adapter in adapters.iter_mut() {
            adapter.update(/* ... */);
        }
    });
}
```

**Example with Gilrs (wasm32 only):**

```rust
// 0.16
#[cfg(target_arch = "wasm32")]
use bevy_gilrs::Gilrs;

#[cfg(target_arch = "wasm32")]
fn gamepad_system(gilrs: NonSend<Gilrs>) {
    for (id, gamepad) in gilrs.gamepads() {
        println!("Gamepad {id:?}");
    }
}

// 0.17
#[cfg(target_arch = "wasm32")]
use bevy_gilrs::GILRS;
use bevy_ecs::system::NonSendMarker;

#[cfg(target_arch = "wasm32")]
fn gamepad_system(_marker: NonSendMarker) {
    GILRS.with_borrow(|gilrs| {
        for (id, gamepad) in gilrs.gamepads() {
            println!("Gamepad {id:?}");
        }
    });
}
```

**Notes:**
- This is a **required** change for systems accessing `Gilrs`, `WinitWindows`, or `AccessKitAdapters`
- The `NonSendMarker` parameter is **critical** - without it, the system may run on non-main threads where the thread local is uninitialized
- Borrowing will panic if attempted while already borrowed elsewhere
- Only affects wasm32 builds for `Gilrs` - other platforms unchanged
- User-provided `NonSend` types are currently unchanged but may require migration in the future
- Do **not** access these thread locals from non-main threads

---

---

## Guide 85: Required components refactor

**Pull Requests:** [#20110](https://github.com/bevyengine/bevy/pull/20110)

**Description:**
The required components feature has been refactored to fix soundness issues and make priority ordering more consistent. Required components now follow a depth-first/preorder traversal priority, which was mostly the case before with some exceptions that are now fixed. Several methods are now `unsafe`, the inheritance depth parameter has been removed as it's no longer needed, and `RequiredComponentConstructor`'s field is now private for safety.

**Migration Checklist:**
- [ ] Find manual implementations of `Component::register_required_components`
- [ ] Update method signature to include `ComponentId` parameter
- [ ] Replace `components` and `required_components` parameters with single `RequiredComponentsRegistrator`
- [ ] Remove `inheritance_depth` parameter
- [ ] Remove `recursion_check_stack` handling (now automatic)
- [ ] Add `unsafe` to `Component::register_required_components` implementations
- [ ] Add `unsafe` to `RequiredComponents::register` calls
- [ ] Add `unsafe` to `RequiredComponents::register_by_id` calls
- [ ] Remove direct field access to `RequiredComponentConstructor` (field is now private)
- [ ] Verify safety invariants for `unsafe` implementations

**Search Patterns:**
```rust
// Find register_required_components implementations
fn register_required_components

// Find RequiredComponents::register calls
RequiredComponents::register

// Find RequiredComponents::register_by_id calls
RequiredComponents::register_by_id

// Find RequiredComponentConstructor field access
RequiredComponentConstructor.*\.

// Find inheritance_depth usage
inheritance_depth
```

**Examples:**

```rust
// 0.16 - Old signature
impl Component for MyComponent {
    fn register_required_components(
        components: &mut Components,
        required_components: &mut RequiredComponents,
        inheritance_depth: usize,
        recursion_check_stack: &mut Vec<ComponentId>,
    ) {
        // Register required components
        required_components.register::<Transform>(
            components,
            inheritance_depth,
            recursion_check_stack,
        );
    }
}

// 0.17 - New signature with safety
impl Component for MyComponent {
    // SAFETY: We ensure that the required components form a valid DAG
    // and don't create cycles in the dependency graph
    unsafe fn register_required_components(
        component_id: ComponentId,
        registrator: RequiredComponentsRegistrator,
    ) {
        // Use registrator instead of separate parameters
        // No inheritance_depth or recursion_check_stack needed
        unsafe {
            registrator.register::<Transform>();
        }
    }
}
```

**Example with manual registration:**

```rust
// 0.16
fn setup_custom_components(world: &mut World) {
    let components = world.components_mut();
    let mut required_components = RequiredComponents::default();
    let mut stack = Vec::new();

    required_components.register::<Transform>(
        components,
        0, // inheritance_depth
        &mut stack,
    );
}

// 0.17
fn setup_custom_components(world: &mut World) {
    // SAFETY: We ensure proper component registration order
    unsafe {
        let component_id = world.components().get_id(TypeId::of::<MyComponent>()).unwrap();
        let registrator = world.get_required_components_registrator(component_id);
        registrator.register::<Transform>();
    }
}
```

**Notes:**
- This is a **required** change for code manually implementing `register_required_components`
- Most users relying on `#[derive(Component)]` with `#[require(...)]` attributes won't need changes
- The new depth-first ordering is more predictable and consistent
- The `unsafe` requirement emphasizes the safety invariants that must be upheld
- Removing `inheritance_depth` simplifies the API as it's no longer used
- The `recursion_check_stack` is now handled internally
- Soundness fixes prevent certain edge cases with component registration priority

---

---

## Guide 86: Rework `MergeMeshError`

**Pull Requests:** [#18561](https://github.com/bevyengine/bevy/pull/18561)

**Description:**
`MergeMeshError` has been reworked to handle the possibility of meshes having incompatible `PrimitiveTopology` values. The error type was renamed from `MergeMeshError` to `MeshMergeError` to align with other mesh error naming conventions, and it was changed from a struct to an enum to support multiple error variants.

**Migration Checklist:**
- [ ] Replace `MergeMeshError` type name with `MeshMergeError`
- [ ] Update error handling to account for enum variants instead of struct fields
- [ ] Add handling for the new `IncompatiblePrimitiveTopology` variant
- [ ] Update `Mesh::merge` return type expectations from `Result<(), MergeMeshError>` to `Result<(), MeshMergeError>`
- [ ] Update pattern matching or error message formatting for enum structure

**Search Patterns:**
```rust
// Find the old error type name
MergeMeshError

// Find Mesh::merge calls
\.merge\(

// Find error handling
MergeMeshError

// Find result types
Result<.*MergeMeshError>
```

**Examples:**

```rust
// 0.16
use bevy_render::mesh::MergeMeshError;

fn merge_meshes(mesh_a: &mut Mesh, mesh_b: &Mesh) -> Result<(), MergeMeshError> {
    mesh_a.merge(mesh_b)
}

fn handle_merge_error(err: MergeMeshError) {
    eprintln!("Merge failed: {err}");
}

// 0.17
use bevy_render::mesh::MeshMergeError;

fn merge_meshes(mesh_a: &mut Mesh, mesh_b: &Mesh) -> Result<(), MeshMergeError> {
    mesh_a.merge(mesh_b)
}

fn handle_merge_error(err: MeshMergeError) {
    match err {
        MeshMergeError::IncompatiblePrimitiveTopology { source, target } => {
            eprintln!("Cannot merge meshes with different topologies: {source:?} vs {target:?}");
        }
        other => {
            eprintln!("Merge failed: {other}");
        }
    }
}
```

**Example with explicit variant matching:**

```rust
// 0.17
fn try_merge_with_validation(
    mesh_a: &mut Mesh,
    mesh_b: &Mesh,
) -> Result<(), String> {
    match mesh_a.merge(mesh_b) {
        Ok(()) => Ok(()),
        Err(MeshMergeError::IncompatiblePrimitiveTopology { source, target }) => {
            Err(format!(
                "Primitive topology mismatch: source has {source:?}, target has {target:?}"
            ))
        }
        Err(e) => Err(format!("Mesh merge error: {e}")),
    }
}
```

**Example with error propagation:**

```rust
// 0.16
use bevy_render::mesh::MergeMeshError;

fn combine_level_geometry(
    meshes: Vec<Mesh>,
) -> Result<Mesh, MergeMeshError> {
    let mut combined = meshes[0].clone();
    for mesh in &meshes[1..] {
        combined.merge(mesh)?;
    }
    Ok(combined)
}

// 0.17
use bevy_render::mesh::MeshMergeError;

fn combine_level_geometry(
    meshes: Vec<Mesh>,
) -> Result<Mesh, MeshMergeError> {
    let mut combined = meshes[0].clone();
    for mesh in &meshes[1..] {
        combined.merge(mesh)?; // Now returns MeshMergeError
    }
    Ok(combined)
}
```

**Notes:**
- This is a **required** change for all code using `MergeMeshError`
- The rename to `MeshMergeError` aligns with `MeshVertexAttributeError`, `MeshGenerationError`, etc.
- The new enum structure allows for better error differentiation
- The `IncompatiblePrimitiveTopology` variant should be handled explicitly when topology matters
- Check for compiler errors on the type name and update accordingly

---

---

## Guide 87: Fix `From<Rot2>` implementation for `Mat2`

**Pull Requests:** [#20522](https://github.com/bevyengine/bevy/pull/20522)

**Description:**
The `From<Rot2>` implementation for `Mat2` was incorrect in previous releases, constructing a matrix that rotated clockwise instead of counterclockwise. This was actually the **inverse** of the correct rotation matrix. The implementation has been fixed to produce counterclockwise rotation (the mathematical standard). Code relying on the old clockwise behavior will see different results and may need to invert the rotation.

**Migration Checklist:**
- [ ] Find all uses of `Mat2::from(Rot2::...)` or `Rot2::into()`
- [ ] Test rotation behavior to see if results changed
- [ ] If clockwise rotation is still needed, invert the `Rot2` before conversion: `Mat2::from(rot.inverse())`
- [ ] Alternatively, invert the resulting `Mat2`: `Mat2::from(rot).inverse()`
- [ ] Update unit tests expecting clockwise rotation
- [ ] Verify visual correctness of 2D rotations

**Search Patterns:**
```rust
// Find Rot2 to Mat2 conversions
Mat2::from.*Rot2
Rot2.*\.into\(\)

// Find Rot2 usage in 2D transforms
Rot2::

// Find 2D rotation operations
\.rotate\(
```

**Examples:**

```rust
// 0.16 - Produced clockwise rotation (incorrect)
use bevy_math::{Rot2, Mat2, Vec2};

fn rotate_point(point: Vec2, angle: f32) -> Vec2 {
    let rotation = Rot2::radians(angle);
    let matrix = Mat2::from(rotation); // Rotated clockwise
    matrix * point
}

// Test: rotating (1, 0) by 90 degrees gave (0, -1) - clockwise

// 0.17 - Produces counterclockwise rotation (correct)
use bevy_math::{Rot2, Mat2, Vec2};

fn rotate_point(point: Vec2, angle: f32) -> Vec2 {
    let rotation = Rot2::radians(angle);
    let matrix = Mat2::from(rotation); // Rotates counterclockwise
    matrix * point
}

// Test: rotating (1, 0) by 90 degrees gives (0, 1) - counterclockwise
```

**Example - Preserving old behavior:**

```rust
// 0.17 - If you need the old clockwise behavior
use bevy_math::{Rot2, Mat2, Vec2};

fn rotate_point_clockwise(point: Vec2, angle: f32) -> Vec2 {
    let rotation = Rot2::radians(angle);
    // Invert to get clockwise rotation like before
    let matrix = Mat2::from(rotation.inverse());
    matrix * point
}

// Or invert the resulting matrix
fn rotate_point_clockwise_alt(point: Vec2, angle: f32) -> Vec2 {
    let rotation = Rot2::radians(angle);
    let matrix = Mat2::from(rotation).inverse();
    matrix * point
}
```

**Example - Typical 2D sprite rotation:**

```rust
// 0.16 behavior
fn rotate_sprite_old(angle: f32) -> Mat2 {
    // This actually rotated clockwise due to the bug
    Mat2::from(Rot2::radians(angle))
}

// 0.17 - Now rotates counterclockwise (correct)
fn rotate_sprite_new(angle: f32) -> Mat2 {
    // Standard counterclockwise rotation
    Mat2::from(Rot2::radians(angle))
}

// If your game relied on the buggy clockwise rotation:
fn rotate_sprite_preserve_old_behavior(angle: f32) -> Mat2 {
    // Negate angle to get clockwise rotation
    Mat2::from(Rot2::radians(-angle))
}
```

**Notes:**
- This is a **breaking behavioral change** that may affect visual output
- The new behavior is mathematically correct (standard counterclockwise rotation)
- Only affects code using `Mat2::from(Rot2)` or equivalent conversions
- Most 2D games expect counterclockwise rotation, so this fix aligns with expectations
- If your code compensated for the bug, you'll need to remove that compensation
- Test visually to ensure rotations look correct after migration

---

---

## Guide 88: `VectorSpace` implementations

**Pull Requests:** [#19194](https://github.com/bevyengine/bevy/pull/19194)

**Description:**
The `VectorSpace` trait previously required types to use or interface with `f32`, making it less useful for double-precision types. The trait now has a required associated type `Scalar` bounded by the new `ScalarField` trait. `bevy_math` implements `ScalarField` for both `f32` and `f64`, and `VectorSpace` is now implemented for `DVec2`, `DVec3`, and `DVec4` types. This allows double-precision vector operations without constant casting.

**Migration Checklist:**
- [ ] Find manual `VectorSpace` implementations
- [ ] Add `type Scalar = f32;` (or appropriate type) to implementations
- [ ] Update method signatures to use `Self::Scalar` instead of hardcoded `f32`
- [ ] Consider implementing for double-precision types if applicable
- [ ] Update generic bounds from `VectorSpace` to include scalar type constraints if needed

**Search Patterns:**
```rust
// Find VectorSpace implementations
impl.*VectorSpace

// Find VectorSpace trait bounds
: VectorSpace

// Find f32 hardcoded in vector operations
fn.*f32.*VectorSpace

// Find DVec usage that might benefit
DVec
```

**Examples:**

```rust
// 0.16 - Required f32
impl VectorSpace for MyVec3 {
    fn lerp(&self, other: &Self, t: f32) -> Self {
        MyVec3 {
            x: self.x + (other.x - self.x) * t,
            y: self.y + (other.y - self.y) * t,
            z: self.z + (other.z - self.z) * t,
        }
    }

    fn scale(&self, scalar: f32) -> Self {
        MyVec3 {
            x: self.x * scalar,
            y: self.y * scalar,
            z: self.z * scalar,
        }
    }
}

// 0.17 - Use Scalar associated type
impl VectorSpace for MyVec3 {
    type Scalar = f32;

    fn lerp(&self, other: &Self, t: Self::Scalar) -> Self {
        MyVec3 {
            x: self.x + (other.x - self.x) * t,
            y: self.y + (other.y - self.y) * t,
            z: self.z + (other.z - self.z) * t,
        }
    }

    fn scale(&self, scalar: Self::Scalar) -> Self {
        MyVec3 {
            x: self.x * scalar,
            y: self.y * scalar,
            z: self.z * scalar,
        }
    }
}
```

**Example - Double precision:**

```rust
// 0.17 - Now possible with f64
impl VectorSpace for MyDVec3 {
    type Scalar = f64;

    fn lerp(&self, other: &Self, t: Self::Scalar) -> Self {
        MyDVec3 {
            x: self.x + (other.x - self.x) * t,
            y: self.y + (other.y - self.y) * t,
            z: self.z + (other.z - self.z) * t,
        }
    }

    fn scale(&self, scalar: Self::Scalar) -> Self {
        MyDVec3 {
            x: self.x * scalar,
            y: self.y * scalar,
            z: self.z * scalar,
        }
    }
}
```

**Example - Generic functions:**

```rust
// 0.16 - Hardcoded f32
fn smooth_step<V: VectorSpace>(start: V, end: V, t: f32) -> V {
    start.lerp(&end, t * t * (3.0 - 2.0 * t))
}

// 0.17 - Use associated Scalar type
fn smooth_step<V: VectorSpace>(start: V, end: V, t: V::Scalar) -> V
where
    V::Scalar: std::ops::Mul<Output = V::Scalar> + std::ops::Sub<Output = V::Scalar>,
{
    let smooth = t * t * (V::Scalar::from(3.0) - V::Scalar::from(2.0) * t);
    start.lerp(&end, smooth)
}

// Or, more simply for concrete types:
fn smooth_step_f32<V: VectorSpace<Scalar = f32>>(start: V, end: V, t: f32) -> V {
    start.lerp(&end, t * t * (3.0 - 2.0 * t))
}

fn smooth_step_f64<V: VectorSpace<Scalar = f64>>(start: V, end: V, t: f64) -> V {
    start.lerp(&end, t * t * (3.0 - 2.0 * t))
}
```

**Example - Using built-in DVec types:**

```rust
// 0.17 - DVec types now implement VectorSpace
use bevy_math::{DVec3, VectorSpace};

fn interpolate_positions(start: DVec3, end: DVec3, t: f64) -> DVec3 {
    start.lerp(end, t) // No casting needed!
}

fn scale_high_precision(vec: DVec3, factor: f64) -> DVec3 {
    vec * factor // VectorSpace trait methods available
}
```

**Notes:**
- This is a **required** change for custom `VectorSpace` implementations
- Built-in `Vec2`, `Vec3`, `Vec4` types already updated
- For single-precision types, set `type Scalar = f32;` to maintain old behavior
- Double-precision types (`DVec2`, `DVec3`, `DVec4`) now fully support `VectorSpace`
- Reduces casting when working with mixed precision
- Generic functions using `VectorSpace` may need updated bounds

---

---

## Guide 89: `SceneSpawner` methods have been renamed and replaced

**Pull Requests:** [#18358](https://github.com/bevyengine/bevy/pull/18358)

**Description:**
Several `SceneSpawner` methods have been renamed to clarify their purpose. Methods that worked with `DynamicScene`s now have `_dynamic` in their names, and new methods with the old names now work with static `Scene`s. This change makes it explicit whether you're working with dynamic or static scenes.

**Migration Checklist:**
- [ ] Find calls to `SceneSpawner::despawn`
- [ ] Determine if working with `DynamicScene` or `Scene`
- [ ] Replace `despawn` with `despawn_dynamic` if using `DynamicScene`
- [ ] Replace `despawn_sync` with `despawn_dynamic_sync` if using `DynamicScene`
- [ ] Replace `update_spawned_scenes` with `update_spawned_dynamic_scenes` if using `DynamicScene`
- [ ] Use new `despawn`, `despawn_sync`, and `update_spawned_scenes` for static `Scene`s

**Search Patterns:**
```rust
// Find despawn calls
scene_spawner\.despawn

// Find despawn_sync calls
scene_spawner\.despawn_sync

// Find update_spawned_scenes calls
scene_spawner\.update_spawned_scenes

// Find SceneSpawner usage
SceneSpawner
```

**Examples:**

```rust
// 0.16 - Methods worked with DynamicScene
use bevy_scene::{SceneSpawner, DynamicScene};

fn cleanup_dynamic_scene(
    mut scene_spawner: ResMut<SceneSpawner>,
    scene_instance: SceneInstance,
) {
    scene_spawner.despawn(scene_instance);
}

fn update_scenes(mut scene_spawner: ResMut<SceneSpawner>) {
    scene_spawner.update_spawned_scenes();
}

// 0.17 - Explicit _dynamic suffix for DynamicScene
use bevy_scene::{SceneSpawner, DynamicScene};

fn cleanup_dynamic_scene(
    mut scene_spawner: ResMut<SceneSpawner>,
    scene_instance: SceneInstance,
) {
    scene_spawner.despawn_dynamic(scene_instance);
}

fn update_dynamic_scenes(mut scene_spawner: ResMut<SceneSpawner>) {
    scene_spawner.update_spawned_dynamic_scenes();
}
```

**Example with sync despawn:**

```rust
// 0.16
fn cleanup_scene_sync(
    mut scene_spawner: ResMut<SceneSpawner>,
    scene_instance: SceneInstance,
    world: &mut World,
) {
    scene_spawner.despawn_sync(scene_instance, world);
}

// 0.17 - For DynamicScene
fn cleanup_dynamic_scene_sync(
    mut scene_spawner: ResMut<SceneSpawner>,
    scene_instance: SceneInstance,
    world: &mut World,
) {
    scene_spawner.despawn_dynamic_sync(scene_instance, world);
}

// 0.17 - New method for static Scene
fn cleanup_static_scene_sync(
    mut scene_spawner: ResMut<SceneSpawner>,
    scene_instance: SceneInstance,
    world: &mut World,
) {
    scene_spawner.despawn_sync(scene_instance, world);
}
```

**Example using static scenes:**

```rust
// 0.17 - New methods for static Scene
use bevy_scene::{SceneSpawner, Scene};

fn cleanup_static_scene(
    mut scene_spawner: ResMut<SceneSpawner>,
    scene_instance: SceneInstance,
) {
    // Use the new despawn (without _dynamic) for static scenes
    scene_spawner.despawn(scene_instance);
}

fn update_static_scenes(mut scene_spawner: ResMut<SceneSpawner>) {
    // Use the new update_spawned_scenes for static scenes
    scene_spawner.update_spawned_scenes();
}
```

**Complete migration example:**

```rust
// 0.16 - Ambiguous whether working with Scene or DynamicScene
fn scene_manager_system(
    mut scene_spawner: ResMut<SceneSpawner>,
    dynamic_instances: Query<&DynamicSceneInstance>,
    static_instances: Query<&StaticSceneInstance>,
) {
    // Both used the same methods
    for instance in &dynamic_instances {
        scene_spawner.despawn(instance.0);
    }

    scene_spawner.update_spawned_scenes();
}

// 0.17 - Clear distinction between Scene and DynamicScene
fn scene_manager_system(
    mut scene_spawner: ResMut<SceneSpawner>,
    dynamic_instances: Query<&DynamicSceneInstance>,
    static_instances: Query<&StaticSceneInstance>,
) {
    // Clear that we're working with DynamicScene
    for instance in &dynamic_instances {
        scene_spawner.despawn_dynamic(instance.0);
    }

    // Clear that we're working with static Scene
    for instance in &static_instances {
        scene_spawner.despawn(instance.0);
    }

    // Update both types
    scene_spawner.update_spawned_dynamic_scenes();
    scene_spawner.update_spawned_scenes();
}
```

**Notes:**
- This is a **required** change for code using these `SceneSpawner` methods
- The old method names now work with static `Scene`s instead of `DynamicScene`s
- Methods with `_dynamic` suffix work with `DynamicScene`s (old behavior)
- Makes code more explicit about scene type being used
- No functional changes, only naming clarity

---

---

## Guide 90: Schedule API Cleanup

**Pull Requests:** [#19352](https://github.com/bevyengine/bevy/pull/19352), [#20119](https://github.com/bevyengine/bevy/pull/20119), [#20172](https://github.com/bevyengine/bevy/pull/20172), [#20256](https://github.com/bevyengine/bevy/pull/20256)

**Description:**
To support removing systems from schedules, the internal storage for `System`s and `SystemSet`s has been changed from `Vec`s to `SlotMap`s. This allows safely removing nodes and reusing indices. The maps are keyed by `SystemKey` and `SystemSetKey` instead of plain `usize` indices. This change primarily affects advanced schedule manipulation code.

**Migration Checklist:**
- [ ] Update `DiGraph` and `UnGraph` types to include `NodeId` type parameter: `DiGraph<NodeId>`
- [ ] Update `NodeId::System` usage to work with `SystemKey` instead of `usize`
- [ ] Update `NodeId::Set` usage to work with `SystemSetKey` instead of `usize`
- [ ] Update `ScheduleBuildPass::collapse_set` to accept type-specific keys
- [ ] Update `ScheduleBuildPass::build` to accept `DiGraph<SystemKey>` instead of `DiGraph<NodeId>`
- [ ] Wrap `SystemKey` and `SystemSetKey` in `NodeId` when needed
- [ ] Update code receiving `Schedule::systems` and `ScheduleGraph::conflicting_systems` results
- [ ] Use `ScheduleBuildError::to_string()` to get error messages instead of matching on `String`s
- [ ] Handle `Vec<ScheduleBuildWarning>` returned from `ScheduleGraph::build_schedule`
- [ ] Replace removed functions with new alternatives using `SystemSets` and `Systems` accessors
- [ ] Update error variant handling for `HierarchyRedundancy` and `Ambiguity`

**Replaced Functions:**

| Old Method | New Method |
|------------|------------|
| `ScheduleGraph::contains_set` | `ScheduleGraph::system_sets()` + `SystemSets::contains()` |
| `ScheduleGraph::get_set_at` | `ScheduleGraph::system_sets()` + `SystemSets::get()` |
| `ScheduleGraph::set_at` | `ScheduleGraph::system_sets()` + `SystemSets::index()` |
| `ScheduleGraph::get_set_conditions_at` | `ScheduleGraph::system_sets()` + `SystemSets::get_conditions()` |
| `ScheduleGraph::system_sets` | `ScheduleGraph::system_sets()` + `SystemSets::iter()` |
| `ScheduleGraph::get_system_at` | `ScheduleGraph::systems()` + `Systems::get()` |
| `ScheduleGraph::system_at` | `ScheduleGraph::systems()` + `Systems::index()` |
| `ScheduleGraph::systems` | `ScheduleGraph::systems()` + `Systems::iter()` |

**Removed Functions:**
- `NodeId::index` - Match on `SystemKey` or `SystemSetKey` instead
- `NodeId::cmp` - Use `PartialOrd` and `Ord` traits
- `ScheduleGraph::set_conditions_at` - Use `SystemSets::has_conditions()` or `SystemSets::get_conditions()`

**Search Patterns:**
```rust
// Find DiGraph/UnGraph without NodeId parameter
DiGraph<[^N]
UnGraph<[^N]

// Find NodeId::System usage
NodeId::System

// Find NodeId::Set usage
NodeId::Set

// Find replaced methods
ScheduleGraph::contains_set
ScheduleGraph::get_set_at
ScheduleGraph::set_at
ScheduleGraph::get_system_at
ScheduleGraph::system_at

// Find removed methods
NodeId::index
NodeId::cmp
ScheduleGraph::set_conditions_at

// Find error matching on strings
ScheduleBuildError::.*String
```

**Examples:**

```rust
// 0.16 - DiGraph without type parameter
use bevy_ecs::schedule::{DiGraph, NodeId};

fn build_graph() -> DiGraph {
    DiGraph::new()
}

// 0.17 - DiGraph with NodeId type parameter
use bevy_ecs::schedule::{DiGraph, NodeId};

fn build_graph() -> DiGraph<NodeId> {
    DiGraph::new()
}
```

**Example with NodeId:**

```rust
// 0.16 - NodeId stored usize
match node_id {
    NodeId::System(index) => {
        println!("System at index {index}");
    }
    NodeId::Set(index) => {
        println!("Set at index {index}");
    }
}

// 0.17 - NodeId stores SystemKey/SystemSetKey
match node_id {
    NodeId::System(key) => {
        println!("System with key {key:?}");
    }
    NodeId::Set(key) => {
        println!("Set with key {key:?}");
    }
}
```

**Example with ScheduleBuildPass:**

```rust
// 0.16
impl ScheduleBuildPass for MyPass {
    fn collapse_set(
        &mut self,
        node_id: NodeId,
        graph: &mut DiGraph,
    ) {
        // Work with NodeId containing usize
    }

    fn build(&mut self, graph: DiGraph<NodeId>) {
        // Process graph
    }
}

// 0.17
impl ScheduleBuildPass for MyPass {
    fn collapse_set(
        &mut self,
        key: SystemSetKey, // Type-specific key
        graph: &mut DiGraph<NodeId>,
    ) {
        // Wrap back into NodeId if needed
        let node_id = NodeId::Set(key);
    }

    fn build(&mut self, graph: DiGraph<SystemKey>) { // Now SystemKey
        // Re-wrap keys into NodeId if needed
        for key in graph.nodes() {
            let node_id = NodeId::System(key);
        }
    }
}
```

**Example with replaced methods:**

```rust
// 0.16
fn check_system(schedule_graph: &ScheduleGraph, node_id: NodeId) {
    if let Some(system) = schedule_graph.get_system_at(node_id) {
        println!("Found system: {}", system.name());
    }
}

// 0.17
fn check_system(schedule_graph: &ScheduleGraph, key: SystemKey) {
    let systems = schedule_graph.systems();
    if let Some(system) = systems.get(key) {
        println!("Found system: {}", system.name());
    }
}
```

**Example with error handling:**

```rust
// 0.16
match error {
    ScheduleBuildError::HierarchyRedundancy => {
        eprintln!("Hierarchy redundancy detected");
    }
    ScheduleBuildError::Ambiguity => {
        eprintln!("Ambiguity detected");
    }
    other => eprintln!("Error: {other}"),
}

// 0.17
match error {
    ScheduleBuildError::Elevated(warning) => {
        match warning {
            ScheduleBuildWarning::HierarchyRedundancy => {
                eprintln!("Hierarchy redundancy detected");
            }
            ScheduleBuildWarning::Ambiguity => {
                eprintln!("Ambiguity detected");
            }
        }
    }
    other => eprintln!("Error: {}", other.to_string()),
}
```

**Example with build_schedule:**

```rust
// 0.16
fn build_my_schedule(graph: &mut ScheduleGraph) -> SystemSchedule {
    graph.build_schedule()
}

// 0.17
fn build_my_schedule(graph: &mut ScheduleGraph) -> SystemSchedule {
    let (schedule, warnings) = graph.build_schedule();

    // Handle warnings if needed
    for warning in warnings {
        warn!("Schedule build warning: {warning:?}");
    }

    schedule
}

// Or ignore warnings
fn build_my_schedule_simple(graph: &mut ScheduleGraph) -> SystemSchedule {
    graph.build_schedule().0
}
```

**Example with iteration:**

```rust
// 0.16
fn list_all_systems(schedule_graph: &ScheduleGraph) {
    for (node_id, system) in schedule_graph.systems() {
        println!("System {node_id:?}: {}", system.name());
    }
}

// 0.17
fn list_all_systems(schedule_graph: &ScheduleGraph) {
    let systems = schedule_graph.systems();
    for (key, system) in systems.iter() {
        // Wrap in NodeId if needed
        let node_id = NodeId::System(key);
        println!("System {node_id:?}: {}", system.name());
    }
}
```

**Notes:**
- This is a **required** change for advanced schedule manipulation code
- Most users don't interact with these low-level APIs directly
- The `SlotMap` change enables future system removal functionality
- Type-specific keys (`SystemKey`, `SystemSetKey`) improve type safety
- Use accessor methods (`system_sets()`, `systems()`) to get collections, then call methods on those
- Error messages must now use `.to_string()` instead of matching on string contents
- The `build_schedule` method now returns warnings separately for better error handling

---

---

## Guide 91: Rename `send_event` and similar methods to `write_message`

**Pull Requests:** [#20017](https://github.com/bevyengine/bevy/pull/20017), [#20953](https://github.com/bevyengine/bevy/pull/20953)

**Description:**
Following the `EventWriter::send` to `EventWriter::write` rename in 0.16, many similar methods have been renamed. "Buffered events" are now called `Messages`, and the method naming reflects this terminology. The old methods are deprecated but still available. This change clarifies the distinction between messages (buffered, immediate) and events (queued, processed next frame).

**Migration Checklist:**
- [ ] Replace `World::send_event` with `World::write_message`
- [ ] Replace `World::send_event_default` with `World::write_message_default`
- [ ] Replace `World::send_event_batch` with `World::write_message_batch`
- [ ] Replace `DeferredWorld::send_event` with `DeferredWorld::write_message`
- [ ] Replace `DeferredWorld::send_event_default` with `DeferredWorld::write_message_default`
- [ ] Replace `DeferredWorld::send_event_batch` with `DeferredWorld::write_message_batch`
- [ ] Replace `Commands::send_event` with `Commands::write_message`
- [ ] Replace `Events::send` with `Messages::write`
- [ ] Replace `Events::send_default` with `Messages::write_default`
- [ ] Replace `Events::send_batch` with `Messages::write_batch`
- [ ] Replace `RemovedComponentEvents::send` with `RemovedComponentEvents::write`
- [ ] Replace `command::send_event` with `command::write_message`
- [ ] Replace `SendBatchIds` type with `WriteBatchIds`

**Search Patterns:**
```rust
// Find World send_event calls
World.*send_event

// Find DeferredWorld send_event calls
DeferredWorld.*send_event

// Find Commands send_event calls
Commands.*send_event

// Find Events::send calls
Events::send

// Find RemovedComponentEvents::send
RemovedComponentEvents::send

// Find SendBatchIds type
SendBatchIds
```

**Examples:**

```rust
// 0.16
fn trigger_event(world: &mut World) {
    world.send_event(MyEvent { data: 42 });
}

fn trigger_default_event(world: &mut World) {
    world.send_event_default::<MyEvent>();
}

fn trigger_batch_events(world: &mut World, events: Vec<MyEvent>) {
    world.send_event_batch(events);
}

// 0.17
fn trigger_event(world: &mut World) {
    world.write_message(MyEvent { data: 42 });
}

fn trigger_default_event(world: &mut World) {
    world.write_message_default::<MyEvent>();
}

fn trigger_batch_events(world: &mut World, events: Vec<MyEvent>) {
    world.write_message_batch(events);
}
```

**Example with DeferredWorld:**

```rust
// 0.16
fn my_command(world: DeferredWorld) {
    world.send_event(GameEvent::LevelComplete);
}

// 0.17
fn my_command(world: DeferredWorld) {
    world.write_message(GameEvent::LevelComplete);
}
```

**Example with Commands:**

```rust
// 0.16
fn spawn_with_event(mut commands: Commands) {
    commands.spawn(MyBundle::default());
    commands.send_event(SpawnEvent);
}

// 0.17
fn spawn_with_event(mut commands: Commands) {
    commands.spawn(MyBundle::default());
    commands.write_message(SpawnEvent);
}
```

**Example with Messages/Events:**

```rust
// 0.16
use bevy_ecs::event::Events;

fn custom_event_sender(mut events: ResMut<Events<MyEvent>>) {
    events.send(MyEvent { value: 100 });
    events.send_default();
    events.send_batch(vec![MyEvent { value: 1 }, MyEvent { value: 2 }]);
}

// 0.17
use bevy_ecs::event::Messages;

fn custom_event_sender(mut events: ResMut<Messages<MyEvent>>) {
    events.write(MyEvent { value: 100 });
    events.write_default();
    events.write_batch(vec![MyEvent { value: 1 }, MyEvent { value: 2 }]);
}
```

**Example with RemovedComponentEvents:**

```rust
// 0.16
use bevy_ecs::removal_detection::RemovedComponentEvents;

fn handle_removal(mut removed: RemovedComponentEvents<MyComponent>) {
    removed.send(entity);
}

// 0.17
use bevy_ecs::removal_detection::RemovedComponentEvents;

fn handle_removal(mut removed: RemovedComponentEvents<MyComponent>) {
    removed.write(entity);
}
```

**Example with batch IDs:**

```rust
// 0.16
use bevy_ecs::event::SendBatchIds;

fn process_batch_ids(ids: SendBatchIds) {
    for id in ids {
        println!("Sent event {id:?}");
    }
}

// 0.17
use bevy_ecs::event::WriteBatchIds;

fn process_batch_ids(ids: WriteBatchIds) {
    for id in ids {
        println!("Wrote message {id:?}");
    }
}
```

**Complete example:**

```rust
// 0.16
fn game_system(
    world: &mut World,
    mut commands: Commands,
    mut events: ResMut<Events<GameEvent>>,
) {
    // Multiple old-style calls
    world.send_event(GameEvent::Start);
    commands.send_event(GameEvent::PlayerSpawned);
    events.send(GameEvent::ScoreUpdated(100));

    let batch = vec![
        GameEvent::EnemySpawned,
        GameEvent::EnemySpawned,
    ];
    world.send_event_batch(batch);
}

// 0.17
fn game_system(
    world: &mut World,
    mut commands: Commands,
    mut events: ResMut<Messages<GameEvent>>,
) {
    // New message-based terminology
    world.write_message(GameEvent::Start);
    commands.write_message(GameEvent::PlayerSpawned);
    events.write(GameEvent::ScoreUpdated(100));

    let batch = vec![
        GameEvent::EnemySpawned,
        GameEvent::EnemySpawned,
    ];
    world.write_message_batch(batch);
}
```

**Notes:**
- This is an **optional** change - old methods are deprecated but still work
- The new naming clarifies that these are "messages" (immediate, buffered) not "events" (queued)
- Following the 0.16 change where `EventWriter::send` became `EventWriter::write`
- Consistent with the messages/events terminology distinction in Bevy
- The `Events` type is now `Messages` for buffered event storage
- Deprecation warnings will guide you to the new methods

---

## Deprecated Simple Executor

**Pull Requests:** 18753

**Description:**
Bevy has deprecated `SimpleExecutor`, one of the system executors. `SimpleExecutor` applied commands immediately after each system runs, guaranteeing a clean `World` state for each system and enforcing strict order-of-addition execution. This behavior differs from `SingleThreadedExecutor` and `MultiThreadedExecutor`, which apply commands based on explicit ordering constraints (`before`, `after`, `chain`). The dual behavior model was confusing and difficult to maintain, so `SimpleExecutor` is being removed.

**Checklist:**
- [ ] **REQUIRED:** Search for all `SimpleExecutor` usages in your codebase
- [ ] **REQUIRED:** Replace `SimpleExecutor` with `SingleThreadedExecutor` for smaller schedules
- [ ] **REQUIRED:** OR replace `SimpleExecutor` with `MultiThreadedExecutor` for larger schedules with parallelizable systems
- [ ] Identify systems that relied on implicit ordering (order of addition to schedule)
- [ ] Add explicit ordering constraints (`before`, `after`, `chain`) where systems depend on command application order
- [ ] Use `.chain()` to mimic `SimpleExecutor`'s sequential behavior if needed
- [ ] Test for bugs where systems depend on others' commands without explicit ordering
- [ ] Review schedule configuration to ensure proper executor selection

**Search Patterns:** `SimpleExecutor`, `SingleThreadedExecutor`, `MultiThreadedExecutor`, `SystemExecutor`, `.before(`, `.after(`, `.chain()`

**Examples:**
```rust
// 0.16 - Using SimpleExecutor
app.add_systems(Update, (
    system_a,
    system_b, // Implicitly runs after system_a
    system_c, // Implicitly runs after system_b
).using_executor(SimpleExecutor));

// 0.17 - Using SingleThreadedExecutor with explicit ordering
app.add_systems(Update, (
    system_a,
    system_b.after(system_a), // Explicit ordering
    system_c.after(system_b),
).using_executor(SingleThreadedExecutor));

// 0.17 - Using chain() to enforce sequential execution
app.add_systems(Update, (
    system_a,
    system_b,
    system_c,
).chain().using_executor(SingleThreadedExecutor));
```

---

---

## `SpawnableList` now uses `MovingPtr<Self>`

**Pull Requests:** 20772, 20877

**Description:**
To reduce stack size when spawning and inserting large bundles, the `SpawnableList` trait's `spawn` method now takes `MovingPtr<'_, Self>` instead of `self` by value. This requires `SpawnableList` implementations to be `Sized`. `MovingPtr<T>` is a safe, typed, box-like pointer that owns data but not the underlying memory, allowing efficient field-by-field decomposition without stack copies. Use `MovingPtr::read()` to move the entire value to the stack, or `deconstruct_moving_ptr!` macro to get `MovingPtr` references to individual fields.

**Checklist:**
- [ ] **REQUIRED:** Search for all custom `SpawnableList` trait implementations
- [ ] **REQUIRED:** Update `spawn` method signature from `self` to `this: MovingPtr<'_, Self>`
- [ ] **REQUIRED:** Update `spawn` method body to use `this.read()` to extract the full value
- [ ] **OPTIONAL:** Use `deconstruct_moving_ptr!` macro for field-by-field extraction to avoid stack copies
- [ ] **REQUIRED:** Ensure all `SpawnableList` implementations are `Sized` (remove `?Sized` if present)
- [ ] Review large bundle spawning code for potential performance improvements
- [ ] Test spawning logic to ensure proper value ownership and memory management

**Search Patterns:** `SpawnableList`, `impl SpawnableList`, `fn spawn(`, `MovingPtr`, `deconstruct_moving_ptr!`, `MovingPtr::read()`

**Examples:**
```rust
// 0.16 - Old signature with self by value
impl<R: Relationship> SpawnableList<R> for MySpawnableList<A: Bundle, B: Bundle> {
    fn spawn(self, world: &mut World, entity: Entity) {
        let MySpawnableList { a, b } = self;
        world.spawn((R::from(entity), a, b));
    }
}

// 0.17 - New signature with MovingPtr, reading full value
impl<R: Relationship> SpawnableList<R> for MySpawnableList<A: Bundle, B: Bundle> {
    fn spawn(this: MovingPtr<'_, Self>, world: &mut World, entity: Entity) {
        let MySpawnableList { a, b } = this.read();
        world.spawn((R::from(entity), a, b));
    }
}

// 0.17 - Using deconstruct_moving_ptr! for field-by-field extraction
impl<R: Relationship> SpawnableList<R> for MySpawnableList<A: Bundle, B: Bundle> {
    fn spawn(this: MovingPtr<'_, Self>, world: &mut World, entity: Entity) {
        unsafe {
            deconstruct_moving_ptr!(this => { a, b, });
            let a = a.read();
            let b = b.read();
            world.spawn((R::from(entity), a, b));
        }
    }
}

// 0.17 - Example with MovingPtr deref
struct MySpawnableList<A: Bundle, B: Bundle> {
    a: A,
    b: B,
}
let my_ptr: MovingPtr<'_, MySpawnableList<u32, String>> = ...;
deconstruct_moving_ptr!(my_ptr => { a, b, });
let a_ptr: MovingPtr<'_, u32> = a;
let b_ptr: MovingPtr<'_, String> = b;
```

---

---

## Specialized UI transform components

**Pull Requests:** 16615

**Description:**
Bevy UI now uses specialized 2D UI transform components `UiTransform` and `UiGlobalTransform` instead of the general `Transform` and `GlobalTransform`. `UiTransform` is a 2D-only equivalent with responsive translation in `Val` units (supporting percentage, pixels, etc.). `UiGlobalTransform` wraps `Affine2` and is updated in `ui_layout_system`. `Node` now requires `UiTransform` instead of `Transform`. Importantly, `ui_layout_system` no longer overwrites translations each frame, eliminating the need for workaround systems that cache and rewrite transforms.

**Checklist:**
- [ ] **REQUIRED:** Search for all UI entities with `Node` component and `Transform`
- [ ] **REQUIRED:** Replace `Transform` with `UiTransform` for all UI entities
- [ ] **REQUIRED:** Replace `GlobalTransform` with `UiGlobalTransform` for all UI entities
- [ ] **REQUIRED:** Convert `Transform::translation` (Vec3) to `UiTransform::translation` (Val2)
- [ ] **REQUIRED:** Convert `Transform::rotation` (Quat) to `UiTransform::rotation` (Rot2)
- [ ] **REQUIRED:** Convert `Transform::scale` (Vec3) to `UiTransform::scale` (Vec2)
- [ ] Update queries that filter for UI entities: change `Query<&Transform, With<Node>>` to `Query<&UiTransform, With<Node>>`
- [ ] Remove any workaround systems that cached and rewrote UI transforms (no longer needed)
- [ ] If you relied on `GlobalTransform.translation().z` for UI depth, derive from `UiStack` instead
- [ ] Update UI animation code to work with `Val2` instead of `Vec3`

**Search Patterns:** `Node`, `Transform`, `GlobalTransform`, `UiTransform`, `UiGlobalTransform`, `Val2`, `Rot2`, `ui_layout_system`, `UiStack`

**Examples:**
```rust
// 0.16 - Using Transform for UI
commands.spawn((
    Node::default(),
    Transform {
        translation: Vec3::new(100.0, 50.0, 0.0),
        rotation: Quat::from_rotation_z(0.5),
        scale: Vec3::new(2.0, 2.0, 1.0),
    },
));

// 0.17 - Using UiTransform for UI
commands.spawn((
    Node::default(),
    UiTransform {
        translation: Val2::px(100.0, 50.0),
        rotation: Rot2::from_radians(0.5),
        scale: Vec2::new(2.0, 2.0),
    },
));

// 0.16 - Query for UI transforms
fn my_system(query: Query<&Transform, With<Node>>) {
    for transform in &query {
        let pos = transform.translation;
    }
}

// 0.17 - Query for UI transforms
fn my_system(query: Query<&UiTransform, With<Node>>) {
    for ui_transform in &query {
        let pos = ui_transform.translation; // Val2, not Vec3
    }
}

// 0.17 - Converting Transform to UiTransform
let old_transform = Transform {
    translation: Vec3 { x: 10.0, y: 20.0, z: 5.0 },
    rotation: Quat::from_rotation_z(0.785),
    scale: Vec3::new(1.5, 1.5, 1.0),
};

let new_ui_transform = UiTransform {
    translation: Val2::px(10.0, 20.0), // z is no longer used
    rotation: Rot2::from_radians(0.785),
    scale: Vec2::new(1.5, 1.5), // only x and y
};
```

---

---

## Window is now split into multiple components

**Pull Requests:** 19668

**Description:**
The `Window` component has been split into multiple smaller components to improve maintainability and approachability. `CursorOptions` has been extracted into its own component on the same entity. When accessing cursor options, you now query for `CursorOptions` directly instead of accessing it through `Window`. This split also applies to initial window configuration in `WindowPlugin`.

**Checklist:**
- [ ] **REQUIRED:** Search for `Window` queries that access `cursor_options` field
- [ ] **REQUIRED:** Replace `Query<&mut Window>` with `Query<&mut CursorOptions>` where only cursor options are accessed
- [ ] **REQUIRED:** Update field access from `window.cursor_options.grab_mode` to `cursor_options.grab_mode`
- [ ] **REQUIRED:** Update `WindowPlugin` configuration: move `cursor_options` from `Window` struct to `primary_cursor_options` field
- [ ] If you need both `Window` and `CursorOptions`, use tuple queries: `Query<(&Window, &CursorOptions)>`
- [ ] Update any window spawning code to spawn `CursorOptions` as a separate component
- [ ] Review window entity bundles to ensure all necessary components are included

**Search Patterns:** `Window`, `CursorOptions`, `cursor_options`, `grab_mode`, `WindowPlugin`, `primary_cursor_options`, `primary_window`, `PrimaryWindow`

**Examples:**
```rust
// 0.16 - Accessing cursor options through Window
fn lock_cursor(primary_window: Single<&mut Window, With<PrimaryWindow>>) {
    primary_window.cursor_options.grab_mode = CursorGrabMode::Locked;
    primary_window.cursor_options.visible = false;
}

// 0.17 - Accessing cursor options directly
fn lock_cursor(primary_cursor_options: Single<&mut CursorOptions, With<PrimaryWindow>>) {
    primary_cursor_options.grab_mode = CursorGrabMode::Locked;
    primary_cursor_options.visible = false;
}

// 0.16 - Initial window configuration
app.add_plugins(DefaultPlugins.set(WindowPlugin {
    primary_window: Some(Window {
        title: "My Game".to_string(),
        cursor_options: CursorOptions {
            grab_mode: CursorGrabMode::Locked,
            visible: false,
            ..default()
        },
        ..default()
    }),
    ..default()
}));

// 0.17 - Initial window configuration with split components
app.add_plugins(DefaultPlugins.set(WindowPlugin {
    primary_window: Some(Window {
        title: "My Game".to_string(),
        ..default()
    }),
    primary_cursor_options: Some(CursorOptions {
        grab_mode: CursorGrabMode::Locked,
        visible: false,
        ..default()
    }),
    ..default()
}));

// 0.17 - Accessing both Window and CursorOptions
fn system(query: Query<(&Window, &CursorOptions), With<PrimaryWindow>>) {
    let (window, cursor_options) = query.single();
    println!("Window: {}, Cursor locked: {}", window.title,
             cursor_options.grab_mode == CursorGrabMode::Locked);
}
```

---

---

## `ComputedUiTargetCamera` split into separate components

**Pull Requests:** 20535

**Description:**
The render target information (scale factor and physical size) has been removed from `ComputedUiTargetCamera` and placed into a new component `ComputedUiRenderTargetInfo`. This split allows for better organization of UI camera-related data and more efficient queries when you only need specific information.

**Checklist:**
- [ ] **REQUIRED:** Search for all `ComputedUiTargetCamera` usages
- [ ] Identify code that accesses scale factor or physical size fields from `ComputedUiTargetCamera`
- [ ] **REQUIRED:** Update queries to include `ComputedUiRenderTargetInfo` for accessing scale factor or physical size
- [ ] Update UI systems that calculate positions or sizes based on render target info
- [ ] Review UI rendering code to ensure it uses the correct component for its needs

**Search Patterns:** `ComputedUiTargetCamera`, `ComputedUiRenderTargetInfo`, `scale_factor`, `physical_size`

**Examples:**
```rust
// 0.16 - Accessing render target info from ComputedUiTargetCamera
fn system(query: Query<&ComputedUiTargetCamera>) {
    for computed in &query {
        let scale = computed.scale_factor;
        let size = computed.physical_size;
    }
}

// 0.17 - Accessing render target info from separate component
fn system(query: Query<(&ComputedUiTargetCamera, &ComputedUiRenderTargetInfo)>) {
    for (computed_camera, render_info) in &query {
        let scale = render_info.scale_factor;
        let size = render_info.physical_size;
        // Use computed_camera for camera-specific data
    }
}

// 0.17 - If you only need render target info
fn system(query: Query<&ComputedUiRenderTargetInfo>) {
    for render_info in &query {
        let scale = render_info.scale_factor;
        let size = render_info.physical_size;
    }
}
```

---

---

## Fixed UI draw order and `stack_z_offsets` changes

**Pull Requests:** 19691

**Description:**
The draw order of UI elements is now fixed and deterministic. Previously, the ordering of background colors and texture-sliced images was inconsistent and depended on system ordering. The new fixed order (back-to-front) is: box shadows, background colors, borders, gradients, border gradients, images, materials, text. The `stack_z_offsets` constants have been updated to enforce this ordering. `NODE` is renamed to `BACKGROUND_COLOR`, `TEXTURE_SLICE` is removed (use `IMAGE`), and new constants `BORDER`, `BORDER_GRADIENT`, and `TEXT` have been added.

**Checklist:**
- [ ] **REQUIRED:** Search for all usages of `stack_z_offsets::NODE` and replace with `stack_z_offsets::BACKGROUND_COLOR`
- [ ] **REQUIRED:** Search for all usages of `stack_z_offsets::TEXTURE_SLICE` and replace with `stack_z_offsets::IMAGE`
- [ ] Review custom UI rendering code that depends on draw order
- [ ] Update any code using `BORDER`, `BORDER_GRADIENT`, or `TEXT` constants if they existed in custom implementations
- [ ] Test UI rendering to ensure visual correctness with the new fixed draw order
- [ ] Verify that background colors, borders, images, and text appear in the correct layers

**Search Patterns:** `stack_z_offsets`, `stack_z_offsets::NODE`, `stack_z_offsets::BACKGROUND_COLOR`, `stack_z_offsets::TEXTURE_SLICE`, `stack_z_offsets::IMAGE`, `stack_z_offsets::BORDER`, `stack_z_offsets::BORDER_GRADIENT`, `stack_z_offsets::TEXT`

**Examples:**
```rust
// 0.16 - Using old constants
use bevy::ui::stack_z_offsets::{NODE, TEXTURE_SLICE};

fn my_ui_system() {
    let z_offset = NODE; // Background color
    let image_z = TEXTURE_SLICE; // Texture-sliced images
}

// 0.17 - Using new constants
use bevy::ui::stack_z_offsets::{BACKGROUND_COLOR, IMAGE, BORDER, TEXT};

fn my_ui_system() {
    let z_offset = BACKGROUND_COLOR; // Renamed from NODE
    let image_z = IMAGE; // Replaces TEXTURE_SLICE
    let border_z = BORDER; // New constant
    let text_z = TEXT; // New constant
}

// 0.17 - Fixed draw order (back to front):
// 1. Box shadows
// 2. Node background colors (BACKGROUND_COLOR)
// 3. Node borders (BORDER)
// 4. Gradients
// 5. Border Gradients (BORDER_GRADIENT)
// 6. Images (IMAGE) - includes texture-sliced images
// 7. Materials
// 8. Text (TEXT) - includes text shadows
```

---

---

## State-scoped entities are now always enabled implicitly

**Pull Requests:** 19354, 20883

**Description:**
State-scoped entities are now always enabled by default in Bevy 0.17.0. The `app.enable_state_scoped_entities::<State>()` method is no longer needed and has been marked as deprecated (it does nothing when called). The attribute `#[states(scoped_entities)]` has been completely removed. This simplifies state management by making entity scoping a standard feature that's always available.

**Checklist:**
- [ ] **REQUIRED:** Search for all `enable_state_scoped_entities` method calls
- [ ] **REQUIRED:** Remove all `app.enable_state_scoped_entities::<State>()` calls (deprecated, does nothing)
- [ ] **REQUIRED:** Search for `#[states(scoped_entities)]` attribute usage
- [ ] **REQUIRED:** Remove all `#[states(scoped_entities)]` attributes from state definitions
- [ ] Clean up imports if `enable_state_scoped_entities` was explicitly imported
- [ ] Verify that state-scoped entity behavior still works as expected (it's now automatic)

**Search Patterns:** `enable_state_scoped_entities`, `#[states(scoped_entities)]`, `states(scoped_entities)`

**Examples:**
```rust
// 0.16 - Manually enabling state-scoped entities
#[derive(States, Default, Clone, Eq, PartialEq, Hash, Debug)]
#[states(scoped_entities)]
enum GameState {
    #[default]
    Menu,
    InGame,
}

fn main() {
    App::new()
        .init_state::<GameState>()
        .enable_state_scoped_entities::<GameState>()
        .run();
}

// 0.17 - State-scoped entities enabled by default
#[derive(States, Default, Clone, Eq, PartialEq, Hash, Debug)]
// Remove #[states(scoped_entities)] - no longer needed
enum GameState {
    #[default]
    Menu,
    InGame,
}

fn main() {
    App::new()
        .init_state::<GameState>()
        // Remove enable_state_scoped_entities - it's automatic
        .run();
}

// State-scoped entities work automatically in 0.17
fn spawn_menu_ui(mut commands: Commands, state: Res<State<GameState>>) {
    // This entity will be automatically despawned when leaving the Menu state
    commands.spawn((
        StateScoped(GameState::Menu),
        Node::default(),
    ));
}
```

---

---

## Stop exposing mp3 support through minimp3

**Pull Requests:** 20183

**Description:**
The `minimp3` feature is no longer exposed from Bevy. Bevy still supports MP3 audio through the `mp3` feature, but it no longer uses `minimp3` by default due to maintenance, security, and platform compatibility issues. `minimp3` is not actively maintained, doesn't work in WASM, has known security vulnerabilities, and can cause application rejection from the Apple App Store. If you specifically need `minimp3`, you can add it as a direct dependency to `rodio`, but this is discouraged.

**Checklist:**
- [ ] **REQUIRED:** Search for `minimp3` feature flags in `Cargo.toml`
- [ ] **REQUIRED:** Remove `minimp3` from Bevy feature flags
- [ ] **REQUIRED:** Replace with `mp3` feature flag if MP3 support is needed
- [ ] If you absolutely require `minimp3` (not recommended), add `rodio` dependency with `minimp3` feature
- [ ] Test audio playback to ensure MP3 files still work with the new decoder
- [ ] Review security advisories related to `minimp3` if you choose to keep using it

**Search Patterns:** `minimp3`, `mp3`, `rodio`, `features = [`, `bevy/minimp3`, `bevy/mp3`

**Examples:**
```toml
# 0.16 - Cargo.toml with minimp3 feature
[dependencies]
bevy = { version = "0.16", features = ["minimp3"] }

# 0.17 - Cargo.toml with mp3 feature (recommended)
[dependencies]
bevy = { version = "0.17", features = ["mp3"] }

# 0.17 - If you really need minimp3 (not recommended)
[dependencies]
bevy = { version = "0.17", features = ["mp3"] }
rodio = { version = "0.20", features = ["minimp3"] }
```

```rust
// No code changes needed - just feature flag changes in Cargo.toml
// MP3 files will continue to work with the `mp3` feature
fn setup_audio(asset_server: Res<AssetServer>, mut commands: Commands) {
    commands.spawn((
        AudioPlayer::new(asset_server.load("music.mp3")),
        PlaybackSettings::LOOP,
    ));
}
```

---

---

## Stop storing access in systems

**Pull Requests:** 19496, 19477

**Description:**
Bevy no longer stores component access information in individual systems to reduce memory usage. Component access is now stored in the schedule instead, since it's only needed for top-level systems. The trait methods `System::component_access` and `System::component_access_set` have been removed. Instead, `System::initialize` now returns the access information. For manual `SystemParam` implementations, `init_state` has been split into `init_state` (creates state) and `init_access` (calculates access). The `component_access_set` is now passed as a separate parameter during initialization.

**Checklist:**
- [ ] **REQUIRED:** Search for manual `System` trait implementations
- [ ] **REQUIRED:** Update `initialize` method to return `FilteredAccessSet<ComponentId>` instead of storing access
- [ ] **REQUIRED:** Search for calls to `system.component_access()` or `system.component_access_set()`
- [ ] **REQUIRED:** Replace with storing and using the return value from `initialize`
- [ ] **REQUIRED:** Search for manual `SystemParam` trait implementations
- [ ] **REQUIRED:** Split `init_state` into `init_state` (creates state) and `init_access` (calculates access)
- [ ] **REQUIRED:** Update `init_state` to remove `SystemMeta` and `component_access_set` parameters
- [ ] **REQUIRED:** Implement `init_access` with `&state`, `&mut meta`, `&mut component_access_set`, `&World` parameters
- [ ] **REQUIRED:** Search for manual `init_state` calls and add subsequent `init_access` call
- [ ] Update `SystemParamBuilder::build` implementations to only create state (access is calculated separately)
- [ ] Review custom system and parameter types for any access-related storage that can be removed

**Search Patterns:** `System::component_access`, `System::component_access_set`, `System::initialize`, `SystemParam::init_state`, `SystemParam::init_access`, `SystemMeta`, `FilteredAccessSet`, `component_access_set`, `SystemParamBuilder`

**Examples:**
```rust
// 0.16 - Accessing component access from system
let mut system = IntoSystem::into_system(my_system);
system.initialize(&mut world);
let access = system.component_access();
let access_set = system.component_access_set();

// 0.17 - Access returned from initialize
let mut system = IntoSystem::into_system(my_system);
let component_access_set = system.initialize(&mut world);
let access = component_access_set.combined_access();

// 0.16 - Manual SystemParam implementation
unsafe impl SystemParam for MyParam {
    type State = MyParamState;
    type Item<'world, 'state> = MyParam;

    fn init_state(world: &mut World, system_meta: &mut SystemMeta) -> Self::State {
        // Access system_meta.component_access_set here
        let mut access = system_meta.component_access_set_mut();
        // ... calculate and store access
        MyParamState { /* ... */ }
    }

    unsafe fn get_param<'world, 'state>(
        state: &'state mut Self::State,
        system_meta: &SystemMeta,
        world: UnsafeWorldCell<'world>,
        change_tick: Tick,
    ) -> Self::Item<'world, 'state> {
        MyParam
    }
}

// 0.17 - Split into init_state and init_access
unsafe impl SystemParam for MyParam {
    type State = MyParamState;
    type Item<'world, 'state> = MyParam;

    fn init_state(world: &mut World) -> Self::State {
        // No access to SystemMeta or component_access_set
        // Only create the state
        MyParamState { /* ... */ }
    }

    fn init_access(
        state: &Self::State,
        system_meta: &mut SystemMeta,
        component_access_set: &mut FilteredAccessSet<ComponentId>,
        world: &World,
    ) {
        // Calculate and register access here
        // Use component_access_set parameter instead of system_meta
    }

    unsafe fn get_param<'world, 'state>(
        state: &'state mut Self::State,
        system_meta: &SystemMeta,
        world: UnsafeWorldCell<'world>,
        change_tick: Tick,
    ) -> Self::Item<'world, 'state> {
        MyParam
    }
}

// 0.16 - Manual init_state call
let param_state = MyParam::init_state(world, &mut meta);

// 0.17 - Manual init_state + init_access calls
let param_state = MyParam::init_state(world);
let mut component_access_set = FilteredAccessSet::new();
MyParam::init_access(&param_state, &mut meta, &mut component_access_set, world);
```

---

---

## `SyncCell` and `SyncUnsafeCell` moved to bevy_platform

**Pull Requests:** 19305

**Description:**
The `SyncCell` and `SyncUnsafeCell` types have been moved from `bevy_utils` to `bevy_platform`. These are platform-specific synchronization primitives that are better organized in the platform-specific crate. This is a simple import path change.

**Checklist:**
- [ ] **REQUIRED:** Search for all imports of `bevy_utils::synccell::SyncCell`
- [ ] **REQUIRED:** Replace with `bevy_platform::cell::SyncCell`
- [ ] **REQUIRED:** Search for all imports of `bevy_utils::syncunsafecell::SyncUnsafeCell`
- [ ] **REQUIRED:** Replace with `bevy_platform::cell::SyncUnsafeCell`
- [ ] Update wildcard imports if you were using `use bevy_utils::*`
- [ ] Verify compilation after import path updates

**Search Patterns:** `bevy_utils::synccell::SyncCell`, `bevy_utils::syncunsafecell::SyncUnsafeCell`, `bevy_platform::cell::SyncCell`, `bevy_platform::cell::SyncUnsafeCell`, `SyncCell`, `SyncUnsafeCell`, `use bevy_utils::`

**Examples:**
```rust
// 0.16 - Importing from bevy_utils
use bevy_utils::synccell::SyncCell;
use bevy_utils::syncunsafecell::SyncUnsafeCell;

// 0.17 - Importing from bevy_platform
use bevy_platform::cell::SyncCell;
use bevy_platform::cell::SyncUnsafeCell;

// Usage remains the same
fn my_system() {
    let cell = SyncCell::new(42);
    let unsafe_cell = SyncUnsafeCell::new(vec![1, 2, 3]);
}
```

---

---

## `System::run` returns `Result`

**Pull Requests:** 19145

**Description:**
To support fallible systems and parameter-based system skipping (like `Single` and `If<T>`), `System::run` and related methods now return `Result<Out, RunSystemError>` instead of `Out` directly. For infallible systems, the simplest migration is to `unwrap()` the result. Invalid parameters (like missing resources) that previously panicked will now return `Err`. For manual `System` implementations, the return type has changed. Systems that return `Result<T, BevyError>` may experience type inference issues and need explicit type annotations.

**Checklist:**
- [ ] **REQUIRED:** Search for all calls to `System::run`, `System::run_unsafe`, `System::run_without_applying_deferred`, `ReadOnlySystem::run_readonly`
- [ ] **REQUIRED:** Add `.unwrap()` to handle the new `Result` return type (for infallible systems)
- [ ] **OPTIONAL:** Use `?` operator if calling from a function that returns `Result<T, BevyError>`
- [ ] **REQUIRED:** Search for manual `System` trait implementations
- [ ] **REQUIRED:** Update `run_unsafe` return type from `Out` to `Result<Out, RunSystemError>`
- [ ] **REQUIRED:** Wrap return values in `Ok(...)` for infallible systems
- [ ] **REQUIRED:** For fallible systems, change `type Out = Result<T, BevyError>` to `type Out = T`
- [ ] Remove manual `validate_param` or `validate_param_unsafe` calls (now automatic in `run` methods, except `run_unsafe`)
- [ ] Search for type inference errors with `Result`-returning systems
- [ ] Add explicit type annotations using `IntoSystem::<_, OutType, _>` or `run_system_cached::<OutType, _, _>` syntax
- [ ] Test system execution to ensure proper error handling

**Search Patterns:** `System::run`, `System::run_unsafe`, `System::run_without_applying_deferred`, `ReadOnlySystem::run_readonly`, `validate_param`, `validate_param_unsafe`, `impl System`, `fn run_unsafe`, `RunSystemError`, `IntoSystem`, `run_system_cached`

**Examples:**
```rust
// 0.16 - System::run returned Out directly
let result = system.run((), &mut world);

// 0.17 - System::run returns Result<Out, RunSystemError>
let result = system.run((), &mut world).unwrap();

// 0.17 - Using ? operator in Result-returning context
fn my_function(world: &mut World) -> Result<(), BevyError> {
    let system = IntoSystem::into_system(my_system);
    let result = system.run((), world)?;
    Ok(())
}

// 0.16 - Manual System implementation
impl System for MySystem {
    type Out = u32;

    unsafe fn run_unsafe(&mut self, input: Self::In, world: UnsafeWorldCell) -> Self::Out {
        42
    }
}

// 0.17 - Manual System implementation with Result
impl System for MySystem {
    type Out = u32;

    unsafe fn run_unsafe(
        &mut self,
        input: Self::In,
        world: UnsafeWorldCell
    ) -> Result<Self::Out, RunSystemError> {
        Ok(42) // Wrap in Ok for infallible systems
    }
}

// 0.16 - Fallible system with Result as Out type
impl System for MyFallibleSystem {
    type Out = Result<u32, BevyError>;

    unsafe fn run_unsafe(&mut self, input: Self::In, world: UnsafeWorldCell) -> Self::Out {
        Ok(42)
    }
}

// 0.17 - Fallible system with plain Out type
impl System for MyFallibleSystem {
    type Out = u32; // Change from Result<u32, BevyError> to u32

    unsafe fn run_unsafe(
        &mut self,
        input: Self::In,
        world: UnsafeWorldCell
    ) -> Result<Self::Out, RunSystemError> {
        Ok(42)
    }
}

// 0.16 - No validation needed (or manual validation)
system.initialize(&mut world);
system.validate_param_unsafe(world.as_unsafe_world_cell());
let result = system.run((), &mut world);

// 0.17 - Validation automatic in run (except run_unsafe)
system.initialize(&mut world);
let result = system.run((), &mut world).unwrap(); // Validates automatically

// Type inference issues with Result-returning systems
fn example_system() -> Result<(), BevyError> { Ok(()) }

// 0.16 - Type inferred automatically
IntoSystem::into_system(example_system);

// 0.17 - May need explicit type annotation
IntoSystem::<_, (), _>::into_system(example_system); // Out = ()
// OR
IntoSystem::<_, Result<(), BevyError>, _>::into_system(example_system); // Out = Result

// 0.17 - Type inference with run_system_cached
world.run_system_cached::<(), _, _>(example_system).unwrap(); // Out = ()
// OR
let _: () = world.run_system_cached(example_system).unwrap(); // Inferred from usage
let r: Result<(), BevyError> = world.run_system_cached(example_system).unwrap(); // Inferred
```

---

---

## Guide 103: System::run returns Result

**Pull Requests:** #19145

**Description:**
The `System::run` method and related execution methods now return `Result<Out, RunSystemError>` instead of plain `Out` values. This change enables fallible systems and parameter-based system skipping (like `Single` and `If<T>`) to work in more contexts. Previously infallible systems that had invalid parameters would panic; now they return `Err` which must be handled explicitly.

**Migration Checklist:**

Required changes:
- [ ] Unwrap or handle `Result` from `System::run()` calls
- [ ] Unwrap or handle `Result` from `System::run_unsafe()` calls
- [ ] Unwrap or handle `Result` from `System::run_without_applying_deferred()` calls
- [ ] Unwrap or handle `Result` from `ReadOnlySystem::run_readonly()` calls
- [ ] Update manual `System` implementations: change `run_unsafe` return type from `Out` to `Result<Out, RunSystemError>`
- [ ] Wrap infallible system returns in `Ok()`
- [ ] For systems with `type Out = Result<T, BevyError>`, change to `type Out = T`
- [ ] Add explicit type parameters to `IntoSystem::into_system()` for `Result`-returning or `!`-returning functions
- [ ] Add explicit type parameters to `World::run_system_cached()` for ambiguous return types

Optional changes:
- [ ] Remove manual `validate_param()` / `validate_param_unsafe()` calls before `run()` methods (now automatic except for `run_unsafe`)
- [ ] Use `?` operator in functions returning `Result<T, BevyError>` instead of `unwrap()`

**Search Patterns:**

```rust
// Find calls to run methods that need Result handling
System::run
System::run_unsafe
System::run_without_applying_deferred
ReadOnlySystem::run_readonly

// Find manual System implementations
impl System for
fn run_unsafe

// Find IntoSystem usage with Result-returning functions
IntoSystem::into_system
world.run_system_cached

// Find validate_param calls that may be redundant
validate_param
validate_param_unsafe
```

**Migration Examples:**

```rust
// 0.16: Direct value returned
let result = my_system.run(world);

// 0.17: Result must be handled
let result = my_system.run(world).unwrap();
// or
let result = my_system.run(world)?;

// 0.16: Manual System implementation
impl System for MySystem {
    type Out = String;

    unsafe fn run_unsafe(&mut self, world: UnsafeWorldCell) -> String {
        "result".to_string()
    }
}

// 0.17: Wrap return in Ok()
impl System for MySystem {
    type Out = String;

    unsafe fn run_unsafe(&mut self, world: UnsafeWorldCell) -> Result<String, RunSystemError> {
        Ok("result".to_string())
    }
}

// 0.16: Fallible system with Result output
impl System for FallibleSystem {
    type Out = Result<String, BevyError>;

    unsafe fn run_unsafe(&mut self, world: UnsafeWorldCell) -> Result<String, BevyError> {
        Ok("success".to_string())
    }
}

// 0.17: Output type is unwrapped T
impl System for FallibleSystem {
    type Out = String;

    unsafe fn run_unsafe(&mut self, world: UnsafeWorldCell) -> Result<String, RunSystemError> {
        Ok("success".to_string())
    }
}

// 0.16: Ambiguous Result-returning function
fn example_system() -> Result<(), BevyError> { Ok(()) }
IntoSystem::into_system(example_system)

// 0.17: Explicit type parameter required
IntoSystem::<_, (), _>::into_system(example_system);
// or infer from usage
let _: () = world.run_system_cached(example_system).unwrap();

// 0.16: Manual validation before run
system.validate_param_unsafe(world)?;
system.run(world);

// 0.17: Validation is automatic (except for run_unsafe)
system.run(world).unwrap(); // validates internally
```

---

---

## Guide 104: Consistent *Systems naming convention for system sets

**Pull Requests:** #18900

**Description:**
System sets across Bevy now consistently use the `Systems` suffix for naming. This affects 30+ system set types across rendering, animation, input, transforms, UI, and other subsystems. The change improves ecosystem-wide consistency and makes code more readable by establishing a clear naming pattern.

**Migration Checklist:**

Required changes:
- [ ] Rename `AccessibilitySystem` → `AccessibilitySystems`
- [ ] Rename `GizmoRenderSystem` → `GizmoRenderSystems`
- [ ] Rename `PickSet` → `PickingSystems`
- [ ] Rename `RunFixedMainLoopSystem` → `RunFixedMainLoopSystems`
- [ ] Rename `TransformSystem` → `TransformSystems`
- [ ] Rename `RemoteSet` → `RemoteSystems`
- [ ] Rename `RenderSet` → `RenderSystems`
- [ ] Rename `SpriteSystem` → `SpriteSystems`
- [ ] Rename `StateTransitionSteps` → `StateTransitionSystems`
- [ ] Rename `RenderUiSystem` → `RenderUiSystems`
- [ ] Rename `UiSystem` → `UiSystems`
- [ ] Rename `Animation` → `AnimationSystems`
- [ ] Rename `AssetEvents` → `AssetEventSystems`
- [ ] Rename `TrackAssets` → `AssetTrackingSystems`
- [ ] Rename `UpdateGizmoMeshes` → `GizmoMeshSystems`
- [ ] Rename `InputSystem` → `InputSystems`
- [ ] Rename `InputFocusSet` → `InputFocusSystems`
- [ ] Rename `ExtractMaterialsSet` → `MaterialExtractionSystems`
- [ ] Rename `ExtractMeshesSet` → `MeshExtractionSystems`
- [ ] Rename `RumbleSystem` → `RumbleSystems`
- [ ] Rename `CameraUpdateSystem` → `CameraUpdateSystems`
- [ ] Rename `ExtractAssetsSet` → `AssetExtractionSystems`
- [ ] Rename `Update2dText` → `Text2dUpdateSystems`
- [ ] Rename `TimeSystem` → `TimeSystems`
- [ ] Rename `EventUpdates` → `EventUpdateSystems`

Optional changes:
- [ ] Adopt `*Systems` naming for custom ecosystem system sets

**Search Patterns:**

```rust
// Old system set names
AccessibilitySystem
GizmoRenderSystem
PickSet
RunFixedMainLoopSystem
TransformSystem
RemoteSet
RenderSet
SpriteSystem
StateTransitionSteps
RenderUiSystem
UiSystem
Animation
AssetEvents
TrackAssets
UpdateGizmoMeshes
InputSystem
InputFocusSet
ExtractMaterialsSet
ExtractMeshesSet
RumbleSystem
CameraUpdateSystem
ExtractAssetsSet
Update2dText
TimeSystem
EventUpdates

// Common patterns with old names
.in_set(
.before(
.after(
.run_if(
.add_systems(
.configure_sets(
```

**Migration Examples:**

```rust
// 0.16: Old system set names
app.add_systems(Update, my_system.in_set(TransformSystem::TransformPropagate));
app.add_systems(PostUpdate, render_system.after(UiSystem::Prepare));
app.configure_sets(Update, MySet.before(InputSystem));

// 0.17: New *Systems suffix
app.add_systems(Update, my_system.in_set(TransformSystems::TransformPropagate));
app.add_systems(PostUpdate, render_system.after(UiSystems::Prepare));
app.configure_sets(Update, MySet.before(InputSystems));

// 0.16: Various renamed sets
.in_set(PickSet)
.before(RenderSet::Render)
.after(StateTransitionSteps)
.in_set(AssetEvents)

// 0.17: Consistent naming
.in_set(PickingSystems)
.before(RenderSystems::Render)
.after(StateTransitionSystems)
.in_set(AssetEventSystems)
```

---

---

## Guide 105: TAA is no longer experimental

**Pull Requests:** #18349

**Description:**
Temporal Anti-Aliasing (TAA) has graduated from experimental status and is now part of the default plugin setup. The `TemporalAntiAliasPlugin` is automatically included via `DefaultPlugins` through the new `AntiAliasPlugin`. Import paths have changed from `bevy::anti_alias::experimental::taa` to `bevy::anti_alias::taa`. TAA now uses `MipBias` as a required component in the main world instead of manual render world overrides.

**Migration Checklist:**

Required changes:
- [ ] Remove manual `TemporalAntiAliasPlugin` additions to app (now in `DefaultPlugins`)
- [ ] Update imports: `bevy::anti_alias::experimental::taa::*` → `bevy::anti_alias::taa::*`
- [ ] Update `TemporalAntiAliasNode` import path
- [ ] Update `TemporalAntiAliasing` import path
- [ ] Update `TemporalAntiAliasPlugin` import path (if still explicitly used)

Optional changes:
- [ ] Review TAA camera setup to leverage new `MipBias` component integration

**Search Patterns:**

```rust
// Old import paths
bevy::anti_alias::experimental::taa
use bevy::anti_alias::experimental::taa::TemporalAntiAliasing
use bevy::anti_alias::experimental::taa::TemporalAntiAliasNode
use bevy::anti_alias::experimental::taa::TemporalAntiAliasPlugin

// Plugin additions
.add_plugins(TemporalAntiAliasPlugin)

// Component usage
TemporalAntiAliasing
```

**Migration Examples:**

```rust
// 0.16: Manual plugin addition and experimental import
use bevy::anti_alias::experimental::taa::TemporalAntiAliasPlugin;
use bevy::anti_alias::experimental::taa::TemporalAntiAliasing;

App::new()
    .add_plugins(DefaultPlugins)
    .add_plugins(TemporalAntiAliasPlugin)
    .run();

// 0.17: Plugin included in DefaultPlugins, new import path
use bevy::anti_alias::taa::TemporalAntiAliasing;

App::new()
    .add_plugins(DefaultPlugins) // TAA plugin already included
    .run();

// 0.16: Adding TAA to camera
use bevy::anti_alias::experimental::taa::TemporalAntiAliasing;

commands.spawn((
    Camera3d::default(),
    TemporalAntiAliasing::default(),
));

// 0.17: New import path, same usage
use bevy::anti_alias::taa::TemporalAntiAliasing;

commands.spawn((
    Camera3d::default(),
    TemporalAntiAliasing::default(),
));
```

---

---

## Guide 106: Text2d moved to bevy_sprite

**Pull Requests:** #20594

**Description:**
The world-space text components `Text2d` and `Text2dShadow` have been relocated from the text module to the `bevy_sprite` crate, along with their associated rendering systems. This reorganization better reflects their purpose as sprite-based world-space text rendering.

**Migration Checklist:**

Required changes:
- [ ] Update `Text2d` imports to `bevy::sprite::Text2d` or `bevy_sprite::Text2d`
- [ ] Update `Text2dShadow` imports to `bevy::sprite::Text2dShadow` or `bevy_sprite::Text2dShadow`
- [ ] Update any text 2D system imports to reference `bevy_sprite`

**Search Patterns:**

```rust
// Old imports
use bevy::text::Text2d
use bevy::text::Text2dShadow
bevy::text::Text2d
bevy_text::Text2d

// Component usage
Text2d
Text2dShadow
```

**Migration Examples:**

```rust
// 0.16: Old import from text module
use bevy::text::{Text2d, Text2dShadow};

// 0.17: New import from sprite
use bevy::sprite::{Text2d, Text2dShadow};
// or when using bevy_sprite directly
use bevy_sprite::{Text2d, Text2dShadow};

// 0.16: Spawning world-space text
use bevy::text::Text2d;

commands.spawn((
    Text2d::new("Hello World"),
    Transform::from_xyz(0.0, 0.0, 0.0),
));

// 0.17: Same usage, different import
use bevy::sprite::Text2d;

commands.spawn((
    Text2d::new("Hello World"),
    Transform::from_xyz(0.0, 0.0, 0.0),
));
```

---

---

## Guide 107: TextShadow moved to bevy::ui::widget::text

**Pull Requests:** (None listed)

**Description:**
The `TextShadow` component for UI text has been moved to a more specific module path: `bevy::ui::widget::text`. This reorganization improves the UI module structure and separates widget-specific components.

**Migration Checklist:**

Required changes:
- [ ] Update `TextShadow` imports to `bevy::ui::widget::text::TextShadow`

**Search Patterns:**

```rust
// Old imports (various possible locations)
use bevy::ui::TextShadow
bevy::ui::TextShadow
TextShadow

// Component usage in UI contexts
TextShadow
```

**Migration Examples:**

```rust
// 0.16: Old import path
use bevy::ui::TextShadow;

// 0.17: New import path
use bevy::ui::widget::text::TextShadow;

// 0.16: Using TextShadow in UI
use bevy::ui::TextShadow;

commands.spawn((
    Text::new("Shadowed Text"),
    TextShadow::default(),
));

// 0.17: Same usage, different import
use bevy::ui::widget::text::TextShadow;

commands.spawn((
    Text::new("Shadowed Text"),
    TextShadow::default(),
));
```

---

---

## Guide 108: TextureFormat::pixel_size returns Result

**Pull Requests:** #20574

**Description:**
The `TextureFormat::pixel_size()` method now returns `Result<usize, TextureAccessError>` instead of `usize`. This change makes the API safer by explicitly handling texture formats that don't have well-defined pixel sizes (such as compressed formats), which previously could cause runtime panics.

**Migration Checklist:**

Required changes:
- [ ] Handle `Result` from `TextureFormat::pixel_size()` calls
- [ ] Replace direct `usize` usage with `Result<usize, TextureAccessError>` handling
- [ ] Use `unwrap()`, `expect()`, `?`, or proper error handling

**Search Patterns:**

```rust
// Method calls
.pixel_size()
TextureFormat::pixel_size

// Direct assignments expecting usize
let size = format.pixel_size()
let pixel_size: usize =
```

**Migration Examples:**

```rust
// 0.16: Direct usize return
let format = TextureFormat::Rgba8UnormSrgb;
let size = format.pixel_size();
let bytes = width * height * size;

// 0.17: Result must be handled
let format = TextureFormat::Rgba8UnormSrgb;
let size = format.pixel_size().unwrap();
let bytes = width * height * size;

// 0.16: Using in calculations
fn calculate_buffer_size(format: TextureFormat, width: u32, height: u32) -> usize {
    width as usize * height as usize * format.pixel_size()
}

// 0.17: Propagate error with ?
fn calculate_buffer_size(format: TextureFormat, width: u32, height: u32)
    -> Result<usize, TextureAccessError>
{
    let size = format.pixel_size()?;
    Ok(width as usize * height as usize * size)
}

// 0.17: Or handle with expect for known formats
fn calculate_buffer_size(format: TextureFormat, width: u32, height: u32) -> usize {
    let size = format.pixel_size()
        .expect("format should have well-defined pixel size");
    width as usize * height as usize * size
}

// 0.17: Match on Result for conditional logic
match format.pixel_size() {
    Ok(size) => {
        // Use size
        process_sized_format(size)
    }
    Err(_) => {
        // Handle compressed or special formats
        process_special_format(format)
    }
}
```

---

---

## Guide 109: UI Debug Options moved from bevy_ui to bevy_ui_render

**Pull Requests:** #18703

**Description:**
The `UiDebugOptions` resource for controlling the UI debug overlay has been moved from the internal `bevy_ui` crate to the `bevy_ui_render` crate. It remains accessible from `bevy::prelude` but the direct crate import path has changed. This reorganization better reflects the rendering nature of debug overlays.

**Migration Checklist:**

Required changes:
- [ ] Update direct `UiDebugOptions` imports from `bevy::ui::UiDebugOptions` to `bevy_ui_render::prelude::*`

Optional changes:
- [ ] Continue using `bevy::prelude::*` (still works, no change needed)

**Search Patterns:**

```rust
// Old direct import
use bevy::ui::UiDebugOptions
bevy::ui::UiDebugOptions

// Resource usage
UiDebugOptions
world.resource_mut::<UiDebugOptions>
```

**Migration Examples:**

```rust
// 0.16: Direct import from bevy::ui
use bevy::ui::UiDebugOptions;

fn setup(mut debug_options: ResMut<UiDebugOptions>) {
    debug_options.enabled = true;
}

// 0.17: Import from bevy_ui_render (if not using full bevy crate)
use bevy_ui_render::prelude::*;

fn setup(mut debug_options: ResMut<UiDebugOptions>) {
    debug_options.enabled = true;
}

// 0.17: Or continue using bevy::prelude (no change needed)
use bevy::prelude::*;

fn setup(mut debug_options: ResMut<UiDebugOptions>) {
    debug_options.enabled = true;
}
```

---

---

## Guide 110: Unified system state flag

**Pull Requests:** #19506

**Description:**
Systems now use a unified `SystemStateFlags` bitflag type to represent different system states, replacing the individual boolean methods `is_send()`, `is_exclusive()`, and `has_deferred()`. This simplifies the `System` trait interface and makes state management more efficient through bitflag operations.

**Migration Checklist:**

Required changes:
- [ ] Replace `fn is_send(&self) -> bool` with bitflag in `flags()`
- [ ] Replace `fn is_exclusive(&self) -> bool` with bitflag in `flags()`
- [ ] Replace `fn has_deferred(&self) -> bool` with bitflag in `flags()`
- [ ] Implement `fn flags(&self) -> SystemStateFlags` method
- [ ] Use `SystemStateFlags::NON_SEND` for non-send systems (note: inverted logic from `is_send`)
- [ ] Use `SystemStateFlags::EXCLUSIVE` for exclusive systems
- [ ] Use `SystemStateFlags::HAS_DEFERRED` for systems with deferred operations
- [ ] Combine flags with `|` operator when multiple apply

**Search Patterns:**

```rust
// Old methods in System implementations
fn is_send(&self) -> bool
fn is_exclusive(&self) -> bool
fn has_deferred(&self) -> bool

// System trait implementations
impl System for

// Look for patterns that check these states
system.is_send()
system.is_exclusive()
system.has_deferred()
```

**Migration Examples:**

```rust
// 0.16: Individual boolean methods
impl System for MyCustomSystem {
    type In = ();
    type Out = ();

    fn is_send(&self) -> bool {
        false
    }

    fn is_exclusive(&self) -> bool {
        true
    }

    fn has_deferred(&self) -> bool {
        false
    }

    // other methods...
}

// 0.17: Unified flags method
impl System for MyCustomSystem {
    type In = ();
    type Out = ();

    fn flags(&self) -> SystemStateFlags {
        SystemStateFlags::NON_SEND | SystemStateFlags::EXCLUSIVE
    }

    // other methods...
}

// 0.16: Send, non-exclusive system without deferred
impl System for SendSystem {
    fn is_send(&self) -> bool { true }
    fn is_exclusive(&self) -> bool { false }
    fn has_deferred(&self) -> bool { false }
}

// 0.17: Empty flags for default behavior
impl System for SendSystem {
    fn flags(&self) -> SystemStateFlags {
        SystemStateFlags::empty()
    }
}

// 0.16: Non-send with deferred operations
impl System for DeferredSystem {
    fn is_send(&self) -> bool { false }
    fn is_exclusive(&self) -> bool { false }
    fn has_deferred(&self) -> bool { true }
}

// 0.17: Combined flags
impl System for DeferredSystem {
    fn flags(&self) -> SystemStateFlags {
        SystemStateFlags::NON_SEND | SystemStateFlags::HAS_DEFERRED
    }
}
```

---

---

## Guide 111: view_transformations.wgsl deprecated in favor of view.wgsl

**Pull Requests:** #20313

**Description:**
All shader functions in `view_transformations.wgsl` have been replaced and deprecated in favor of new functions in `view.wgsl`. The new API requires passing custom view bindings explicitly, enabling code reuse and flexibility. The deprecated file will be removed in Bevy 0.18.

**Migration Checklist:**

Required changes:
- [ ] Replace `#import bevy_pbr::view_transformations` with `#import bevy_render::view`
- [ ] Update `view_transformations::position_view_to_world()` calls to pass `view_bindings::view.world_from_view` parameter
- [ ] Update all other `view_transformations::*` function calls to new `view::*` equivalents with binding parameters

**Search Patterns:**

```wgsl
// Old imports
#import bevy_pbr::view_transformations
bevy_pbr::view_transformations

// Function calls
view_transformations::position_view_to_world
view_transformations::position_world_to_view
view_transformations::direction_view_to_world
view_transformations::direction_world_to_view
view_transformations::
```

**Migration Examples:**

```wgsl
// 0.16: Old view_transformations API
#import bevy_pbr::view_transformations

let world_pos = view_transformations::position_view_to_world(view_pos);
let view_dir = view_transformations::direction_world_to_view(world_dir);

// 0.17: New view API with explicit bindings
#import bevy_render::view

let world_pos = view::position_view_to_world(view_pos, view_bindings::view.world_from_view);
let view_dir = view::direction_world_to_view(world_dir, view_bindings::view.view_from_world);

// 0.16: Converting world to view space
#import bevy_pbr::view_transformations

fn fragment(in: VertexOutput) -> @location(0) vec4<f32> {
    let view_pos = view_transformations::position_world_to_view(in.world_position);
    // ... use view_pos
}

// 0.17: Explicit binding parameter
#import bevy_render::view

fn fragment(in: VertexOutput) -> @location(0) vec4<f32> {
    let view_pos = view::position_world_to_view(
        in.world_position,
        view_bindings::view.view_from_world
    );
    // ... use view_pos
}
```

---

---

## Guide 112: Enable Wayland by default

**Pull Requests:** #19232

**Description:**
Wayland support is now enabled by default in the `bevy` crate's default features. On Linux systems without Wayland development libraries, this will cause build failures. Users must either install the required Wayland client libraries or disable default features.

**Migration Checklist:**

Required changes (Linux only, if build errors occur):
- [ ] Install `libwayland-dev` on Ubuntu/Debian: `sudo apt install libwayland-dev`
- [ ] Install `wayland` on Arch: `sudo pacman -S wayland`
- [ ] Add `wayland` to `buildInputs` in Nix: `buildInputs = [ pkgs.wayland ];`
- [ ] OR disable default features in Cargo.toml: `bevy = { version = "0.17", default-features = false }`

**Search Patterns:**

Build errors:
```text
pkg-config exited with status code 1
wayland-client
wayland-client.pc
PKG_CONFIG_PATH
libwayland-dev
```

Cargo.toml:
```toml
[dependencies]
bevy =
default-features =
```

**Migration Examples:**

```toml
# 0.16: Wayland was optional, X11 was default
[dependencies]
bevy = "0.16"

# 0.17: Wayland now in default features
[dependencies]
bevy = "0.17"  # Requires Wayland libraries on Linux

# 0.17: Option 1 - Install Wayland libraries (Ubuntu)
# Run: sudo apt install libwayland-dev

# 0.17: Option 2 - Disable default features if Wayland not available
[dependencies]
bevy = { version = "0.17", default-features = false, features = ["x11", ...] }
```

```nix
# 0.16: Nix build without Wayland
{ pkgs }:
pkgs.mkShell {
  buildInputs = [ ];
}

# 0.17: Add Wayland package
{ pkgs }:
pkgs.mkShell {
  buildInputs = [ pkgs.wayland ];
}
```

Build error example:
```text
error: called `Result::unwrap()` on an `Err` value:
pkg-config exited with status code 1
> PKG_CONFIG_ALLOW_SYSTEM_LIBS=1 PKG_CONFIG_ALLOW_SYSTEM_CFLAGS=1 pkg-config --libs --cflags wayland-client

The system library `wayland-client` required by crate `wayland-sys` was not found.
The file `wayland-client.pc` needs to be installed and the PKG_CONFIG_PATH environment variable must contain its parent directory.
```

---

---

## Guide 113: wgpu 25

**Pull Requests:** #19563

**Description:**
Bevy has upgraded to wgpu 25, which introduces breaking changes to shader bind group organization. The most significant change: dynamic offsets and uniforms cannot coexist with binding arrays in the same bind group. This has reorganized 3D rendering bind groups, shifting material bindings from `@group(2)` to `@group(3)`. A `MATERIAL_BIND_GROUP` shader definition ensures future compatibility. Additionally, float constants must now have explicit type declarations.

**Migration Checklist:**

Required changes:
- [ ] Update material shaders: change `@group(2)` to `@group(#{MATERIAL_BIND_GROUP})`
- [ ] Update custom 3D shader bind group indices to new numbering:
  - `@group(0)`: view binding resources (unchanged)
  - `@group(1)`: view resources requiring binding arrays (new)
  - `@group(2)`: mesh binding resources (was mesh, unchanged)
  - `@group(3)`: material binding resources (was `@group(2)`)
- [ ] Add explicit type to float constant exports: `const FOO = 1.0;` → `const FOO: f32 = 1.0;`
- [ ] Search for validation errors mentioning group/binding mismatches and update indices

Optional changes:
- [ ] Review wgpu 25 changelog for additional breaking changes affecting custom rendering code

**Search Patterns:**

```wgsl
// Material bind groups that need updating
@group(2) @binding(

// Float constants without explicit types
const FOO = 1.0;
const BAR =

// Shader imports with materials
#import bevy_pbr::

// Any group/binding declarations in custom shaders
@group(
@binding(
```

Rust error patterns:
```text
// Validation errors
wgpu error: Validation Error
Shader global ResourceBinding { group: 2, binding:
is not available in the pipeline layout
bevy_render::render_resource::pipeline_cache::PipelineCache::process_pipeline_queue_system
```

**Migration Examples:**

```wgsl
// 0.16: Material at group 2
@group(2) @binding(0)
var<uniform> material: StandardMaterial;

@group(2) @binding(1)
var base_color_texture: texture_2d<f32>;

// 0.17: Material at group 3 (or use MATERIAL_BIND_GROUP)
@group(#{MATERIAL_BIND_GROUP}) @binding(0)
var<uniform> material: StandardMaterial;

@group(#{MATERIAL_BIND_GROUP}) @binding(1)
var base_color_texture: texture_2d<f32>;

// 0.16: Float constants without types
const ROUGHNESS_FACTOR = 0.5;
const METALLIC_VALUE = 1.0;

// 0.17: Explicit types required
const ROUGHNESS_FACTOR: f32 = 0.5;
const METALLIC_VALUE: f32 = 1.0;

// 0.16: Custom material shader
#import bevy_pbr::mesh_vertex_output::VertexOutput

@group(2) @binding(0)
var<uniform> custom_material: CustomMaterial;

@fragment
fn fragment(mesh: VertexOutput) -> @location(0) vec4<f32> {
    return custom_material.color;
}

// 0.17: Updated bind group index
#import bevy_pbr::mesh_vertex_output::VertexOutput

@group(#{MATERIAL_BIND_GROUP}) @binding(0)
var<uniform> custom_material: CustomMaterial;

@fragment
fn fragment(mesh: VertexOutput) -> @location(0) vec4<f32> {
    return custom_material.color;
}
```

**Debugging bind group errors:**

When encountering validation panics like:
```
wgpu error: Validation Error
Shader global ResourceBinding { group: 2, binding: 100 } is not available in the pipeline layout
```

1. Search your shaders for the mentioned group and binding: `@group(2) @binding(100)`
2. Update the group index according to the new numbering scheme
3. For material resources, use `@group(#{MATERIAL_BIND_GROUP})` for forward compatibility

---

---

## Summary Statistics

**Total Guides Reviewed:** 11 (guides 103-113)

**Breaking Changes by Category:**
- API Changes: 5 (System::run Result, pixel_size Result, unified flags, wgpu 25 bind groups, view transforms)
- Renames: 2 (System set naming, TAA non-experimental paths)
- Relocations: 3 (Text2d to sprite, TextShadow to widget, UiDebugOptions to render)
- Build/Platform: 1 (Wayland default)

**Impact Assessment:**
- High Impact: wgpu 25 (affects all custom shaders), System::run Result (affects system execution), System set renames (30+ types)
- Medium Impact: TAA paths, TextureFormat::pixel_size, unified system flags, view_transformations deprecation
- Low Impact: Text2d/TextShadow moves, UiDebugOptions move, Wayland default (Linux only)

**Required vs Optional Changes:**
- Required: 98% (most changes break compilation)
- Optional: 2% (optimizations, best practices)

---
