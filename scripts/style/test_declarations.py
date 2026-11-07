"""A line of a Rust file reads as the item that declares it, so a held finding can follow its declaration."""

from __future__ import annotations

import sys
import declarations
import unittest
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))

from declarations import Declaration, DeclaredItem, ImplOrTraitItem, ModuleItem, NoDeclarationAtLine, read_declarations

SOURCE = """use std::fmt;

/// The parent.
#[derive(Debug)]
pub struct Parent {
    old: u32,
}

impl fmt::Display for App {
    fn fmt(&self) -> u32 {
        1
    }
}
"""


def declaration(text: str, line: int) -> Declaration | NoDeclarationAtLine:
    read = read_declarations(text).declaration_at(line)
    return read.declaration if isinstance(read, DeclaredItem) else read


@final
class DeclarationTests(unittest.TestCase):
    def test_a_field_a_derive_and_a_doc_comment_line_read_their_struct(self) -> None:
        parent = Declaration("struct", "Parent", ModuleItem())
        self.assertEqual([declaration(SOURCE, line) for line in (6, 4, 3)], [parent] * 3)

    def test_a_method_line_of_a_trait_impl_reads_the_method_under_the_impl_type(self) -> None:
        self.assertEqual(declaration(SOURCE, 11), Declaration("fn", "fmt", ImplOrTraitItem("App")))

    def test_a_use_line_and_an_impl_header_line_read_no_declaration(self) -> None:
        self.assertEqual([declaration(SOURCE, line) for line in (1, 9)], [NoDeclarationAtLine()] * 2)

    def test_an_item_in_an_inline_module_and_at_a_file_top_level_are_one_declaration(self) -> None:
        inline, = (item.declaration for item in read_declarations("mod a {\n    struct X;\n}\n").declared_items if item.declaration.name == "X")
        self.assertEqual(inline, read_declarations("struct X;\n").declared_items[0].declaration)


    def test_trait_membership_preserves_the_implementing_type_and_reads_qualified_generic_headers(self) -> None:
        headers = (
            ("impl Trait for Type", ("Trait",)),
            ("impl<T> crate::traits::Trait<T> for Type<T>", ("crate", "traits", "Trait")),
            ("impl<T> Trait for Type<T> where T: for<'a> Fn(&'a str)", ("Trait",)),
            ("impl<T: for<'a> Fn(&'a str)> Trait for Type<T>", ("Trait",)),
        )
        for header, path in headers:
            with self.subTest(header=header):
                source = header + " {\n    fn method(&self) {}\n}\n"
                item = read_declarations(source).declaration_at(2)
                self.assertIsInstance(item, DeclaredItem)
                if isinstance(item, DeclaredItem):
                    self.assertEqual(item.declaration, Declaration("fn", "method", ImplOrTraitItem("Type")))
                    self.assertEqual(item.implemented_trait, declarations.ImplementedTrait(
                        path, ("<", "T", ">") if "Trait<T>" in header else ()))
                    self.assertEqual(declarations.declaration_line(item.declaration), "fn Type::method")

    def test_trait_arguments_distinguish_impls_and_ignore_only_whitespace(self) -> None:
        items = [read_declarations(header + " { fn method() {} }").declared_items[0].implemented_trait
                 for header in ("impl Trait<u8> for Type", "impl Trait<u16> for Type", "impl Trait < u8 > for Type")]
        self.assertNotEqual(items[0], items[1])
        self.assertEqual(items[0], items[2])

    def test_inherent_trait_body_and_nested_functions_do_not_inherit_trait_impl_membership(self) -> None:
        source = """impl Type {
    fn inherent() {}
}
trait Trait {
    fn required();
}
impl Trait for Type {
    fn method() {
        fn nested() {}
        { fn block_nested() {} }
    }
    const VALUE: u32 = {
        fn constant_nested() {}
        1
    };
}
"""
        items = {item.declaration.name: item for item in read_declarations(source).declared_items}
        self.assertEqual(items["inherent"].declaration.owner, ImplOrTraitItem("Type"))
        self.assertEqual(items["required"].declaration.owner, ImplOrTraitItem("Trait"))
        self.assertEqual(items["nested"].declaration.owner, ModuleItem())
        self.assertEqual([items[name].implemented_trait for name in ("inherent", "required", "nested", "block_nested", "constant_nested")],
                         [declarations.NoImplementedTrait()] * 5)
        self.assertEqual(items["method"].implemented_trait, declarations.ImplementedTrait(("Trait",)))

    def test_an_unreadable_trait_path_keeps_the_readable_impl_owner_without_claiming_membership(self) -> None:
        source = "impl <Type as Family>::Trait for Type {\n    fn method() {}\n}\n"
        item = read_declarations(source).declaration_at(2)
        self.assertIsInstance(item, DeclaredItem)
        if isinstance(item, DeclaredItem):
            self.assertEqual(item.declaration, Declaration("fn", "method", ImplOrTraitItem("Type")))
            self.assertEqual(item.implemented_trait, declarations.UnreadableImplementedTrait())

    def test_implementing_type_metadata_keeps_the_complete_path_arguments_and_header_scope(self) -> None:
        source = """impl<T> Trait for one::Type<T> where T: Copy {
    fn first() {}
}
fn enclosing() {
    impl Trait for two::Type<u8> {
        fn second() {}
    }
}
impl Type {
    fn inherent() {}
}
"""
        file = read_declarations(source)
        items = {item.declaration.name: item for item in file.declared_items}
        first = items["first"].implemented_type
        second = items["second"].implemented_type
        self.assertIsInstance(first, declarations.ImplementedType)
        self.assertIsInstance(second, declarations.ImplementedType)
        if isinstance(first, declarations.ImplementedType) and isinstance(second, declarations.ImplementedType):
            self.assertEqual(first.path, ("one", "Type"))
            self.assertEqual(first.arguments, ("<", "T", ">"))
            self.assertEqual(first.position, declarations.FileLevelImpl())
            self.assertEqual(second.path, ("two", "Type"))
            self.assertEqual(second.arguments, ("<", "u8", ">"))
            self.assertIsInstance(second.position, declarations.BlockScopedImpl)
        self.assertEqual(items["first"].declaration.owner, items["second"].declaration.owner)
        self.assertEqual(items["inherent"].implemented_type, declarations.ImplementedType(("Type",)))
        self.assertEqual(items["enclosing"].implemented_type, declarations.NoImplementedType())

    def test_site_kind_distinguishes_parameters_and_fields_from_headers_and_statements(self) -> None:
        source = """#[test]
fn original(
    input: u32,
) -> u32 {
    input + 1
}
struct Original {
    selected: u32,
}
enum Choice {
    Selected,
}
"""
        file = read_declarations(source)
        function, structure, enumeration = file.declared_items
        self.assertEqual([file.site_kind(function, line) for line in (1, 2)], [declarations.DeclarationSite.HEADER] * 2)
        self.assertEqual(file.site_kind(function, 3), declarations.DeclarationSite.UNSUPPORTED_CHILD)
        self.assertEqual(file.site_kind(function, 5), declarations.DeclarationSite.STATEMENT)
        self.assertEqual(file.site_kind(structure, 8), declarations.DeclarationSite.UNSUPPORTED_CHILD)
        self.assertEqual(file.site_kind(enumeration, 11), declarations.DeclarationSite.UNSUPPORTED_CHILD)
        self.assertEqual(file.site_kind(function, 8), declarations.DeclarationSite.UNREADABLE)

    def test_closure_parameters_keep_their_owning_closures_scope(self) -> None:
        source = """fn original() {
    common_one();
    common_two();
    consume(move |answers| {
        first(answers);
    });
    consume(move |other| {
        second(other);
    });
    common_three();
    common_four();
}
"""
        target = source.replace("original", "renamed").replace("|answers|", "|removed|")
        target = target.replace("first(answers)", "first(removed)").replace("|other|", "|answers|")
        target = target.replace("second(other)", "second(answers)")
        old, new = read_declarations(source), read_declarations(target)
        owner, renamed = old.declared_items[0], new.declared_items[0]
        binding = declarations.local_bindings(old, owner)[0]
        replacement = declarations.local_bindings(new, renamed)[1]
        self.assertEqual(binding.name, replacement.name)
        self.assertNotEqual(old.binding_scope(owner, binding), new.binding_scope(renamed, replacement))
        kept = read_declarations(source.replace("original", "renamed"))
        retained = declarations.local_bindings(kept, kept.declared_items[0])[0]
        self.assertEqual(old.binding_scope(owner, binding), kept.binding_scope(kept.declared_items[0], retained))
        changed = read_declarations(source.replace("consume(move |answers|", "consume(|answers|"))
        changed_binding = declarations.local_bindings(changed, changed.declared_items[0])[0]
        self.assertNotEqual(old.binding_scope(owner, binding), changed.binding_scope(changed.declared_items[0], changed_binding))
        inserted = read_declarations(source.replace("    consume(move |answers|", "    consume(|| 0);\n    consume(move |answers|"))
        shifted = declarations.local_bindings(inserted, inserted.declared_items[0])[0]
        self.assertNotEqual(old.binding_scope(owner, binding), inserted.binding_scope(inserted.declared_items[0], shifted))

    def test_a_typed_initializer_keeps_its_closure_binding_context_after_an_owner_rename(self) -> None:
        for annotation in (
            "handler: fn(u32)",
            "mut handler: fn(u32)",
            "handler: Box<dyn Fn([u32; 2]) -> Option<Vec<u32>>>",
            "handler: Handler<{ 1 }, [u32; 2]>",
        ):
            with self.subTest(annotation=annotation):
                source = f"""fn original() {{
    common_one();
    common_two();
    let {annotation} =
        move |answers| {{
            consume(answers);
        }};
    common_three();
    common_four();
}}
"""
                old = read_declarations(source)
                new = read_declarations(source.replace("fn original", "fn renamed"))
                owner, renamed = old.declared_items[0], new.declared_items[0]
                binding = declarations.binding_at(old, owner, 5)
                retained = declarations.binding_at(new, renamed, 5)
                self.assertIsInstance(binding, declarations.LocalBinding)
                self.assertIsInstance(retained, declarations.LocalBinding)
                if not isinstance(binding, declarations.LocalBinding) or not isinstance(retained, declarations.LocalBinding):
                    self.fail("the typed initializer must retain its closure parameter")
                self.assertIsInstance(binding.closure, declarations.OwningClosure)
                if not isinstance(binding.closure, declarations.OwningClosure):
                    self.fail("the parameter must belong to the initializer's closure")
                self.assertEqual(binding.closure.context, declarations.ClosureInitializer("handler"))
                self.assertEqual(old.binding_scope(owner, binding), new.binding_scope(renamed, retained))
        for annotation in ("handler:", "handler: Handler<u32", "handler: Handler<[u32; 2)>"):
            with self.subTest(unreadable_annotation=annotation):
                file = read_declarations(f"fn owner() {{\n    let {annotation} =\n        move |answers| {{}};\n}}\n")
                self.assertIsInstance(declarations.binding_at(file, file.declared_items[0], 3),
                                      declarations.UnreadableLocalBinding)

    def test_a_deleted_closures_parameter_does_not_belong_to_the_surviving_call(self) -> None:
        source = """fn original() {
    common_one();
    common_two();
    first(move |answers| {
        consume(answers);
    });
    second(move |other| {
        consume(other);
    });
    common_three();
    common_four();
}
"""
        target = """fn renamed() {
    common_one();
    common_two();
    second(move |answers| {
        consume(answers);
    });
    common_three();
    common_four();
}
"""
        old, new = read_declarations(source), read_declarations(target)
        owner, renamed = old.declared_items[0], new.declared_items[0]
        binding = old.local_bindings(owner)[0]
        replacement = new.local_bindings(renamed)[0]
        self.assertEqual(binding.name, replacement.name)
        self.assertNotEqual(old.binding_scope(owner, binding), new.binding_scope(renamed, replacement))
        shifted = read_declarations(source.replace("first(move", "first(input, move"))
        shifted_owner = shifted.declared_items[0]
        shifted_binding = shifted.local_bindings(shifted_owner)[0]
        self.assertNotEqual(old.binding_scope(owner, binding), shifted.binding_scope(shifted_owner, shifted_binding))
        ambiguous = read_declarations(target.replace("    common_three();", "    second(move |other| {});"))
        self.assertIsInstance(declarations.binding_at(ambiguous, ambiguous.declared_items[0], 4),
                              declarations.UnreadableLocalBinding)
        bound = read_declarations("fn owner() {\n    let handler = move |answers| {};\n}\n")
        rebound = read_declarations("fn owner() {\n    let different = move |answers| {};\n}\n")
        self.assertNotEqual(bound.binding_scope(bound.declared_items[0], bound.local_bindings(bound.declared_items[0])[1]),
                            rebound.binding_scope(rebound.declared_items[0], rebound.local_bindings(rebound.declared_items[0])[1]))

    def test_a_retained_closure_scope_excludes_changed_call_arguments(self) -> None:
        source = """    fn uncached_completed_rows_arrive_before_hierarchy_discovery_is_released() {
        struct GatedHierarchy {
            entered: Sender<()>,
            release: Mutex<Receiver<()>>,
        }
        impl HierarchyRunner for GatedHierarchy {
            fn run(&self, _: &str, _: Duration) -> Result<HierarchyEdges, HierarchyError> {
                self.entered.send(()).expect("signal discovery");
                self.release
                    .lock()
                    .expect("release lock")
                    .recv()
                    .expect("release discovery");
                Err(HierarchyError::TimedOut)
            }
        }
        let (entered, waiting) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let expected = vec![session_row("worker", 2)];
        let rows = expected.clone();
        let updates = spawn_with_test_source(
            HierarchyCommand::new(HierarchySource::Command("fixture".to_string())),
            Arc::new(GatedHierarchy {
                entered,
                release: Mutex::new(released),
            }),
            move |answers| {
                answers
                    .publish(ScanStage::Completed, rows.clone())
                    .expect("publish completion");
            },
        );
        waiting
            .recv_timeout(Duration::from_millis(200))
            .expect("discovery starts");
        let early = updates.try_recv();
        release
            .send(())
            .expect("release discovery before assertions");
        assert_eq!(
            early.expect("uncached completed rows precede discovery"),
            CensusUpdate::Local {
                rows:      expected.clone(),
                hierarchy: HierarchyEdges::default().into(),
                stage:     ScanStage::Classified,
            }
        );
        assert_eq!(
            updates
                .recv_timeout(Duration::from_millis(200))
                .expect("failed discovery answers"),
            noted(expected, HierarchyNotice::Failed(HierarchyError::TimedOut))
        );
    }
"""
        target = """    fn uncached_first_publication_respects_its_budget_and_holds_claude_rows() {
        struct GatedHierarchy {
            entered: Sender<()>,
            release: Mutex<Receiver<()>>,
        }
        impl HierarchyRunner for GatedHierarchy {
            fn run(&self, _: &str, _: Duration) -> Result<HierarchyEdges, HierarchyError> {
                self.entered.send(()).expect("signal discovery");
                self.release
                    .lock()
                    .expect("release lock")
                    .recv()
                    .expect("release discovery");
                Err(HierarchyError::TimedOut)
            }
        }
        let (entered, waiting) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let expected = vec![session_row("worker", 2)];
        let rows = expected.clone();
        let budget = HIERARCHY_FIRST_PUBLISH_BUDGET / 100;
        let deadline = budget + HIERARCHY_FIRST_PUBLISH_BUDGET / 5;
        let (finish, details) = mpsc::channel();
        let started = Instant::now();
        let updates = spawn_with_test_source_and_budget(
            HierarchyCommand::new(HierarchySource::Command("fixture".to_string())),
            Arc::new(GatedHierarchy {
                entered,
                release: Mutex::new(released),
            }),
            budget,
            move |answers| {
                answers
                    .publish(ScanStage::Completed, rows.clone())
                    .expect("publish completion");
                details
                    .recv()
                    .expect("keep the scan open for the late answer");
            },
        );
        waiting
            .recv_timeout(Duration::from_millis(200))
            .expect("discovery starts");
        let early = updates.recv_timeout(deadline);
        let elapsed = started.elapsed();
        eprintln!("bounded first publication: {} ms", elapsed.as_millis());
        release
            .send(())
            .expect("release discovery before assertions");
        assert!(elapsed < deadline, "first publication took {elapsed:?}");
        assert_eq!(
            early.expect("safe rows publish inside the startup budget"),
            CensusUpdate::Local {
                rows:      Vec::new(),
                hierarchy: HierarchySnapshot::default(),
                stage:     ScanStage::Completed,
            }
        );
        let late = updates.recv_timeout(deadline);
        finish.send(()).expect("release details before assertions");
        assert_eq!(
            late.expect("failed discovery answers during detail reads"),
            noted(expected, HierarchyNotice::Failed(HierarchyError::TimedOut))
        );
    }
"""
        old, new = read_declarations(source), read_declarations(target)
        owner, renamed = old.declared_items[0], new.declared_items[0]
        binding = next(binding for binding in old.local_bindings(owner) if binding.name == "answers")
        retained = next(binding for binding in new.local_bindings(renamed) if binding.name == "answers")
        self.assertEqual((binding.line, retained.line), (27, 32))
        self.assertEqual(binding.closure, retained.closure)
        self.assertEqual(old.binding_scope(owner, binding), new.binding_scope(renamed, retained))

    def test_body_preservation_replaces_code_identifiers_and_keeps_literals_and_comments(self) -> None:
        for before, after, old, new in (
            ('    "original"', '    "renamed"', "original", "renamed"),
            ("    'a'", "    'b'", "a", "b"),
            ('    r#"original"#', '    r#"renamed"#', "original", "renamed"),
            ("    // original", "    // renamed", "original", "renamed"),
            ("    /*\noriginal\n    */", "    /*\nrenamed\n    */", "original", "renamed"),
        ):
            with self.subTest(before=before):
                self.assertEqual(declarations.preserved_body(
                    declarations.ReadableBody(tuple(before.splitlines())),
                    declarations.ReadableBody(tuple(after.splitlines())), old, new),
                    2 if before.startswith("    /*") else 0)
        self.assertEqual(declarations.preserved_body(
            declarations.ReadableBody(('    original(); // original', '    "original";')),
            declarations.ReadableBody(('    renamed(); // original', '    "original";')), "original", "renamed"), 2)

    def test_const_generic_header_braces_do_not_supply_an_items_body(self) -> None:
        for header, field in (
            ("struct Original<const N: usize = { 1 }>", "    selected: [u32; N],"),
            ("struct Original<const N: usize = { 1 }> where [u8; { 2 }]: Sized", "    selected: [u32; N],"),
            ("fn original<const N: usize>() -> [u8; { 1 }]", "    build()"),
        ):
            with self.subTest(header=header):
                source = header + " {\n" + field + "\n}\n"
                file = read_declarations(source)
                item = file.declared_items[0]
                self.assertEqual(declarations.item_body(file, item), declarations.ReadableBody((field,)))
                self.assertEqual(file.declaration_at(2), item)
                renamed = read_declarations(source.replace("Original", "Renamed").replace("original", "renamed"))
                self.assertEqual(declarations.moved_site_line(file, 2, item, renamed, renamed.declared_items[0]), 2)
        for source in ("struct Original<const N: usize = { 1 }>;", "struct Original<const N: usize = { 1 }>([u8; N]);",
                       "struct Original<const N: usize = { 1 }>"):
            with self.subTest(source=source):
                file = read_declarations(source)
                self.assertIsInstance(declarations.item_body(file, file.declared_items[0]), declarations.UnreadableBody)
        old = read_declarations("struct Original<const N: usize = { 1 }> {\n    selected: [u32; N],\n}\n")
        new = read_declarations("struct Renamed<const N: usize = { 1 }> {\n    replacement: [u64; N],\n}\n")
        old_body, new_body = declarations.item_body(old, old.declared_items[0]), declarations.item_body(new, new.declared_items[0])
        self.assertIsInstance(old_body, declarations.ReadableBody)
        self.assertIsInstance(new_body, declarations.ReadableBody)
        if isinstance(old_body, declarations.ReadableBody) and isinstance(new_body, declarations.ReadableBody):
            self.assertEqual(declarations.preserved_body(old_body, new_body, "Original", "Renamed"), 0)

    def test_outside_rename_source_evidence_excludes_headers_and_reads_local_children(self) -> None:
        before = "#[test]\nfn original() {\n    let rows = collect();\n    serve(move |answers| { publish(answers); });\n    rows();\n}\n"
        after = "// inserted\n" + before.replace("original", "renamed")
        read = read_declarations(before)
        destination = read_declarations(after)
        diff = declarations.source_diff(before, after)
        expected = Declaration("fn", "renamed", ModuleItem())
        self.assertEqual(declarations.mapped_site(diff, "a.rs", 3, destination),
                         declarations.MappedDeclaration("a.rs", 4, expected))
        self.assertEqual(declarations.mapped_site(diff, "a.rs", 2, destination),
                         declarations.MappedDeclaration("a.rs", 3, expected))
        owner = read.declaration_at(3)
        self.assertIsInstance(owner, DeclaredItem)
        if isinstance(owner, DeclaredItem):
            self.assertEqual(owner.declaration, Declaration("fn", "original", ModuleItem()))
            bindings = declarations.local_bindings(read, owner)
            self.assertEqual([(binding.name, binding.line) for binding in bindings], [("rows", 3), ("answers", 4)])
            self.assertIsInstance(declarations.binding_at(read, owner, 5), declarations.NoLocalBinding)
            self.assertEqual(declarations.item_body(read, owner), declarations.ReadableBody((
                "    let rows = collect();", "    serve(move |answers| { publish(answers); });", "    rows();")))
        body_before = read_declarations("#[test]\nfn original() { Device::new() }\n")
        body_after = read_declarations("#[test]\nfn renamed() { Total::new() }\n")
        old_body = declarations.item_body(body_before, body_before.declared_items[0])
        new_body = declarations.item_body(body_after, body_after.declared_items[0])
        self.assertEqual(old_body, declarations.ReadableBody((" Device::new() ",)))
        self.assertEqual(new_body, declarations.ReadableBody((" Total::new() ",)))
        if isinstance(old_body, declarations.ReadableBody) and isinstance(new_body, declarations.ReadableBody):
            self.assertEqual(declarations.preserved_body(old_body, new_body, "original", "renamed"), 0)
        declarations_source = read_declarations("struct Type {\n    value: u32,\n}\ntrait Trait {\n    fn child() { first(); }\n}\n")
        child = declarations_source.declaration_at(5)
        self.assertIsInstance(child, DeclaredItem)
        if isinstance(child, DeclaredItem):
            parent = declarations.lexical_parent(declarations_source, child)
            self.assertIsInstance(parent, DeclaredItem)
            if isinstance(parent, DeclaredItem):
                self.assertEqual(parent.declaration, Declaration("trait", "Trait", ModuleItem()))
        aliases = read_declarations("type Alias = u32;\nfn nearby() { first(); }\n")
        self.assertIsInstance(declarations.item_body(aliases, aliases.declared_items[0]), declarations.UnreadableBody)
        deleted = declarations.mapped_site(declarations.source_diff(before, ""), "a.rs", 3, read_declarations(""))
        self.assertEqual(deleted, declarations.UnmappedSite(declarations.MappingFailure.DELETED))
        ambiguous = read_declarations("fn a() {} fn b() {}\n")
        self.assertEqual(declarations.mapped_site(declarations.source_diff(before, "fn a() {} fn b() {}\n"), "a.rs", 2, ambiguous),
                         declarations.UnmappedSite(declarations.MappingFailure.AMBIGUOUS))
        self.assertEqual(declarations.mapped_site(declarations.source_diff(before, "use crate::x;\n"), "a.rs", 2,
                                                 read_declarations("use crate::x;\n")), declarations.MappedNoDeclaration("a.rs", 1))


if __name__ == "__main__":
    _ = unittest.main()
