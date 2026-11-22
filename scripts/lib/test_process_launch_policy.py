"""Require a guardian for isolated launches in tests."""
from __future__ import annotations

import ast
from collections import Counter
import os
from pathlib import Path
from typing import NamedTuple
import unittest


class LaunchSite(NamedTuple):
    path: str
    scope: str
    call: str


class LaunchException(NamedTuple):
    site: LaunchSite
    reason: str


ALLOWED_LAUNCHES = (
    LaunchException(LaunchSite("agents/test_stop_resume.py", "test_own_empty_pane_executes_the_saved_launch", "os.fork"),
                    "The child inherits the patched exec boundary and the parent reaps it."),
    LaunchException(LaunchSite("aws_target/test_hana_runner.py",
                               "test_command_deadline_leaves_a_live_command_for_the_unit_to_stop", "subprocess.Popen"),
                    "The child inherits the readiness descriptor. The parent closes its input and reaps it."),
    LaunchException(LaunchSite("build_queue/admission_test_support.py", "kill_socket_server", "os.fork"),
                    "The fixture kills and reaps the bound server to leave its socket inode."),
    LaunchException(LaunchSite("build_queue/test_queue_place.py", "test_holder_signals_release_and_restore_handlers", "os.fork"),
                    "The parent signals and reaps the holder with its inherited admission fixture."),
    LaunchException(LaunchSite("build_queue/test_queue_place.py", "test_start_window_interrupts_leave_atomic_records_for_the_next_start", "os.fork"),
                    "The parent reaps the holder after interrupting its inherited launch hooks."),
    LaunchException(LaunchSite("build_queue/test_queue_place.py", "test_owner_gone_keeps_a_populated_scope_then_finalizes_once_when_empty", "os.fork"),
                    "The parent reaps the killed holder while its scope remains populated."),
    LaunchException(LaunchSite("build_queue/test_queue_place.py", "assert_scope_survives_holder_death", "os.fork"),
                    "The parent reaps the holder and stops the scope that survives it."),
    LaunchException(LaunchSite("build_queue/test_queue_place.py", "test_holder_death_before_scope_publication_prevents_program_execution", "os.fork"),
                    "The parent kills and reaps the holder at the inherited scope barrier."),
    LaunchException(LaunchSite("build_queue/test_queue_place.py", "test_int_before_scope_publication_stops_the_published_scope_with_a_live_caller", "os.fork"),
                    "The parent signals and reaps the holder at the inherited scope barrier."),
    LaunchException(LaunchSite("copies/remote_test_support.py", "__init__", "os.fork"),
                    "The claim server inherits the warm imports and the client closes and reaps it."),
    LaunchException(LaunchSite("direct/test_board_process_start.py", "test_zombie_leader_matches_for_cleanup_but_is_stale_for_step_records", "subprocess.Popen"),
                    "The test observes the unreaped session leader before killing its group and reaping it."),
    LaunchException(LaunchSite("lib/stop_injection_test_support.py", "stop_in_child", "os.fork"),
                    "The parent reaps the child that exits at an inherited fault point."),
    LaunchException(LaunchSite("mac_test/test_mac_gate.py", "sleeping_process", "subprocess.Popen"),
                    "The test records the process as a session leader. The parent registers kill and reap cleanup."),
    LaunchException(LaunchSite("message/test_process_test_support.py", "test_cleanup_finds_a_detached_child_before_its_pid_is_recorded", "subprocess.Popen"),
                    "The cleanup under test finds and reaps this raw child without a recorded PID."),
    LaunchException(LaunchSite("message/test_send_delivery.py", "stopped_sender", "os.fork"),
                    "The parent kills and reaps the sender at the inherited delivery checkpoint."),
    LaunchException(LaunchSite("style/test_bulk_rename.py", "stop_process", "os.fork"),
                    "The parent reaps the child that exits inside an inherited file operation."),
    LaunchException(LaunchSite("style/test_style_ledger_tickets.py", "test_batch_owner_migration_reruns_after_process_loss_before_version_write", "os.fork"),
                    "The parent reaps the child that exits at the inherited SQLite fault point."),
)


def bare_launches(path: str, tree: ast.Module) -> list[tuple[LaunchSite, int]]:
    nodes = list(ast.walk(tree))
    parents = {child: node for node in nodes for child in ast.iter_child_nodes(node)}
    os_modules = {"os"}
    os_calls: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            os_modules.update(alias.asname or alias.name for alias in node.names if alias.name == "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            os_calls.update((alias.asname or alias.name, alias.name) for alias in node.names
                            if alias.name in {"fork", "setsid"})

    launches: list[tuple[LaunchSite, int]] = []
    for call in nodes:
        if not isinstance(call, ast.Call):
            continue
        function = call.func
        os_call = ""
        if isinstance(function, ast.Name):
            os_call = os_calls.get(function.id, "")
        elif isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            if function.value.id in os_modules and function.attr in {"fork", "setsid"}:
                os_call = function.attr
        if os_call:
            name = f"os.{os_call}"
        elif any(keyword.arg == "process_group" or (
                keyword.arg == "start_new_session" and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True) for keyword in call.keywords):
            # Mock expectations and mirror metadata do not start a process.
            if isinstance(function, ast.Attribute) and function.attr in {
                "assert_called_once_with", "assert_called_with", "assert_any_call",
            }:
                continue
            name = ast.unparse(function)
            if name == "copies.mark_mirror":
                continue
        else:
            continue

        scope: ast.AST = call
        while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents[scope]
        function_name = scope.name if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
        launches.append((LaunchSite(path, function_name, name), call.lineno))
    return launches


class ProcessLaunchPolicyTests(unittest.TestCase):
    def test_isolated_test_launches_have_a_guardian(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        allowed = Counter(entry.site for entry in ALLOWED_LAUNCHES)
        failures: list[str] = []
        for directory, _, names in os.walk(scripts):
            for name in names:
                if not (name.startswith("test_") and name.endswith(".py") or name.endswith("_test_support.py")):
                    continue
                path = Path(directory) / name
                relative = path.relative_to(scripts).as_posix()
                for site, line in bare_launches(relative, ast.parse(path.read_bytes(), filename=str(path))):
                    if allowed[site]:
                        allowed[site] -= 1
                    else:
                        failures.append(f"scripts/{relative}:{line}: {site.call} starts an unowned process. "
                                        + "Use scripts.lib.process_supervisor.launch in a managed scope.")
        for site, remaining in allowed.items():
            if remaining:
                failures.append(f"scripts/{site.path}: remove the unused exception for {site.scope}: {site.call}.")
        self.assertFalse(failures, "\n".join(sorted(failures)))


if __name__ == "__main__":
    _ = unittest.main()
