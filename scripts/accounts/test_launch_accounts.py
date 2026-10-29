"""Claude launchers resolve an account centrally, then render that decision."""
from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

from scripts.accounts import accounts, nix_env_fake as fixtures
from scripts.shutdown import launch_permission


ROOT = Path(__file__).resolve().parents[1]
# These are account decisions with a separate, explicit contract.
EXCEPTIONS = {
    ("accounts/accounts.py", "exec_account"): "the user explicitly names the account",
    ("shutdown/restart.py", "_resume_parts"): "ungrouped resumes retain their recorded directory; groups use the resolver",
    ("message/send.py", "relay"): "the relay retains the caller account or uses the showrunners resolver",
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
        if isinstance(child, ast.Call) and called(child) == "start_agent" and any(
                key.arg == "kind" and isinstance(key.value, ast.Constant) and key.value.value == "claude"
                for key in child.keywords):
            return True
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
                        if any(called(node) in {"account_for_group_launch", "account_for_unscoped_launch"} for node in nodes)}
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
                selected = any(isinstance(node, ast.Call) and called(node) in {"account_for_group_launch", "account_for_unscoped_launch", *resolver_helpers}
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
    if not re.search(r"\bclaude(?:\s|$)|CLAUDE_BIN", active):
        return []
    problems: list[str] = []
    launches = re.search(
        r'(?m)^\s*(?:(?:exec|command|open_window)\s+)?claude(?=\s|$)(?!\s*=)'
        + r'|\bcommand=\(claude\b|"\$\{CLAUDE_BIN\}" "\$\{launch_args', active)
    shared_environment = (re.search(r'^\s*claude_launch_environment\s', active, re.M)
                          and 'claude_account.sh"' in active)
    if launches and not shared_environment and "CLAUDE_CONFIG_DIR=" not in active:
        problems.append(f"{path}: Claude launch has no resolved account environment")
    resolved_lines = {source[:match.start()].count("\n") + 1
                      for match in re.finditer(r'(?ms)^[ \t]*CLAUDE_CONFIG_DIR="\$\((.*?)^[ \t]*\)"', source)
                      if "account_for_unscoped_launch(" in match.group(1)}
    for number, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if re.search(r"accounts(?:\.py)?\s+(?:pick|placement|role)\b", line):
            problems.append(f"{path}:{number}: shell launcher selects its own account")
        if (re.search(r"\bCLAUDE_CONFIG_DIR=", line) and number not in resolved_lines
                and not re.search(r"resolver|record|account_for_group_launch", line)):
            problems.append(f"{path}:{number}: shell launcher sets an account without a resolved account or record")
    return problems


@final
class ExecLaunchShellTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.folder = Path()
        self.home = Path()
        self.loader = fixtures.fake_nix_loader()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.folder = self.root / "launch folder" / "nested"
        self.folder.mkdir(parents=True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.loader = fixtures.fake_nix_loader()
        _ = cast(object, self.enterContext(patch.object(accounts, "import_module", side_effect=fixtures.imports_with_nix_loader(self.loader))))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home), "PATH": os.defpath,
            "KEEP": "base", "UNSET_BY_SHELL": "old", "AGENT_NOTES_DIR": str(self.home / "notes"),
            "SHUTDOWN_STATE_DIR": str(self.home / "shutdown"),
            "NOTIFIER_SESSIONS_DIR": str(self.home / "sessions")}, clear=True)))
        _ = self.enterContext(patch.object(launch_permission, "launch_permission",
            return_value=launch_permission.AllowedByShutdownState()))
        _ = self.enterContext(patch.object(Path, "cwd", return_value=self.folder))
        _ = self.enterContext(patch.object(sys.stdin, "isatty", return_value=False))
        prepared = accounts.PreparedAccount({"CLAUDE_CONFIG_DIR": str(self.home / ".claude4")},
                                            frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))
        _ = self.enterContext(patch.object(accounts, "prepared_account", return_value=prepared))
        _ = self.enterContext(patch.object(accounts, "check_codex_target", return_value=accounts.SelectableCodexAccount(
            self.home / ".codex2", "fixture@example.invalid", "codex2")))
        _ = self.enterContext(patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount(
            "codex2", self.home / ".codex2")))
        # No test may start a shell, agent, probe, or remote command.
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("unexpected subprocess")))
        _ = self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected subprocess")))

    def run_exec(self, label: str) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = accounts.main(["exec", label, "--", "argument with spaces"])
        return code, output.getvalue(), error.getvalue()

    def test_every_label_loads_the_launch_folder_before_exec_on_both_platforms(self) -> None:
        envrc = self.folder.parent / ".envrc"
        _ = envrc.write_text("use flake\n")
        # The helper owns allowing this file and returns the resulting shell.
        shell = {"HOME": str(self.home), "PATH": str(self.root / "shell-bin"), "KEEP": "base",
                 "IN_NIX_SHELL": "impure", "FROM_SHELL": "yes", "CLAUDE_CONFIG_DIR": "wrong",
                 "CODEX_HOME": "wrong", "CLAUDE_CODE_OAUTH_TOKEN": "fixture-token"}
        arguments = ["argument with spaces"]
        for platform in ("linux", "darwin"):
            for label in ("claude", "claude1", "claude2", "claude3", "claude4", "units", "showrunners",
                          "codex1", "codex2", "workers"):
                with (self.subTest(platform=platform, label=label), patch.object(sys, "platform", platform),
                      patch.object(self.loader, "ensure", return_value=fixtures.FakeLoadedShell(shell, envrc)) as ensure,
                      patch.object(accounts, "resolve_program", return_value="/fixture/agent") as resolve,
                      patch.object(os, "execvpe") as launch):
                    self.assertEqual(self.run_exec(label), (0, "", ""))
                    ensure.assert_called_once_with(self.folder, os.environ)
                    launch.assert_called_once()
                    program, argv, environment = cast(tuple[str, list[str], dict[str, str]], launch.call_args.args)
                    self.assertEqual((program, argv), ("/fixture/agent", ["/fixture/agent", *arguments]))
                    self.assertEqual(environment["FROM_SHELL"], "yes")
                    self.assertNotIn("UNSET_BY_SHELL", environment)
                    if label.startswith("codex") or label == "workers":
                        self.assertEqual(environment["CODEX_HOME"], str(self.home / ".codex2"))
                        tool = "codex"
                    else:
                        if label == "claude":
                            self.assertNotIn("CLAUDE_CONFIG_DIR", environment)
                        else:
                            self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.home / ".claude4"))
                        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
                        tool = "claude"
                    resolve.assert_called_once_with(tool, environment=environment)
                    self.assertEqual(shell["CLAUDE_CONFIG_DIR"], "wrong")
                    self.assertNotIn("FROM_SHELL", os.environ)

    def test_loaded_shell_cannot_change_the_callers_home(self) -> None:
        shell = {**os.environ, "HOME": str(self.root / "project-home"), "IN_NIX_SHELL": "impure"}
        for label in ("claude", "claude4", "codex2", "workers"):
            with (self.subTest(label=label),
                  patch.object(self.loader, "ensure", return_value=fixtures.FakeLoadedShell(shell, self.folder / ".envrc")),
                  patch.object(accounts, "resolve_program", return_value="/fixture/agent"),
                  patch.object(os, "execvpe") as launch):
                self.assertEqual(self.run_exec(label), (0, "", ""))
                environment = cast(dict[str, str], launch.call_args.args[2])
                self.assertEqual(environment["HOME"], str(self.home))
                if label == "claude":
                    self.assertNotIn("CLAUDE_CONFIG_DIR", environment)
                self.assertEqual(shell["HOME"], str(self.root / "project-home"))

    def test_execute_only_program_is_returned_when_wrapper_check_cannot_read_it(self) -> None:
        for tool in ("claude", "codex"):
            program = fixtures.installed_program(self.root / "execute-only", tool)
            with (self.subTest(tool=tool), patch.object(Path, "open", side_effect=PermissionError("execute only"))):
                self.assertEqual(accounts.resolve_program(tool, environment={"PATH": str(Path(program).parent)}), program)

    def test_folder_without_envrc_execs_with_the_unchanged_base_environment(self) -> None:
        for label in ("claude", "claude4", "codex2", "workers"):
            with (self.subTest(label=label),
                  patch.object(self.loader, "ensure", return_value=fixtures.FakeNoEnvrc(dict(os.environ))) as ensure,
                  patch.object(accounts, "resolve_program", return_value="/fixture/agent"),
                  patch.object(os, "execvpe") as launch):
                self.assertEqual(self.run_exec(label), (0, "", ""))
                ensure.assert_called_once_with(self.folder, os.environ)
                environment = cast(dict[str, str], launch.call_args.args[2])
                self.assertEqual(environment["KEEP"], "base")
                self.assertEqual(environment["UNSET_BY_SHELL"], "old")
                self.assertNotIn("IN_NIX_SHELL", environment)
                self.assertFalse((self.folder / ".envrc").exists())

    def test_refused_envrc_exits_one_without_starting_an_agent_or_supervisor(self) -> None:
        envrc = self.folder.parent / ".envrc"
        _ = envrc.write_text("use flake\n")
        for label in ("claude", "claude4", "units", "showrunners", "codex2", "workers"):
            for reason in (f"nix_env: run direnv allow {envrc}", "nix_env: IN_NIX_SHELL is not set",
                           "nix_env: direnv timed out"):
                with (self.subTest(label=label, reason=reason), patch.object(sys.stdin, "isatty", return_value=True),
                      patch.object(self.loader, "ensure", return_value=fixtures.FakeShellRefusal(reason)),
                      patch.object(accounts, "resolve_program") as resolve, patch.object(os, "execvpe") as launch):
                    self.assertEqual(self.run_exec(label), (1, "", reason + "\n"))
                    resolve.assert_not_called()
                    launch.assert_not_called()

    def test_missing_helper_fails_only_exec_with_a_clear_error(self) -> None:
        with (patch.object(accounts, "import_module", side_effect=ImportError("fixture helper missing")), patch.object(os, "execvpe") as launch,
              patch.object(accounts, "status_report", return_value="fixture status")):
            self.assertEqual(self.run_exec("claude"), (1, "",
                "accounts: nix shell helper unavailable (scripts/lib/nix_env.py); update the shared scripts\n"))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(accounts.main(["status"]), 0)
            self.assertEqual(output.getvalue(), "fixture status\n")
            launch.assert_not_called()

    def test_program_is_resolved_from_the_loaded_shell_path(self) -> None:
        for label, tool in (("claude", "claude"), ("codex2", "codex")):
            program = fixtures.installed_program(self.root / "shell", tool)
            shell = {**os.environ, "PATH": str(Path(program).parent), "IN_NIX_SHELL": "impure"}
            with (self.subTest(label=label),
                  patch.object(self.loader, "ensure", return_value=fixtures.FakeLoadedShell(shell, self.folder / ".envrc")),
                  patch.object(os, "execvpe") as launch):
                self.assertEqual(self.run_exec(label), (0, "", ""))
                self.assertEqual(launch.call_args.args[:2], (program, [program, "argument with spaces"]))

    def test_exec_skips_nix_shell_wrappers_before_the_real_program(self) -> None:
        for label, tool in (("claude", "claude"), ("codex2", "codex")):
            wrapper = Path(fixtures.installed_program(self.root / "wrappers", tool))
            _ = wrapper.write_text('#!/bin/sh\nexec python3 /fixture/nix_env.py exec "$@"\n')
            program = fixtures.installed_program(self.root / "real", tool)
            shell = {**os.environ, "PATH": os.pathsep.join((str(wrapper.parent), str(Path(program).parent))),
                     "IN_NIX_SHELL": "impure"}
            with (self.subTest(label=label),
                  patch.object(self.loader, "ensure", return_value=fixtures.FakeLoadedShell(shell, self.folder / ".envrc")) as ensure,
                  patch.object(os, "execvpe") as launch):
                self.assertEqual(self.run_exec(label), (0, "", ""))
                ensure.assert_called_once_with(self.folder, os.environ)
                self.assertEqual(launch.call_args.args[:2], (program, [program, "argument with spaces"]))

    def test_wrapper_path_falls_back_to_installed_program_and_refuses_wrapper_only(self) -> None:
        wrapper = Path(fixtures.installed_program(self.root / "wrappers", "claude"))
        _ = wrapper.write_text('#!/bin/sh\nexec python3 -m lib.nix_env exec "$@"\n')
        program = Path(fixtures.installed_program(self.home, "claude"))
        shell = {**os.environ, "PATH": str(wrapper.parent)}
        with (patch.object(self.loader, "ensure", return_value=fixtures.FakeNoEnvrc(shell)),
              patch.object(os, "execvpe") as launch):
            self.assertEqual(self.run_exec("claude"), (0, "", ""))
            self.assertEqual(launch.call_args.args[0], str(program))
            launch.reset_mock()
            _ = program.write_text(wrapper.read_text())
            code, output, error = self.run_exec("claude")
            self.assertEqual((code, output), (2, ""))
            self.assertIn("claude program not found", error)
            launch.assert_not_called()


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

    def test_native_herdr_launches_reject_independent_account_selection(self) -> None:
        source = '''
def launch():
    selected = accounts.role_account("unit")
    herdr.open_tab("unit", "ad-hoc", "unit", cwd, named_account_environment(selected.dir))
    herdr.start_agent("unit", pane, kind="claude", agent="unit", agent_arguments=[])
'''
        problems = launch_account_violations(source, "fixture.py")
        self.assertTrue(any("selects with" in problem for problem in problems), problems)
        resolved = source.replace('accounts.role_account("unit")',
                                  'accounts.account_for_group_launch("unit", resume=False)')
        self.assertEqual(launch_account_violations(resolved, "fixture.py"), [])

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

    def test_shell_launches_without_any_account_selection_are_rejected(self) -> None:
        for path, source in (
                ("fix/style-eval-review-all.sh", 'claude --print --dangerously-skip-permissions -- "$prompt" > "$log" 2>&1'),
                ("berth_fix/launch.sh", 'exec env -u CLAUDE_CODE_MESSAGING_SOCKET \\\n    claude -n "$session_name" --permission-mode auto /berth_fix\n'
                 + 'open_window claude -n "$session_name" /berth_fix'),
                ("agents/agent_bg.sh", '"${CLAUDE_BIN}" "${launch_args[@]}"')):
            with self.subTest(path=path):
                self.assertTrue(any("no resolved account" in problem
                                    for problem in shell_launch_account_violations(source, path)))

    def test_embedded_python_binary_lookup_does_not_count_as_a_shell_launch(self) -> None:
        source = '''\
claude = os.environ.get("CLAUDE_BIN") or str(Path.home() / ".local/bin/claude")
subprocess.run([claude, "agents", "--json"], capture_output=True, text=True)
'''
        self.assertEqual(shell_launch_account_violations(source, "direct/implement.sh"), [])

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
