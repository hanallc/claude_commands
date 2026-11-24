"""List exceptions to the config form rules."""

from dataclasses import dataclass
from enum import Enum, auto


class ExceptionLifetime(Enum):
    PERMANENT = auto()
    NO_REMOVAL_PHASE = auto()


@dataclass(frozen=True)
class RemoveInPhase:
    title: str


@dataclass(frozen=True)
class FileException:
    path: str
    reason: str
    until: ExceptionLifetime | RemoveInPhase


@dataclass(frozen=True)
class KeyException:
    path: str
    key: str
    reason: str
    until: ExceptionLifetime | RemoveInPhase


DATA_FILES: tuple[FileException, ...] = tuple(
    FileException(path, "data, rule 10", ExceptionLifetime.PERMANENT)
    for path in (
        "config/names.toml",
        "config/cargo-fmt-allowlist.json",
        "config/aws-build-tailscale.json",
        "config/orphans_expected.json",
        "config/settings-orders.json",
        "config/bake_textures_example.json",
        "config/rust_roots.txt",
        "config/mac_gate_modules.txt",
        "config/acknowledgements.txt",
    )
)

DATA_SUFFIXES: tuple[str, ...] = (".md",)

PENDING_TOML_FILES: tuple[FileException, ...] = tuple(
    FileException(path, "Tunable values still use TOML.", RemoveInPhase("TOML tunables to .conf"))
    for path in (
        "config/herdr_upkeep.toml",
        "config/session_retries.toml",
        "config/agent_notes.toml",
        "config/accounts.toml",
    )
)

KEY_FORM_FILES: tuple[FileException, ...] = (
    FileException("config/models.conf", "Catalog keys use model IDs.", ExceptionLifetime.PERMANENT),
)

KEY_FORM_KEYS: tuple[KeyException, ...] = tuple(
    KeyException(f"config/{stem}.conf", key, reason, ExceptionLifetime.NO_REMOVAL_PHASE)
    for stem, reason, keys in (
        ("build_admission", "The build queue uses hyphenated program and repository names.", (
            "max_runs.natedev.cargo-liner",
            "peak_hold_seconds.test-affected",
            "program_first_peak_gib.hand-made",
            "program_first_peak_gib.rust-analyzer",
            "program_first_peak_gib.test-affected",
            "program_probe_arguments.rust-analyzer",
        )),
        ("build_folders", "The folder rules use hyphenated repository and build names.", (
            "patterns.legacy-stable",
            "patterns.legacy-unique",
            "prefixes.admit-sock",
            "prefixes.admit-test",
            "prefixes.bulk-rule-test",
            "prefixes.bump-gate",
            "prefixes.checkpoint-doc",
            "prefixes.checkpoint-paths",
            "prefixes.ci-point",
            "prefixes.copies-index",
            "prefixes.copy-watch",
            "prefixes.fast-track",
            "prefixes.fast-track-check",
            "prefixes.fast-track-index",
            "prefixes.fast-track-promote",
            "prefixes.fast-track-restore-index",
            "prefixes.linux-cache-stage",
            "prefixes.linux-step",
            "prefixes.merge-gate",
            "prefixes.merge-gate-nightly",
            "prefixes.production-scratch",
            "prefixes.promotion-index",
            "prefixes.queue-place",
            "prefixes.rust-release-trial",
            "prefixes.style-review",
            "prefixes.style-sync-lint",
            "prefixes.suite-module",
            "prefixes.targeted-test",
            "prefixes.verify-copy",
            "prefixes.verify-hold",
            "prefixes.verify-tree",
            "slots.Nates-MacBook-Pro-2023.cargo-liner.max",
            "slots.natedev.cargo-liner.max",
        )),
        ("build_steps", "The build steps use hyphenated step names.", (
            "about.between-steps",
            "about.build-step",
            "about.cargo-bench",
            "about.cargo-build",
            "about.cargo-check",
            "about.cargo-clippy",
            "about.cargo-doc",
            "about.cargo-fix",
            "about.cargo-fmt",
            "about.cargo-install",
            "about.cargo-mend",
            "about.cargo-miri",
            "about.cargo-nextest",
            "about.cargo-run",
            "about.cargo-rustc",
            "about.cargo-rustdoc",
            "about.cargo-test",
            "about.ci-bench",
            "about.ci-nextest",
            "about.codex-servers",
            "about.compiler-cache",
            "about.gate-job",
            "about.mac-unavailable",
            "about.repository-slot",
            "about.test-affected",
            "about.test-all",
            "about.test-modules",
            "about.test-nightly",
            "about.unowned-processes",
            "about.verify-build",
            "about.verify-check",
            "about.verify-doc",
            "about.verify-example",
            "about.verify-final",
            "about.verify-lint",
            "about.verify-test",
            "names.fast-track",
            "names.hand-made",
            "names.test-affected",
            "names.test-all",
            "names.test-modules",
            "names.test-nightly",
            "names.toolchain-trial",
            "owned.ci-default",
            "owned.showrunner-default",
        )),
        ("buildlog", "The keys use uppercase names.", (
            "DISK_WALK_HOUR",
            "DISK_WALK_MINUTES",
            "INDEX_READ_WAIT_SECONDS",
            "INDEX_SPILL_PAGES",
            "INDEX_TOUCH_MINUTES",
            "INDEX_UNUSED_DAYS",
            "TOP_COST_RECENT_DAYS",
        )),
        ("copies", "The allow list uses script paths as keys.", (
            "allow.agents/shutdown.py",
            "allow.bevy_migration_plan/bevy_migration_ensure_repo.sh",
            "allow.copies/copies.py",
            "allow.lib/build_folders.py",
            "allow.production/add_unit.py",
            "allow.style/add_style.py",
            "allow.worktree/make_worktree.sh",
            "allow.worktree/worktrees.py",
        )),
        ("detached_runs", "The keys use uppercase names.", (
            "LIVE_RUN_LIMIT",
            "PROCESS_CHECK_SECONDS",
            "RESULT_LINE_CHARS",
            "RESULT_PUSH_RETRIES",
            "RESULT_PUSH_RETRY_SECONDS",
            "RESULT_TEXT_CHARS",
        )),
        ("direct", "The keys use uppercase names.", (
            "ASTRA_AFTER_FAILED_FIXES",
            "CLAUDE_WORKER_POLL_SECS",
            "CLAUDE_WORKER_REGISTER_WAIT_SECS",
            "MAX_CONSECUTIVE_SAME_KIND_PASSES",
            "MAX_FIX_ATTEMPTS",
            "MAX_REOPENS",
            "MAX_REVIEW_CANCELLATIONS",
            "MIN_REPAIR_BUDGET",
            "PANE_AGENT_START_SECONDS",
            "REPAIR_ROUNDS_PER_FINDING",
            "ROUTING_FROM_ROUND",
            "RUNAWAY_ROUNDS",
            "SIMPLIFY_AGENTS",
            "SIMPLIFY_FIRST_RUN_BEFORE",
            "SIMPLIFY_LENS",
            "STALLED_ROUNDS",
            "TOKEN_GUARD_ATTEMPTS",
            "TOKEN_GUARD_DELAY_MS",
            "UNIT_COMPACT_ABOVE_TOKENS",
            "UNIT_PROGRESS_INTERVAL_SECONDS",
            "VERIFY_RESULT_ENV_NAMES",
            "VERIFY_RESULT_ENV_PREFIXES",
            "VERIFY_TEST_FIXED_ENV",
            "VERIFY_TOKEN_HOLD_SECONDS",
            "VERIFY_TOKEN_RENEW_SECONDS",
            "WORKER_COMPACT_SECONDS",
            "WORKER_FIRST_TURN_SECONDS",
            "WORKER_FIRST_TURN_TEXT",
            "WORKER_PUBLISH_LOCK_SECONDS",
            "WORKER_SETTLE_SECONDS",
            "WORKER_SPLIT_RATIO",
        )),
        ("disks", "The drive labels use mount paths as keys.", (
            "name./",
            "name./build",
        )),
        ("gate", "The keys use uppercase names.", (
            "GATE_TIME_LIMIT_FACTOR",
        )),
        ("mac_test", "The Mac test rules use repository names.", (
            "linux_only..claude",
            "linux_only.cargo-liner",
            "offload_exclude..claude",
        )),
        ("merge_checkpoint", "The keys use uppercase names.", (
            "MAC_PULL_RETRY_SECONDS",
            "MAC_PULL_TRIES",
        )),
        ("merge_gate", "The keys use uppercase names.", (
            "GATE_TIME_LIMIT_FACTOR",
        )),
        ("notify", "The keys use uppercase names.", (
            "REVIEW_TIMEOUT_SECONDS",
            "TEXT_ASK_MINUTES",
        )),
        ("producer_map", "The producer map uses project UUIDs as keys.", (
            "20b0427d-945b-4c40-bb24-a6bec83dd648",
            "439f9cfc-4aef-41ce-931a-c7abac39327e",
            "450ac99b-caed-49ba-8c6f-29016df421ab",
            "49511209-add0-40d2-8c53-bca664aaa852",
            "51505394-82db-43d3-a528-136f4ee38d7a",
            "96b016bf-87cf-4a19-af7a-77425852c2e2",
            "b6374931-fe3a-4f6d-96b7-ad20415a8346",
            "e404d92e-8cc6-4714-952f-528c8de5cb7c",
            "e46c8ba2-9fde-4b71-b1ee-c1d87ad1f850",
        )),
        ("production", "The keys use uppercase names.", (
            "PROMPT_ALERT_SECONDS",
            "RENAME_COMMAND_TIMEOUT_SECONDS",
            "RENAME_LIVE_WAIT_SECONDS",
            "RENAME_POLL_SECONDS",
            "RENAME_RUN_STOP_WAIT_SECONDS",
            "RETIRE_COMMAND_TIMEOUT_SECONDS",
            "RETIRE_POLL_SECONDS",
            "RETIRE_TERM_WAIT_SECONDS",
            "UNIT_ANOMALIES_TO",
        )),
        ("shutdown", "The keys use uppercase names.", (
            "HOLDER_EXIT_SECONDS",
            "KILL_GRACE_SECONDS",
            "MOVE_ALERT_REPEAT_MINUTES",
            "MOVE_STOP_MARGIN_SECONDS",
            "OPENING_PROMPT_SECONDS",
            "OWN_PANE_SHELL_SECONDS",
            "REBOOT_LOG_FOLDER",
            "REBOOT_SPINNER_SECONDS",
            "RESTART_RECORD_LOCK_SECONDS",
            "RESUME_CHECK_INTERVAL_SECONDS",
            "RESUME_CHECK_SECONDS",
            "START_SECONDS",
            "STAY_SECONDS",
            "STOP_GRACE_SECONDS",
            "UPKEEP_ROW_WAIT_SECONDS",
        )),
        ("staff", "The keys use uppercase names.", (
            "OPERATION_LOCK_SECONDS",
            "PRODUCER_FOLDER_ROOT",
            "PRODUCER_WORKSPACE",
            "REMOTE_SECONDS",
        )),
        ("statusline", "The colors use role, background, and model names.", (
            "background.codex-pair",
            "model.claude-fable-5-1",
            "model.claude-opus-5-5",
            "model.gpt-6.2-astra",
            "model.gpt-6.2-sol",
            "role.astra-designer",
            "role.astra-reviewer",
            "role.unit-director",
        )),
        ("test_times", "The keys use uppercase names.", (
            "COVERAGE_LEAF_PROGRAMS",
            "GATE_FAILURE_LOG_KIB",
            "MODULE_COST_LEFTOVER_S",
            "MODULE_COST_STAGE_TIMEOUT_S",
            "NEXTEST_SLOW_TEST_S",
            "SLOW_TEST_S",
            "TOP_AWS_CHECKOUT",
            "TOP_CHECKOUT",
            "TOP_MAX_AGE_DAYS",
            "TOP_RUNS",
            "TREND_DAYS",
            "TREND_SETTLE_MINUTES",
        )),
    )
    for key in keys
)

SECTION_FILES: tuple[FileException, ...] = (
    FileException("config/models.conf", "The registry uses sections.", ExceptionLifetime.PERMANENT),
    *(
        FileException(path, "The file still uses sections.", RemoveInPhase("Remove section lines"))
        for path in (
            "config/build_admission.conf",
            "config/build_folders.conf",
            "config/build_steps.conf",
            "config/gate_rename_deploy.conf",
            "config/copies.conf",
            "config/design_roles.conf",
            "config/lint.conf",
        )
    ),
)

COMMENT_LIMIT_FILES: tuple[FileException, ...] = tuple(
    FileException(f"config/{stem}.conf", reason, ExceptionLifetime.NO_REMOVAL_PHASE)
    for stem, reason in (
        ("aws_target", "Some numeric rows lack a limit line."),
        ("build_folders", "Some key blocks lack a comment."),
        ("build_steps", "Some key blocks lack a comment."),
        ("build_targets", "Some numeric rows lack a limit line."),
        ("console", "Some key blocks lack a comment."),
        ("copies", "Some key blocks lack comments or numeric limits."),
        ("design_roles", "Some key blocks lack a comment."),
        ("lint", "Some numeric rows lack a limit line."),
        ("mac_linux", "Some numeric rows lack a limit line."),
        ("memory_feed", "Some numeric rows lack a limit line."),
        ("models", "Some key blocks lack comments or numeric limits."),
        ("notify", "Some numeric rows lack a limit line."),
        ("production", "Some numeric rows lack a limit line."),
        ("send_repeat_guard", "Some numeric rows lack a limit line."),
        ("stall_watch", "Some numeric rows lack a limit line."),
        ("statusline", "Some numeric rows lack a limit line."),
    )
)

BLANK_LINE_FILES: tuple[FileException, ...] = (
    FileException(
        "config/the_list.conf",
        "Item 229 and listadd change this file.",
        RemoveInPhase("Production and direct literals to config"),
    ),
)
