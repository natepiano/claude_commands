"""Claude launchers resolve an account centrally, then render that decision."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# These are account decisions with a separate, explicit contract.
EXCEPTIONS = {
    ("accounts/accounts.py", "exec_account"): "the user explicitly names the account",
    ("shutdown/restart.py", "_resume_parts"): "ungrouped resumes retain their recorded directory; groups use the resolver",
    ("message/send.py", "relay"): "the headless message relay uses its own login",
    ("notify/review.py", "reviewer_environment"): "the headless reviewer uses the units account through role_account",
}
SELECTORS = frozenset({"pick", "placement", "role_account", "group_label"})
RENDERERS = frozenset({"prepared_account", "account_prefix", "named_account_environment", "account_dir"})


def called(node: ast.Call) -> str:
    return node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""


def names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def starts_claude(node: ast.AST) -> bool:
    parents = {child: parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
    for child in ast.walk(node):
        if isinstance(child, (ast.List, ast.Tuple)) and child.elts:
            parent = parents.get(child)
            if (isinstance(parent, ast.Subscript) and parent.slice is child
                    or isinstance(parent, (ast.For, ast.AsyncFor)) and parent.iter is child
                    or isinstance(parent, ast.keyword) and parent.arg == "choices"):
                continue
            first = child.elts[0]
            if isinstance(first, ast.Constant) and first.value == "claude":
                # Identity and roster queries do not launch an agent.
                if len(child.elts) > 1 and isinstance(child.elts[1], ast.Constant) and child.elts[1].value in {
                        "auth", "agents", "rm", "--version"}:
                    continue
                return True
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and re.search(
                r"\b(?:command|exec) claude(?:\s|$)", child.value):
            return True
    return False


def launch_account_violations(source: str, path: str) -> list[str]:
    tree = ast.parse(source)
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module] = {
        node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    functions["<module>"] = ast.Module(body=[node for node in tree.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))], type_ignores=[])
    calls = {name: [node for node in ast.walk(body) if isinstance(node, ast.Call)] for name, body in functions.items()}
    launchers = {name for name, body in functions.items() if starts_claude(body)
                 and (path, name) not in EXCEPTIONS}
    # Account selection can live in a helper that returns to a launcher, or in
    # a caller that passes its selection down. Audit both sides of that edge.
    connected = set(launchers)
    while True:
        expanded = connected | {name for name, nodes in calls.items()
                                if any(called(node) in connected for node in nodes)}
        expanded |= {called(node) for name in connected for node in calls.get(name, [])
                     if called(node) in functions and (path, called(node)) not in EXCEPTIONS}
        if expanded == connected:
            break
        connected = expanded
    resolver_helpers = {name for name, nodes in calls.items()
                        if any(called(node) == "account_for_group_launch" for node in nodes)}
    while True:
        expanded = resolver_helpers | {name for name, nodes in calls.items()
                                       if any(called(node) in resolver_helpers for node in nodes)}
        if expanded == resolver_helpers:
            break
        resolver_helpers = expanded
    problems: list[str] = []
    audited = connected | {name for file, name in EXCEPTIONS if file == path and name in functions}
    for name in sorted(audited):
        body = functions[name]
        nodes = calls[name]
        for node in nodes:
            if called(node) in SELECTORS:
                reviewer_role = ((path, name) == ("notify/review.py", "reviewer_environment")
                                 and called(node) == "role_account" and node.args
                                 and isinstance(node.args[0], ast.Constant) and node.args[0].value == "unit")
                if not reviewer_role:
                    problems.append(f"{path}:{node.lineno}: {name} selects with {called(node)} instead of account_for_group_launch")
        if (path, name) in EXCEPTIONS:
            continue
        arguments = body.args.args if isinstance(body, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
        trusted = {arg.arg for arg in arguments if arg.annotation is not None and any(
            word in ast.unparse(arg.annotation) for word in ("ClaudeLaunchAccount", "NamedAccount", "SessionShutdown", "ResumeParts", "Session"))}
        assignments = [node for node in ast.walk(body) if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None]
        while True:
            added = set(trusted)
            for assignment in assignments:
                value = assignment.value
                assert value is not None
                selected = any(isinstance(node, ast.Call) and called(node) in {"account_for_group_launch", *resolver_helpers}
                               for node in ast.walk(value))
                retained = bool(names(value) & trusted)
                if selected or retained:
                    targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            added.add(target.id)
            if added == trusted:
                break
            trusted = added
        for assignment in assignments:
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for target in targets:
                if (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "CLAUDE_CONFIG_DIR"):
                    value = assignment.value
                    assert value is not None
                    if not names(value) & trusted:
                        problems.append(f"{path}:{assignment.lineno}: {name} sets CLAUDE_CONFIG_DIR without a resolved account or record")
        for mapping in (node for node in ast.walk(body) if isinstance(node, ast.Dict)):
            for key, value in zip(mapping.keys, mapping.values):
                if (isinstance(key, ast.Constant) and key.value == "CLAUDE_CONFIG_DIR"
                        and not names(value) & trusted):
                    problems.append(f"{path}:{mapping.lineno}: {name} builds CLAUDE_CONFIG_DIR without a resolved account or record")
        for node in nodes:
            if called(node) in RENDERERS and node.args:
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    problems.append(f"{path}:{node.lineno}: {name} renders a literal account instead of a resolved one")
                elif called(node) != "account_dir" and not names(argument) & trusted:
                    problems.append(f"{path}:{node.lineno}: {name} renders an account without a resolver or record")
    return problems


def shell_launch_account_violations(source: str, path: str) -> list[str]:
    active = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    if not re.search(r"\bclaude(?:\s|$)", active):
        return []
    problems: list[str] = []
    for number, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if re.search(r"accounts(?:\.py)?\s+(?:pick|placement|role)\b", line):
            problems.append(f"{path}:{number}: shell launcher selects its own account")
        if re.search(r"\bCLAUDE_CONFIG_DIR=", line) and not re.search(r"resolver|record|account_for_group_launch", line):
            problems.append(f"{path}:{number}: shell launcher sets an account without a resolved account or record")
    return problems


class LaunchAccountTests(unittest.TestCase):
    def test_tool_choices_and_annotations_do_not_turn_status_rendering_into_a_launcher(self) -> None:
        source = '''\
def status(tool: Literal["claude", "codex"]):
    parser.add_argument("tool", choices=("claude", "codex"))
    for tool in ("claude", "codex"):
        selected = accounts.group_label(groups, "units")
    return selected
'''
        self.assertEqual(launch_account_violations(source, "fixture.py"), [])

    def test_selecting_a_role_or_picking_an_account_in_a_launcher_is_rejected(self) -> None:
        for selection in ('accounts.role_account("unit")', 'accounts.pick("claude", now)'):
            for arguments in ('"--resume", "session"', '"-p", "prompt"', '"prompt"', 'prompt'):
                source = f'''\
def launch():
    selected = {selection}
    env = {{}}
    env["CLAUDE_CONFIG_DIR"] = str(selected.dir)
    subprocess.run(["claude", {arguments}], env=env)
'''
                with self.subTest(selection=selection, arguments=arguments):
                    problems = launch_account_violations(source, "fixture.py")
                    self.assertTrue(any("selects with" in problem for problem in problems), problems)

    def test_rendering_a_resolved_account_or_a_recorded_target_is_allowed(self) -> None:
        for setup, render in ((
                'selected = accounts.account_for_group_launch("unit", resume=True)',
                'accounts.prepared_account(selected.label, resume=True)'), (
                'target = record["target"]', 'accounts.prepared_account(target, resume=True)')):
            for arguments in ('"--resume", "session"', '"-p", "prompt"', '"prompt"', 'prompt'):
                source = f'''\
def launch(record: SessionShutdown):
    {setup}
    prepared = {render}
    subprocess.run(["claude", {arguments}], env=prepared.variables)
'''
                with self.subTest(setup=setup, arguments=arguments):
                    self.assertEqual(launch_account_violations(source, "fixture.py"), [])

    def test_hardcoded_launch_directory_is_rejected_even_without_a_picker(self) -> None:
        for environment in ('env = {}; env["CLAUDE_CONFIG_DIR"] = "/somewhere/.claude3"',
                            'env = {"CLAUDE_CONFIG_DIR": directory}'):
            source = f'''\
def launch():
    {environment}
    subprocess.run(["claude", "--resume", "session"], env=env)
'''
            with self.subTest(environment=environment):
                self.assertTrue(any("CLAUDE_CONFIG_DIR" in problem for problem in launch_account_violations(source, "fixture.py")))

    def test_preparing_an_arbitrary_supplied_label_does_not_count_as_resolving_a_launch_account(self) -> None:
        source = '''\
def launch(label: str):
    prepared = accounts.prepared_account(label, resume=False)
    subprocess.run(["claude"], env=prepared.variables)
'''
        self.assertTrue(any("without a resolver or record" in problem
                            for problem in launch_account_violations(source, "fixture.py")))

    def test_selection_at_module_scope_is_checked_as_a_launch_decision(self) -> None:
        source = '''\
selected = accounts.role_account("unit")
subprocess.run(["claude", "--resume", "session"], env={"CLAUDE_CONFIG_DIR": str(selected.dir)})
'''
        self.assertTrue(any("selects with role_account" in problem
                            for problem in launch_account_violations(source, "fixture.py")))

    def test_recorded_resume_exception_does_not_allow_selecting_a_groups_launch_account(self) -> None:
        source = '''\
def _resume_parts(session: Session, prompt: str):
    selected = accounts.role_account("unit")
    return selected
'''
        self.assertTrue(any("selects with role_account" in problem
                            for problem in launch_account_violations(source, "shutdown/restart.py")))

    def test_all_python_claude_launchers_use_the_shared_account_resolver(self) -> None:
        violations: list[str] = []
        for path in sorted(ROOT.rglob("*.py")):
            if path.name.startswith(("test_", "fake_")):
                continue
            violations.extend(launch_account_violations(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT))))
        self.assertEqual(violations, [], "\n".join(violations))

    def test_shell_launchers_do_not_pick_or_set_an_unresolved_account(self) -> None:
        for arguments in ("--print prompt", "-p prompt", "prompt", ""):
            with self.subTest(arguments=arguments):
                selecting = f'chosen=$(python accounts.py pick claude)\nCLAUDE_CONFIG_DIR="$chosen" claude {arguments}\n'
                self.assertTrue(shell_launch_account_violations(selecting, "fixture.sh"))
                unresolved = f'CLAUDE_CONFIG_DIR="/somewhere/.claude3" claude {arguments}\n'
                self.assertTrue(shell_launch_account_violations(unresolved, "fixture.sh"))
                for directory in ("resolver_config_dir", "record_config_dir"):
                    rendering = f'CLAUDE_CONFIG_DIR="${directory}" claude {arguments}\n'
                    self.assertEqual(shell_launch_account_violations(rendering, "fixture.sh"), [])
        violations: list[str] = []
        for path in sorted(ROOT.rglob("*.sh")):
            if path.name.startswith(("test_", "fake_")):
                continue
            violations.extend(shell_launch_account_violations(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT))))
        self.assertEqual(violations, [], "\n".join(violations))


if __name__ == "__main__":
    _ = unittest.main()
