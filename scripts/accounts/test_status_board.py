"""Test the accounts board columns, lines, and tones for the Hana console Accounts page."""
from __future__ import annotations

from dataclasses import replace
from collections import Counter
from collections.abc import Callable, Generator
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
from pathlib import Path
from typing import Literal, cast, override
import unittest
from unittest.mock import PropertyMock, patch
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from scripts.accounts import accounts, next_moves, role_map, session_state
from scripts.accounts.home_test_support import ACCOUNT_MODEL_SETTINGS, ACCOUNT_STATUS_SETTINGS, scratch_account_home
from scripts.agents import remote
from scripts.herdr import herdr, pane_launch, worker_panes
from scripts.lib import conf, models_registry, transcripts
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message import sessions
from scripts.production import session_staff, staff
from scripts.whoami import account, agent_accounts

NOW = datetime(2026, 10, 16, 18, tzinfo=timezone.utc)
ZONE = ZoneInfo("America/New_York")


def row(label: str, roles: tuple[str, ...], counts: tuple[accounts.RoleSessionCounts | accounts.NoRoleCount, ...],
        weekly: float | None, resets: str | None, credits: float | Literal["unlimited"] | None, *,
        selected: tuple[str, ...] = (), use_credits: bool = False, read: bool = True) -> accounts.StatusRow:
    unavailable = accounts.StatusUnavailable("unavailable")
    return accounts.StatusRow(
        label=label, tool="claude", folder=f"~/.{label}", login="fixture@example.invalid", roles=roles,
        weekly_left_percent=unavailable if weekly is None else weekly,
        resets_at=unavailable if resets is None else datetime.fromisoformat(resets),
        credits=unavailable if credits is None else credits, on_credits="no", use_credits=use_credits,
        read_at=NOW - timedelta(minutes=1) if read else unavailable, read_outcome=agent_accounts.UsageReadOutcome.OK,
        attempted_at=NOW, role_counts=counts, selected_roles=selected)


def counts(busy: int, idle: int, paused: int, exited: int) -> accounts.RoleSessionCounts:
    return accounts.RoleSessionCounts(busy, idle, paused, exited)


def fixture() -> accounts.StatusReading:
    """A readable status fixture for the board: deliberately unsorted and mixed case."""
    rows = (
        row("Gamma", ("workers",), (counts(1, 2, 0, 1),), 74, "2026-10-24T18:00:00+00:00", None),
        row("beta", ("workers",), (counts(7, 5, 0, 0),), 20.2, "2026-10-17T18:00:00+00:00", 13446.2, read=False),
        row("Echo", ("showrunners", "unit directors"), (counts(0, 0, 0, 0), counts(0, 0, 0, 0)), None, None, None, read=False),
        row("aLpha", ("showrunners", "unit directors"), (counts(1, 0, 0, 1), counts(2, 1, 0, 0)), 60.4,
            "2026-10-16T18:00:00+00:00", "unlimited", selected=("showrunners",), use_credits=True),
        row("Foxtrot", ("spare",), (accounts.NoRoleCount(),), 90, "2026-10-22T18:00:00+00:00", None, use_credits=True),
        row("Charlie", ("workers",), (counts(0, 1, 0, 0),), 8.4, "2026-10-18T18:00:00+00:00", None),
        row("Delta", ("spare",), (accounts.NoRoleCount(),), 25, "2026-10-23T18:00:00+00:00", None),
    )
    return accounts.StatusReading(rows, (), (), ZONE, NOW)


def text(spans: list[accounts.BoardSpan]) -> str:
    return "".join(span["text"] for span in spans)


class StatusBoardTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = write_config_fixture(configuration / "accounts.conf",
                                 "session_model_cache_entries=2\n",
                                 defaults=ACCOUNT_STATUS_SETTINGS + ACCOUNT_MODEL_SETTINGS)
        _ = write_config_fixture(configuration / "statusline.conf", defaults="")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))

    def test_the_table_matches_the_plugin_columns_lines_and_totals(self) -> None:
        board = accounts.status_board(fixture())
        keys = [column["key"] for column in board["columns"]]
        self.assertEqual([column["heading"] for column in board["columns"]],
                         ["", "account", "left", "role", "busy", "idle", "paused", "exited", "total", "resets", "credits", "enabled"])
        lines = [[text(line["cells"][key]) for key in keys] for line in board["rows"]]
        self.assertEqual(lines, [
            ["●", "aLpha", "60%", "showrunners ✓", "1", "0", "0", "1", "2", "14:00", "unlimited", "yes"],
            ["", "", "", "unit directors", "2", "1", "0", "0", "3", "", "", ""],
            ["●", "beta", "20%", "workers", "7", "5", "0", "0", "12", "Sat 14:00", "13,446", ""],
            ["●", "Charlie", "8%", "workers", "0", "1", "0", "0", "1", "Sun 14:00", "", ""],
            ["●", "Delta", "25%", "spare", "", "", "", "", "", "23 Oct", "", ""],
            ["●", "Echo", "—", "showrunners", "0", "0", "0", "0", "0", "", "", ""],
            ["", "", "", "unit directors", "0", "0", "0", "0", "0", "", "", ""],
            ["●", "Foxtrot", "90%", "spare", "", "", "", "", "", "Thu 14:00", "", "yes"],
            ["●", "Gamma", "74%", "workers", "1", "2", "0", "1", "4", "24 Oct", "", ""],
            ["", "total", "", "", "11", "9", "0", "2", "22", "", "", ""],
        ])
        self.assertEqual([column["align"] for column in board["columns"]],
                         ["left", "left", "right", "left", *["right"] * 5, "right", "right", "left"])

    def test_only_selected_claude_group_lines_name_their_group_for_the_move_menu(self) -> None:
        rows = (row("claude1", ("units", "showrunners", "workers"), (counts(1, 0, 0, 0),) * 3, 50, None, None,
                    selected=("units", "showrunners")),
                row("claude2", ("units",), (counts(0, 1, 0, 0),), 70, None, None),
                replace(row("codex1", ("units",), (counts(0, 0, 0, 0),), 80, None, None, selected=("units",)), tool="codex"))
        board = accounts.status_board(accounts.StatusReading(rows, (), (), ZONE, NOW))
        self.assertEqual([(text(line["cells"]["roles"]), line.get("group")) for line in board["rows"]], [
            ("units ✓", accounts.BoardGroup(account="claude1", group="units")),
            ("showrunners ✓", accounts.BoardGroup(account="claude1", group="showrunners")),
            ("workers", None), ("units", None), ("units ✓", None), ("", None)])

    def test_status_uses_one_queue_read_for_the_upcoming_list_and_first_targets(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with scratch_account_home(home, {}, clear=True), patch.object(role_map, "is_role_map_host", return_value=True):
            first = next_moves.add_next_move("units", 1, "claude2")
            later = next_moves.add_next_move("units", 2, "claude3")
            workers = next_moves.add_next_move("workers", 1, "codex2")
            entries = (first, later, workers)
            expected = next_moves.render_next_moves(entries)
            for content in (next_moves.encode_next_moves(entries), "[]", "["):
                with self.subTest(content=content):
                    _ = next_moves.next_moves_file().write_text(content)
                    with (patch.object(accounts, "_read_status", return_value=fixture()),
                          patch.object(next_moves, "read_next_moves", wraps=next_moves.read_next_moves) as read):
                        result = accounts.read_status(source=accounts.StoredUsageOnly())
                    read.assert_called_once_with()
                    if content == "[":
                        self.assertIsInstance(result.first_next_moves, accounts.NextMovesUnavailable)
                        self.assertIn("Upcoming moves: unavailable", result.upcoming_moves[0])
                    elif content == "[]":
                        self.assertEqual(result.first_next_moves, accounts.FirstNextMoveTargets())
                        self.assertEqual(result.upcoming_moves, ("Upcoming moves: none",))
                    else:
                        self.assertEqual(result.first_next_moves, accounts.FirstNextMoveTargets({"units": "claude2", "workers": "codex2"}))
                        self.assertEqual(result.upcoming_moves, ("Upcoming moves:", *expected.splitlines()))
            with (patch.object(accounts, "_read_status", return_value=fixture()),
                  patch.object(role_map, "is_role_map_host", return_value=False),
                  patch.object(role_map, "read_role_map", return_value=role_map.RoleMap({}, "codex1", upcoming_moves=("saved moves",))),
                  patch.object(next_moves, "read_next_moves", side_effect=AssertionError("unexpected queue read"))):
                result = accounts.read_status(source=accounts.StoredUsageOnly())
            self.assertEqual(result.upcoming_moves, ("saved moves",))
            self.assertIsInstance(result.first_next_moves, accounts.NextMovesUnavailable)

    def test_tones_mark_access_and_low_quota(self) -> None:
        rows = {text(line["cells"]["label"]): line for line in accounts.status_board(fixture())["rows"]}
        self.assertEqual({label: line["cells"]["dot"][0]["tone"] for label, line in rows.items() if line["cells"]["dot"]},
                         {"aLpha": "success", "beta": "error", "Charlie": "success", "Delta": "success", "Echo": "error",
                          "Foxtrot": "success", "Gamma": "success"})
        self.assertEqual({label: line["cells"]["left"][0]["tone"] for label, line in rows.items() if line["cells"]["left"]},
                         {"aLpha": "default", "beta": "warning", "Charlie": "error", "Delta": "default", "Echo": "default",
                          "Foxtrot": "default", "Gamma": "default"})
        self.assertEqual(rows["total"]["cells"]["label"], [accounts.BoardSpan(text="total", tone="default", kind="bold")])

    def test_quota_boundaries_color_the_left_cell_and_keep_the_dot(self) -> None:
        rows = tuple(row(f"Level {value:02}", ("workers",), (counts(0, 0, 0, 0),), value, None, None) for value in (9, 10, 24, 25))
        board = accounts.status_board(accounts.StatusReading(rows, (), (), ZONE, NOW))
        cells = [line["cells"] for line in board["rows"][:-1]]
        self.assertEqual([(text(cell["left"]), cell["left"][0]["tone"]) for cell in cells],
                         [("9%", "error"), ("10%", "warning"), ("24%", "warning"), ("25%", "default")])
        self.assertEqual({cell["dot"][0]["tone"] for cell in cells}, {"success"})

    def test_reset_text_is_the_clock_the_weekday_or_the_date_in_the_zone(self) -> None:
        for at, expected in (("2026-10-17T02:00:00+00:00", "22:00"),  # The same New York day, the next UTC day.
                             ("2026-10-10T18:00:00+00:00", "Sat 14:00"), ("2026-10-22T18:00:00+00:00", "Thu 14:00"),
                             ("2026-10-09T18:00:00+00:00", "9 Oct"), ("2026-10-24T18:00:00+00:00", "24 Oct")):
            with self.subTest(at=at):
                board = accounts.status_board(accounts.StatusReading(
                    (row("Low", ("workers",), (counts(0, 0, 0, 0),), 50, at, None),), (), (), ZONE, NOW))
                self.assertEqual(text(board["rows"][0]["cells"]["resets"]), expected)

    def test_zero_is_a_value_and_spare_has_no_counts(self) -> None:
        reading = accounts.StatusReading((row("Zero", ("spare",), (accounts.NoRoleCount(),), 0, None, 0),), (), (), ZONE, NOW)
        cells = accounts.status_board(reading)["rows"][0]["cells"]
        self.assertEqual([text(cells[key]) for key in ("left", "roles", "busy", "total", "credits")], ["0%", "spare", "", "", "0"])

    def test_each_worker_role_keeps_its_own_counts_and_the_total_adds_them(self) -> None:
        reading = accounts.StatusReading((row("Watch", ("workers", "reviewers"), (counts(2, 3, 0, 0), counts(1, 4, 0, 0)),
                                              50, None, None),), (), (), ZONE, NOW)
        board = accounts.status_board(reading)
        keys = ("roles", "busy", "idle", "paused", "exited", "total")
        self.assertEqual([[text(line["cells"][key]) for key in keys] for line in board["rows"]],
                         [["workers", "2", "3", "0", "0", "5"], ["reviewers", "1", "4", "0", "0", "5"], ["", "3", "7", "0", "0", "10"]])

    def test_the_board_is_stale_at_the_next_local_midnight(self) -> None:
        self.assertEqual(accounts.status_board(fixture())["stale_at"], "2026-10-17T04:00:00+00:00")
        late = accounts.StatusReading((), (), (), ZONE, datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc))
        # 23:30 EDT on 31 Oct; the clocks go back at 02:00 on 1 Nov, so midnight is still EDT.
        self.assertEqual(accounts.status_board(late)["stale_at"], "2026-11-01T04:00:00+00:00")

    def test_no_accounts_is_an_empty_table_and_a_stale_read_is_red(self) -> None:
        self.assertEqual(accounts.status_board(accounts.StatusReading((), (), (), ZONE, NOW))["rows"], [])
        stale = replace(fixture().rows[0], read_at=NOW - timedelta(hours=1))
        board = accounts.status_board(accounts.StatusReading((stale,), (), (), ZONE, NOW))
        self.assertEqual(board["rows"][0]["cells"]["dot"][0]["tone"], "error")

    def test_local_model_counts_use_the_turns_of_each_staff_kind(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        claude_home, codex_home = root / ".claude1", root / ".codex1"
        workspace = staff.PresentWorktree(remote.local_machine(), root / "worktree")
        def make_member(kind: staff.Kind, name: str, parent: staff.StaffRoot | staff.ReportsTo = staff.STAFF_ROOT) -> staff.StaffRow:
            identity = staff.StaffId(str(uuid4()))
            return staff.StaffRow(identity, kind, name, parent, current_session=identity)

        unit = replace(make_member("unit", "fixture-unit"), worktree=workspace)
        parent = staff.ReportsTo(unit.id)
        claude_roles = (unit, make_member("showrunner", "fixture-showrunner"),
                        make_member("producer", "fixture-claude-producer"), make_member("worker", "fixture-claude-worker", parent))
        millisecond = int(NOW.timestamp() * 1000)
        thread_prefix = (millisecond << 80) | (7 << 76) | (2 << 62)
        codex_roles = tuple(replace(member, current_session=staff.StaffId(str(UUID(int=thread_prefix + index))))
                            for index, member in enumerate((
                                make_member("producer", "fixture-codex-producer"),
                                make_member("worker", "fixture-codex-worker", parent),
                                make_member("worker", "fixture-designer", parent),
                                make_member("worker", "fixture-reviewer", parent))))
        codex_roles = (codex_roles[0], codex_roles[1],
                       replace(codex_roles[2], role=staff.KnownWorkerRole("astra-designer")),
                       replace(codex_roles[3], role=staff.KnownWorkerRole("astra-reviewer")))
        open_worker = replace(make_member("worker", "fixture-open-worker", parent),
                              current_session=staff.StaffId(str(UUID(int=thread_prefix + len(codex_roles)))))
        membership = staff.StaffSnapshot((*claude_roles, *codex_roles, open_worker))
        project = claude_home / "projects" / "previous-worktree"
        project.mkdir(parents=True)
        claude_models = ("claude-opus-5-5", "claude-opus-5-5", "claude-fable-5-1", "claude-opus-5-5")
        efforts = ("xhigh", "max", "high", "medium")
        for member, model, effort in zip(claude_roles, claude_models, efforts):
            lines = (
                {"type": "assistant", "message": {"model": "claude-sonnet-4-6"}, "effort": "low"},
                {"type": "assistant", "message": {"model": model}, "effort": effort},
                {"type": "assistant", "message": {"model": "<synthetic>"}},
            )
            _ = (project / f"{member.current_session}.jsonl").write_text(
                "\n".join(json.dumps(line) for line in lines) + '\n{"type":"assistant"')
        codex_models = ("gpt-6.2-sol", "gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.2-astra")
        folder = codex_home / "sessions" / NOW.strftime("%Y/%m/%d")
        folder.mkdir(parents=True)
        for member, model, effort in zip(codex_roles, codex_models, efforts):
            events = ({"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "effort": "low"}},
                      {"type": "turn_context", "payload": {"model": model, "effort": effort}})
            _ = (folder / f"rollout-fixture-{member.current_session}.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events) + "\n")
        live = [sessions.SessionRecord(pid=990001 + index, sessionId=member.current_session, name=member.name,
            messagingSocketPath="", updatedAt=1, tmux="", formerNames=[], cwd=str(workspace.path), status="busy")
            for index, member in enumerate(claude_roles)]
        backends = {member.id: staff.CodexBackend(codex_home) for member in (*codex_roles[1:], open_worker)}
        read = cast(Callable[..., accounts.LocalRoleSessions], getattr(accounts, "_read_local_role_sessions"))
        def settings(member: staff.StaffRow) -> models_registry.LaunchSettings:
            return models_registry.LaunchSettings(
                "claude" if member == claude_roles[2] else "codex", "different-launch-model", "low", "")

        open_pid = 990021
        def process_options(pid: int) -> accounts.SessionModelOptions:
            self.assertEqual(pid, open_pid)
            return accounts.SessionModelOptions("gpt-6.2-sol", "low")

        with (scratch_account_home(root, {}, clear=True),
              patch.object(staff, "local_snapshot", return_value=membership),
              patch.object(staff.StaffSnapshot, "backends", new_callable=PropertyMock, return_value=backends),
              patch.object(sessions, "running_sessions", return_value=live),
              patch.object(sessions, "sessions_dir", return_value=root / "registry"),
              patch.object(pane_launch, "record_directory", return_value=root / "launches"),
              patch.object(session_staff, "pane_parents", return_value=(unit,)),
              patch.object(worker_panes, "observe_parents", return_value={
                  unit.id: [worker_panes.OpenWorker(open_worker, "w1:p1", "fixture", herdr.AgentStatus.IDLE)]}),
              patch.object(session_state, "running_detached_work", return_value=session_state.NO_DETACHED_WORK),
              patch.object(accounts, "producer_settings", side_effect=settings),
              patch.object(accounts, "_session_counts_machine", return_value=workspace.machine),
              patch.object(accounts, "_session_config_dir", return_value=account.ConfiguredClaudeFolder(claude_home)),
              patch.object(accounts, "_local_codex_processes", return_value={
                  **{member.current_session: accounts.CodexRoleProcess(990011 + index, "idle")
                     for index, member in enumerate(codex_roles)},
                  open_worker.current_session: accounts.CodexRoleProcess(open_pid, "idle")}),
              patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(codex_home)),
              patch.object(accounts, "_model_process_options", side_effect=process_options)):
            observed = read(accounts.GroupAccounts(), include_models=True)
        expected = {
            (home, member.current_session): accounts.ObservedSessionModel(model, effort)
            for home, members, models in ((claude_home, claude_roles, claude_models), (codex_home, codex_roles, codex_models))
            for member, model, effort in zip(members, models, efforts)
        }
        expected[codex_home, open_worker.current_session] = accounts.ObservedSessionModel("gpt-6.2-sol", "low")
        self.assertEqual(observed.models, expected)
        self.assertEqual({(address[0], identity) for address, members in observed.counts.items()
                          for identity in members}, set(expected))

    def test_model_aliases_use_the_catalog_and_missing_evidence_stays_unknown(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        registry = root / "models.conf"
        _ = registry.write_text("[claude.families]\nopus=opus\n[codex.families]\nastra=gpt-6.2-astra\n")
        catalog = root / "cache/model-catalog"
        catalog.mkdir(parents=True)
        _ = (catalog / "fixture-cc.json").write_text(json.dumps({
            "catalog": {"config": {"provider_alias_targets": {"opus": {"default": "claude-opus-5-5"}}}}}))
        project = root / "projects" / "old-project"
        project.mkdir(parents=True)
        (project / "fixture.jsonl").touch()
        read = accounts.SessionModelReader()
        for family, model, effort, expected in (
            ("claude", "opus", "max", accounts.ObservedSessionModel("claude-opus-5-5", "max")),
            ("codex", "astra", "ultra", accounts.ObservedSessionModel("gpt-6.2-astra", "ultra")),
            ("claude", "opus", "", accounts.SessionModelUnknown()),
            ("codex", "short-unknown", "high", accounts.SessionModelUnknown()),
        ):
            with (self.subTest(family=family, model=model, effort=effort),
                  patch.dict(os.environ, {"MODELS_CONFIG_FILE": str(registry)}),
                  patch.object(accounts, "_model_process_options", return_value=accounts.SessionModelOptions(model, effort))):
                thread = UUID(int=(int(NOW.timestamp() * 1000) << 80) | (7 << 76) | (2 << 62))
                result = (read.claude(root, "fixture", pid=1) if family == "claude"
                          else read.codex(root, str(thread), pid=1))
                self.assertEqual(result, expected)

    def test_claude_model_finds_the_transcript_after_a_change_of_directory(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        project = home / "projects" / "old-project"
        project.mkdir(parents=True)
        _ = (project / "resumed.jsonl").write_text(
            '{"type":"assistant","message":{"model":"claude-opus-5-5"},"effort":"high"}\n')
        with patch.object(accounts, "_model_process_options",
                          return_value=accounts.SessionModelOptions("claude-fable-5-1", "low")) as launch:
            reader = accounts.SessionModelReader()
            self.assertEqual(reader.claude(home, "resumed", pid=1),
                             accounts.ObservedSessionModel("claude-opus-5-5", "high"))
            self.assertEqual(reader.claude(home, "absent", pid=1),
                             accounts.SessionModelUnknown())
            launch.assert_not_called()

    def test_model_refresh_reuses_the_result_for_an_unchanged_compressed_transcript(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        project = home / "projects" / "-old-project"
        project.mkdir(parents=True)
        source = project / "resumed.jsonl"
        archive = source.with_suffix(".jsonl.zst")
        turn = '{"type":"assistant","message":{"model":"claude-opus-5-5"},"effort":"high"}\n'
        _ = source.write_text(turn + json.dumps({"type": "user", "text": "x" * (128 * 1024)}) + "\n")
        with archive.open("wb") as stream:
            _ = transcripts.compress_transcript(source, stream)
        source.unlink()
        with (patch.dict(os.environ, {"HOME": str(home), "XDG_CACHE_HOME": str(home / "cache")}),
              patch("scripts.lib.transcripts.tempfile.TemporaryFile", side_effect=AssertionError("archive temp file"))):
            self.assertEqual(accounts.SessionModelReader().claude(home, "resumed"),
                             accounts.ObservedSessionModel("claude-opus-5-5", "high"))
            with patch("scripts.lib.transcripts.subprocess.Popen", side_effect=AssertionError("archive reopened")):
                self.assertEqual(accounts.SessionModelReader().claude(home, "resumed"),
                                 accounts.ObservedSessionModel("claude-opus-5-5", "high"))
            modified = archive.stat().st_mtime_ns
            _ = source.write_text(turn.replace("claude-opus-5-5", "claude-fable-5-1"))
            with archive.open("wb") as stream:
                _ = transcripts.compress_transcript(source, stream)
            source.unlink()
            os.utime(archive, ns=(modified, modified + 1_000_000_000))
            self.assertEqual(accounts.SessionModelReader().claude(home, "resumed"),
                             accounts.ObservedSessionModel("claude-fable-5-1", "high"))

    def test_model_cache_evicts_the_least_recently_used_result(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        project = home / "projects" / "fixture"
        project.mkdir(parents=True)
        for identity in ("first", "second", "third"):
            source = project / f"{identity}.jsonl"
            _ = source.write_text('{"type":"assistant","message":{"model":"claude-opus-5-5"},"effort":"high"}\n')
            with source.with_suffix(".jsonl.zst").open("wb") as stream:
                _ = transcripts.compress_transcript(source, stream)
            source.unlink()
        expected = accounts.ObservedSessionModel("claude-opus-5-5", "high")
        paths: list[Path] = []
        decode = transcripts.compressed_transcript_lines

        def read(path: Path) -> Generator[bytes]:
            paths.append(path)
            return decode(path)

        with patch.object(transcripts, "compressed_transcript_lines", side_effect=read):
            for identity in ("first", "second", "first", "third", "first", "second"):
                self.assertEqual(accounts.SessionModelReader().claude(home, identity), expected)
        self.assertEqual(paths, [project.resolve() / f"{identity}.jsonl.zst"
                                 for identity in ("first", "second", "third", "second")])

    def test_codex_model_uses_a_recorded_turn_and_only_uses_process_options_before_any_turn(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        folder = root / "sessions" / NOW.strftime("%Y/%m/%d")
        folder.mkdir(parents=True)
        thread = UUID(int=(int(NOW.timestamp() * 1000) << 80) | (7 << 76) | (2 << 62))
        rollout = folder / f"rollout-fixture-{thread}.jsonl"
        turn = json.dumps({"type": "turn_context", "payload": {"model": "gpt-6.2-astra", "effort": "xhigh"}})
        options = accounts.SessionModelOptions("gpt-6.2-sol", "high")
        for label, transcript, compressed, expected in (
            ("hidden turn", turn + "\n" + json.dumps({"type": "response_item", "text": "x" * (128 * 1024)}),
             False, accounts.ObservedSessionModel("gpt-6.2-astra", "xhigh")),
            ("missing effort", json.dumps({"type": "turn_context", "payload": {"model": options.model}}),
             False, accounts.SessionModelUnknown()),
            ("before first turn", '{"type":"session_meta"}', False,
             accounts.ObservedSessionModel(options.model, options.effort)),
            ("compressed turn", turn, True, accounts.ObservedSessionModel("gpt-6.2-astra", "xhigh")),
        ):
            with (self.subTest(case=label), patch.object(accounts, "_model_process_options", return_value=options),
                  patch.dict(os.environ, {"HOME": str(root / "home"), "XDG_CACHE_HOME": str(root / "cache")})):
                _ = rollout.write_text(transcript + "\n")
                if compressed:
                    with rollout.with_suffix(".jsonl.zst").open("wb") as stream:
                        _ = transcripts.compress_transcript(rollout, stream)
                    rollout.unlink()
                self.assertEqual(accounts.SessionModelReader().codex(root, str(thread), pid=1), expected)
                self.assertFalse((root / "cache").exists())
                self.assertFalse((root / "home").exists())

    def test_model_board_does_not_use_a_host_override_as_a_model_color(self) -> None:
        model = accounts.ObservedSessionModel("gpt-6.2-sol.natedev", "high")
        entry = replace(row("codex1", ("workers",), (counts(1, 0, 0, 0),), 60, None, None),
                        models_by_role={"workers": Counter({model: 1})})
        reading = accounts.StatusReading((entry,), (), (), ZONE, NOW)
        table = accounts.models_board(reading, {f"model.{model.model}": "#123456"})
        self.assertEqual(table.rows[0]["cells"]["model"], [{"text": model.model, "tone": "default", "kind": "bold"}])

    def test_model_board_uses_a_host_color_without_a_base_setting(self) -> None:
        config = Path(self.enterContext(tempfile.TemporaryDirectory()))
        model = accounts.ObservedSessionModel("gpt-6.2-sol", "high")
        key = f"model.{model.model}"
        colors = config / "statusline.conf"
        _ = colors.write_text(f"{key}.fixture=#123456\n{key}.peer=#abcdef\n")
        _ = (config / "machines.conf").write_text("aliases=fixture peer\n")
        entry = replace(row("codex1", ("workers",), (counts(1, 0, 0, 0),), 60, None, None),
                        models_by_role={"workers": Counter({model: 1})})
        reading = accounts.StatusReading((entry,), (), (), ZONE, NOW)
        with (patch.dict(os.environ, {"CONF_DIR": str(config)}),
              patch("socket.gethostname", return_value="fixture.example.invalid")):
            _, value = conf.effective_line(colors, key)
            table = accounts.models_board(reading, accounts.role_colors())
        self.assertEqual(value, "#123456")
        self.assertEqual(table.rows[0]["cells"]["model"], [
            {"text": model.model, "tone": "default", "kind": "bold", "color": value, "color_key": key},
        ])

    def test_model_rows_share_the_account_total_with_current_and_older_peer_counts(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        local_model = accounts.ObservedSessionModel("claude-opus-5-5", "high")
        reading = accounts.StatusReading((replace(
            row("claude1", ("units",), (counts(1, 1, 0, 0),), 60, None, None),
            models_by_role={"units": Counter({local_model: 1})}),), (), (), ZONE, NOW)
        change = cast(Callable[..., tuple[tuple[accounts.StatusRow, ...], tuple[str, ...]]],
                      getattr(accounts, "_machine_role_counts"))
        publish = cast(Callable[..., accounts.MachineSessionCounts], getattr(accounts, "_session_counts_document"))
        folder = home / ".claude1"
        cache = home / "peer.json"
        with (patch.object(remote, "other_machine", return_value="mac"),
              patch.object(accounts, "_session_counts_cache", return_value=cache),
              patch.object(accounts, "_start_session_counts_fetch", return_value=True)):
            for current in (True, False):
                with self.subTest(current=current):
                    evidence = {(folder, "runner"): accounts.ObservedSessionModel("claude-opus-5-5", "low"),
                                (folder, "producer"): accounts.ObservedSessionModel("claude-fable-5-1", "high")}
                    document = publish({(folder, "showrunners"): {"runner": "paused"},
                                        (folder, "producers"): {"producer": "exited"}}, models=evidence if current else {})
                    document.update(machine="mac", at=NOW.isoformat())
                    _ = cache.write_text(json.dumps(document))
                    rows, problems = change(reading.rows, NOW)
                    self.assertEqual(problems, ())
                    both = replace(reading, rows=rows)
                    table = accounts.models_board(both, {"model.claude-opus-5-5": "#e08a5c"})
                    self.assertEqual([[text(line["cells"][key]) for key in
                                       ("model", "effort", *accounts.MODEL_COUNT_COLUMNS)] for line in table.rows],
                                     ([["claude-fable-5-1", "high", "1", "0", "0", "0", "1"],
                                       ["claude-opus-5-5", "low", "0", "1", "0", "0", "1"],
                                       ["", "high", "0", "0", "1", "0", "1"],
                                       ["unknown", "-", "0", "0", "1", "0", "1"]]
                                      if current else [["claude-opus-5-5", "high", "0", "0", "1", "0", "1"],
                                                       ["unknown", "-", "1", "1", "1", "0", "3"]])
                                     + [["total", "", "1", "1", "2", "0", "4"]])
                    self.assertEqual(text(table.rows[-1]["cells"]["total"]),
                                     text(accounts.status_board(both)["rows"][-1]["cells"]["total"]))


if __name__ == "__main__":
    _ = unittest.main()
