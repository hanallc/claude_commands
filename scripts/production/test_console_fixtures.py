"""Serve fixture pages and run probes inside the console processes."""
from __future__ import annotations

from base64 import urlsafe_b64encode
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast
import json
import os
import sqlite3
import sys
from unittest.mock import patch

from scripts.disks import disks_page
from scripts.lib import conf, user_zone
from scripts.review_links import review_link
from scripts.statusline import rebuild_row


def first_page() -> str:
    return "/fixture/first.html"


def page_with_live_setup() -> str:
    flag = Path(os.environ["CONSOLE_TEST_SETUP_FLAG"])
    if flag.exists():
        raise ValueError("The fixture input is unavailable.")
    if Path.cwd().name == "candidate":
        raise ValueError("The candidate input failed on retry.")
    return second_page()


def second_page() -> str:
    return "/fixture/second.html"


def base_first_page() -> str:
    return "/fixture/base-first.html"


def base_second_page() -> str:
    return "/fixture/base-second.html"


def rebuilds_page() -> str:
    return "/fixture/rebuilds.html"


def legacy_server_page() -> str:
    from scripts.review_links import page_frame

    if 'data-status-component="images.refresh"' not in page_frame.title("Images", "images"):
        raise ValueError("The base server did not assign the Images component.")
    return first_page()


def failed_status(component: str, reason: str, *, age: int = 1) -> str:
    from html import escape

    attribute = f' data-status-component="{component}"' if component else ""
    return (f'<p data-page-status data-state="Failed"{attribute}>Failed '
            f'<span data-status-age>{age} min ago</span> '
            f'<span data-status-reason>{escape(reason)}</span></p>')


class FixtureModelCache(Protocol):
    def read(self) -> str: ...
    def save(self) -> None: ...


def barrier_probes(process: str) -> bytes:
    from scripts.accounts import accounts, role_map

    outside = Path(os.environ["CONSOLE_TEST_OUTSIDE"])
    model_cache = cast(Callable[[Path], FixtureModelCache], getattr(accounts, "_PersistentModelScans"))
    cache = model_cache(outside / "session-models.json")
    assert cache.read() == "Model cache fixture."
    cache.save()
    source = conf.read("accounts").source
    assert source == conf.directory() / "accounts.conf"
    assert source.exists() and source.stat().st_size > 0
    assert role_map.groups_file().is_relative_to(Path.home())
    assert (outside / "open.txt").exists()
    assert (outside / "open.txt").stat().st_size == 4
    assert (outside / "read_bytes.txt").read_bytes() == b"live"
    assert Path(os.fspath(outside / "fspath.txt")).read_text() == "live"
    assert os.listdir(outside / "list") == []
    outside_link = Path(os.environ["CONSOLE_TEST_OUTSIDE_LINK"])
    workspace = outside_link.readlink().parent
    inside_link = workspace / "inside-link"
    directory = os.open(outside, os.O_RDONLY)
    workspace_fd = os.open(workspace, os.O_RDONLY)
    read_log = Path(os.environ["CONF_READ_LOG"])
    append = os.O_WRONLY | os.O_APPEND
    if process == "server":
        read_log_fd = os.open(read_log, append | os.O_CREAT)
    else:
        read_log_fd = os.open(os.fsencode(read_log.name), append, dir_fd=directory)
    try:
        _ = os.write(read_log_fd, f"{process} fixture read\n".encode())
    finally:
        os.close(read_log_fd)
    target = outside / "open.txt"
    databases = outside / "outside.db"
    readonly = databases.as_uri() + "?mode=ro"

    def connect_and_attach() -> None:
        with sqlite3.connect(":memory:") as connection:
            _ = connection.execute("ATTACH DATABASE ? AS outside", (str(databases),))

    def probe_xattr(operation: str, *arguments: object) -> None:
        function = cast(Callable[..., None] | None, getattr(os, operation, None))
        if function is None:
            sys.audit(f"os.{operation}", str(target), *arguments)
        else:
            function(target, *arguments)

    operations: tuple[Callable[[], object], ...] = (
        lambda: (outside / "write.txt").write_text("test"),
        lambda: os.open("write-at.txt", os.O_WRONLY | os.O_CREAT, dir_fd=directory),
        lambda: os.unlink("open.txt", dir_fd=directory),
        lambda: (outside / "list").rmdir(),
        lambda: (outside / "write-dir").mkdir(),
        lambda: (outside / "open.txt").chmod(0o600),
        lambda: os.utime(outside / "open.txt", (10, 10)),
        lambda: target.replace(outside / "renamed.txt"),
        lambda: os.chown(target, os.getuid(), os.getgid()),
        lambda: os.chown("open.txt", os.getuid(), os.getgid(), dir_fd=directory),
        lambda: probe_xattr("setxattr", b"user.console-check", b"data", 0),
        lambda: probe_xattr("removexattr", b"user.console-check"),
        lambda: sqlite3.connect(databases),
        lambda: sqlite3.connect(readonly, uri=True),
        connect_and_attach,
        lambda: outside_link.unlink(),
        lambda: inside_link.write_text("outside write"),
        lambda: os.open(b"inside-link", os.O_WRONLY | os.O_TRUNC, dir_fd=workspace_fd),
        lambda: os.open(read_log, append | os.O_TRUNC),
        lambda: os.open(read_log.name, append | os.O_TRUNC, dir_fd=directory),
        lambda: os.open(read_log, os.O_WRONLY),
        lambda: os.open(read_log, os.O_RDWR | os.O_APPEND),
        lambda: os.truncate(read_log, 0),
        lambda: read_log.unlink(),
        lambda: read_log.rename(workspace / "moved-read-log"),
        lambda: os.open(workspace / "read-log-link", append),
        lambda: os.link(read_log, workspace / "hard-read-log"),
    )
    for operation in operations:
        try:
            _ = operation()
        except PermissionError:
            pass
        else:
            raise AssertionError("The console allowed a write outside the workspace.")
    os.close(directory)
    descriptor = os.open(b"allowed.bin", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, dir_fd=workspace_fd)
    with os.fdopen(descriptor, "wb") as allowed:
        _ = allowed.write(b"inside write")
    os.close(workspace_fd)
    assert (workspace / "allowed.bin").read_bytes() == b"inside write"
    return b"<!-- Outside writes denied -->"


def missing_review_config_page() -> str:
    from scripts.production import change_routing

    sources: list[Path] = []
    root = change_routing.review_root(sources=sources)
    assert len(sources) == 1
    recovered = conf.read("change_review").origin("review_links_directory", str)
    assert isinstance(recovered, conf.RecoveredValue) and "No such file" in recovered.reason
    assert b"Page ready" in (root / "fixture/first.html").read_bytes()
    return first_page()


def live_review_delivery_page() -> str:
    from scripts.production import change_routing, delivery_state

    review = Path(os.environ["CONSOLE_TEST_REVIEW_LIST"])
    path = change_routing.delivery_path(change_routing.review_root(), review, "A1")
    delivery = change_routing.delivery_slots(path, strict=True)
    assert isinstance(delivery.validation, delivery_state.ValidationRequested)
    assert delivery.validation.feedback == "Fixture delivery"
    return first_page()


def live_accounts_job() -> bytes:
    from scripts.accounts import session_state
    from scripts.production import staff

    identity = staff.StaffId("fixture-director")
    row = staff.StaffRow(identity, "showrunner", "Fixture director", staff.STAFF_ROOT, current_session=identity,
                         run=staff.OpenRun(Path(os.environ["CONSOLE_TEST_RUN"])))
    observed = session_state.running_detached_work(staff.StaffSnapshot((row,)))
    assert observed.labels[identity] == ("Fixture detached job",)
    assert session_state.classify(row, session_state.NoLiveSession(), detached=observed) == "busy"
    return b"<!-- Fixture detached job is busy -->"


def ordinary_warning_page() -> str:
    print("warning: /fixture/Unclassified console configuration read: file.conf: normal warning", file=sys.stderr)
    return first_page()


def cached_role_and_cutover() -> bytes:
    from scripts.accounts import accounts, role_map

    with patch.object(role_map, "is_role_map_host", return_value=False), role_map.stored_role_map():
        cached = role_map.read_role_map()
    history = accounts.resolve_history_label("default", "2025-01-01T00:00:00+00:00")
    label = history.label if isinstance(history, accounts.ResolvedHistoryLabel) else "unresolved"
    return json.dumps([cached.workers, label]).encode()


def first_page_with_live_host() -> str:
    root = review_link.review_root()
    return review_link.snapshot_url(root, root / "fixture/first.html")


def first_page_with_repository_defaults() -> str:
    _ = conf.read("machines").words("aliases")
    return first_page()


def live_input_values() -> dict[str, object]:
    """Read installed inputs through the interfaces used by the console."""
    from scripts.accounts import role_map
    from scripts.lib import build_steps, models_registry, scratch

    aws = conf.read(conf.path("aws_target"))
    unavailable = aws.origin("spend_pace_days.invalid", str)
    holds_file = scratch.holds_file()
    rebuilds = {key: value for key, value in rebuild_row.config_values(rebuild_row.DEFAULT_CONFIG).items()
                if key.startswith("rebuild.")}
    production = conf.read("production").values(
        "mac_run_log", "mac_run_launch", "mac_run_status", "mac_run_bundle",
        "mac_run_example_log_prefix", "mac_run_source")
    return {
        "accounts": conf.read("accounts").whole("usage_stale_minutes"),
        "aws_page": conf.read("aws_page").text("output_folder"),
        "aws_target": [aws.whole("spend_pace_days"), isinstance(unavailable, conf.UnavailableValue)],
        "build_admission": conf.read_or_repository("build_admission").text("ci_label"),
        "build_folders": {"roots.session": conf.read_or_repository("build_folders").text("roots.session")},
        "build_steps": build_steps.read_step_names().shown("integrate"),
        "change_review": str(review_link.review_root()),
        "cloud_gate": conf.read_or_repository("cloud_gate").words("regions"),
        "codex_daemon": conf.read("codex_daemon").number("timeout_seconds"),
        "console": conf.read_or_repository("console").text("for_you.host"),
        "machines": sorted(conf.known_host_names()),
        "memory_feed": conf.read("memory_feed").text("output_folder"),
        "models": models_registry.registry_text(),
        "nightly_review": conf.read("nightly_review").text("state_directory"),
        "production": production,
        "review_links": [review_link.link_host(), conf.read("review_links").whole("server_port"),
                         conf.read_or_repository("review_links").text("review_attachments_root")],
        "role": role_map.read_role_map().workers,
        "scratch": [holds_file.is_absolute(), str(holds_file.resolve())],
        "showrunners": conf.read(conf.path("showrunners")).number("repeat_minutes"),
        "staff": conf.read(Path(os.environ["STAFF_CONFIG"])).text("fixture"),
        "statusline": rebuilds,
        "user_zone": user_zone.user_zone_name(),
    }


def installed_inputs_page() -> str:
    values = json.dumps(live_input_values(), sort_keys=True).encode()
    return "/fixture/first.html?" + urlsafe_b64encode(values).decode()


def broken_disks_generation() -> str:
    with patch.object(disks_page, "page_settings", side_effect=RuntimeError("Fixture Disks generation failed.")):
        return disks_page.page_url()
