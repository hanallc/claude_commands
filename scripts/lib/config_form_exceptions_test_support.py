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


@dataclass(frozen=True)
class ReaderException:
    path: str
    reads: tuple[str, ...]
    reason: str
    until: ExceptionLifetime | RemoveInPhase


@dataclass(frozen=True)
class VariableException:
    name: str
    reason: str
    until: ExceptionLifetime | RemoveInPhase


@dataclass(frozen=True)
class TunableLiteral:
    path: str
    name: str
    value: str
    stem: str
    key: str
    limit: str
    batch: RemoveInPhase


@dataclass(frozen=True)
class ExternalLiteral:
    path: str
    name: str
    reason: str


@dataclass(frozen=True)
class InternalLiteral:
    path: str
    name: str
    reason: str


_TOML_PHASE = RemoveInPhase("TOML tunables to .conf")
_SECTION_PHASE = RemoveInPhase("Remove section lines")
_CLI_PHASE = RemoveInPhase("config/cli.conf")
_CONTEXT_DEFERRAL_PHASE = RemoveInPhase("Context deferral rules to the common form")

READER_EXCEPTIONS: tuple[ReaderException, ...] = (
    ReaderException(
        "scripts/accounts/accounts.py", ("accounts.toml",),
        "Read tunables from config/accounts.toml.", _TOML_PHASE,
    ),
    ReaderException(
        "scripts/accounts/new_account.py", ("statusline.conf",),
        "Read the original rows of config/statusline.conf before adding an account color.",
        ExceptionLifetime.PERMANENT,
    ),
    ReaderException(
        "scripts/astra/design_roles.py", ("design_roles.conf", "designer.md"),
        "Read config/design_roles.conf by section and load config/design_roles/designer.md.",
        _SECTION_PHASE,
    ),
    ReaderException(
        "scripts/aws_target/hana_render.py", ("build_folders.conf", "build_targets.conf", "copies.conf", "hana_aws.conf"),
        "Read build_folders.conf, build_targets.conf, copies.conf and hana_aws.conf to render settings for a guest host.",
        ExceptionLifetime.PERMANENT,
    ),
    ReaderException(
        "scripts/direct/pass_record.py", ("direct.conf",),
        "Hash the exact bytes of config/direct.conf.",
        ExceptionLifetime.PERMANENT,
    ),
    ReaderException(
        "scripts/hooks/stop-context-deferral.py", ("context_deferral.conf",),
        "Parse the rule list in config/context_deferral.conf.",
        _CONTEXT_DEFERRAL_PHASE,
    ),
    ReaderException(
        "scripts/lint/lint_config.sh", ("lint.conf",),
        "Read config/lint.conf without a subshell and preserve its rows during edits.",
        ExceptionLifetime.PERMANENT,
    ),
    ReaderException(
        "scripts/maintenance/herdr_upkeep.py", ("herdr_upkeep.toml",),
        "Read tunables from config/herdr_upkeep.toml.", _TOML_PHASE,
    ),
    ReaderException(
        "scripts/maintenance/settings.py", ("agent_notes.toml",),
        "Read tunables from config/agent_notes.toml.", _TOML_PHASE,
    ),
    ReaderException(
        "scripts/message/session_records.py", ("session_retries.toml",),
        "Read tunables from config/session_retries.toml.", _TOML_PHASE,
    ),
    ReaderException(
        "scripts/statusline/statusline.sh", ("statusline.conf", "user_zone.conf"),
        "Read the zone in POSIX sh and pass the full color file to jq for color lookups.",
        ExceptionLifetime.PERMANENT,
    ),
    *(
        ReaderException(path, (file.rsplit("/", 1)[-1],), f"Read {file} as data.", ExceptionLifetime.PERMANENT)
        for path, file in (
            ("scripts/direct/simplify_result.py", "config/review/simplify.md"),
            ("scripts/hooks/post-tool-use-random-ack.sh", "config/acknowledgements.txt"),
            ("scripts/hooks/session-start-ste.py", "config/simplified-technical-english.md"),
            ("scripts/lib/names.py", "config/names.toml"),
            ("scripts/lint/folder_audit.py", "config/rust_roots.txt"),
            ("scripts/lint/style_review.py", "config/review/style.md"),
            ("scripts/mac_test/mac_gate.py", "config/mac_gate_modules.txt"),
            ("scripts/orphans/orphans.py", "config/orphans_expected.json"),
        )
    ),
    *(
        ReaderException(path, ("*",), "Read or write the requested config file.", ExceptionLifetime.PERMANENT)
        for path in (
            "scripts/lib/conf.py",
            "scripts/lib/conf.sh",
            "scripts/lib/conf_reads.py",
            "scripts/lib/live_settings.py",
        )
    ),
    ReaderException(
        "scripts/lib/waiting_rows.py", ("models.conf",),
        "Read models.conf to validate model colors during registry recovery.", ExceptionLifetime.PERMANENT,
    ),
)

VARIABLE_EXCEPTIONS: tuple[VariableException, ...] = (
    VariableException("MODELS_CONFIG_FILE", "Keep the exact registry path in a newly launched Claude worker.",
                      ExceptionLifetime.PERMANENT),
    VariableException("HERDR_UPKEEP_CONFIG", "Select config/herdr_upkeep.toml.", _TOML_PHASE),
    VariableException("AGENT_NOTES_JOB_CONFIG", "Select config/agent_notes.toml.", _TOML_PHASE),
    VariableException("DESIGN_ROLES_CONFIG", "Select config/design_roles.conf.", _SECTION_PHASE),
    VariableException("CODEX_CONFIG_FILE", "not a config file", ExceptionLifetime.PERMANENT),
)


LITERAL_INVENTORY: tuple[TunableLiteral | ExternalLiteral | InternalLiteral, ...] = (
    InternalLiteral(
        "scripts/accounts/accounts.py", "MODEL_TAIL_BLOCK_BYTES",
        "Read each transcript tail in fixed chunks.",
    ),
    InternalLiteral(
        "scripts/accounts/accounts.py", "_MODEL_EDGE_BYTES",
        "Hash a fixed sample at each end of the cached prefix.",
    ),
    InternalLiteral(
        "scripts/accounts/accounts.py", "_MODEL_CACHE_ENTRY_BYTES",
        "Keep a fixed size ceiling for each cached scan entry.",
    ),
    InternalLiteral(
        "scripts/agents/detached_runs.py", "LIVE_RUN_LIMIT_REACHED",
        "The detached runner returns this status when it reaches the live run limit.",
    ),
    InternalLiteral(
        "scripts/agents/pane_record.py", "ANCESTOR_LIMIT",
        "Bound the process walk that identifies a pane.",
    ),
    InternalLiteral(
        "scripts/aws_target/gpu_size_trial.py", "NEXTEST_LOG_TAIL_CHUNK_BYTES",
        "Read the nextest tail in fixed chunks.",
    ),
    InternalLiteral(
        "scripts/aws_target/hana_protocol.py", "CONTROL_MAX_BYTES",
        "The Hana control message format has a fixed size limit.",
    ),
    InternalLiteral(
        "scripts/aws_target/hana_protocol.py", "COMMAND_ENVELOPE_RESERVE_BYTES",
        "Reserve space for the Hana control envelope.",
    ),
    InternalLiteral(
        "scripts/aws_target/hana_result.py", "RESULT_MAX_BYTES",
        "The Hana result sender and receiver share this message size limit.",
    ),
    ExternalLiteral(
        "scripts/aws_target/ledger.py", "SPOT_PLACEMENT_AWS_24_HOUR_CONFIGURATION_LIMIT",
        "AWS sets the daily Spot placement configuration limit.",
    ),
    ExternalLiteral(
        "scripts/aws_target/plan_run.py", "INLINE_TEMPLATE_LIMIT",
        "AWS CloudFormation sets the inline template body size limit.",
    ),
    InternalLiteral(
        "scripts/aws_target/quota_requests.py", "MAX_REQUESTED_VCPUS",
        "Validate AWS response counts against both limits in cloud_gate.conf.",
    ),
    ExternalLiteral(
        "scripts/build_hold/build_hold.py", "RELEASE_NOTICE_RUN_LIMIT",
        "Linux limits one argv string to 128 KiB; 500 run lines keep one release notice under it.",
    ),
    InternalLiteral(
        "scripts/build_queue/admission_records.py", "FAMILY_LIMIT",
        "Bound the process walk that identifies the caller.",
    ),
    InternalLiteral(
        "scripts/build_queue/admission_wire.py", "ASSIGNMENT_MAX_BYTES",
        "The assignment file format has a fixed size limit.",
    ),
    InternalLiteral(
        "scripts/build_queue/admission_wire.py", "CLEANUP_STRING_MAX",
        "The cleanup protocol uses this bound in its packet size calculation.",
    ),
    InternalLiteral(
        "scripts/build_queue/admission_wire.py", "CLEANUP_STATUS_MAX",
        "The cleanup protocol stores status in a signed 64-bit range.",
    ),
    InternalLiteral(
        "scripts/build_queue/memory_feed.py", "PROCESS_DEPTH_LIMIT",
        "Bound the process walk that collects memory readings.",
    ),
    InternalLiteral(
        "scripts/build_queue/progress_profile.py", "_LOG_CHUNK_BYTES",
        "Read a progress log in fixed chunks.",
    ),
    InternalLiteral(
        "scripts/buildlog/index.py", "MARK_BYTES",
        "Use a fixed sample to identify an unchanged log prefix.",
    ),
    InternalLiteral(
        "scripts/buildlog/launches.py", "MARK_BYTES",
        "Use a fixed sample for the saved launch scan cursor.",
    ),
    InternalLiteral(
        "scripts/buildlog/repository.py", "LINT_COPY_ALLOCATION_BYTES",
        "Set the fixed size of a lint copy allocation id.",
    ),
    InternalLiteral(
        "scripts/lib/python_startup/sitecustomize.py", "_GATE_ANCESTOR_LIMIT",
        "Bound the gate ancestor scan during Python startup.",
    ),
    InternalLiteral(
        "scripts/lib/python_startup/sitecustomize.py", "_GATE_ENVIRONMENT_LIMIT",
        "Bound environment reads during Python startup.",
    ),
    InternalLiteral(
        "scripts/lib/removal_guard.py", "LIST_NAME_TOKEN_BYTES",
        "Name each removal list file with a fixed-size random token.",
    ),
    ExternalLiteral(
        "scripts/message/postmaster.py", "MAX_SAFE_INTEGER",
        "JavaScript Number.MAX_SAFE_INTEGER limits exact integer exchange with the browser.",
    ),
    ExternalLiteral(
        "scripts/message/send.py", "CLAUDE_RELAY_MAX_BYTES",
        "Claude Code caps relay requests at one Mi UTF-16 code units.",
    ),
    ExternalLiteral(
        "scripts/notify/pushover.py", "TITLE_MAX",
        "The Pushover Message API sets the title character limit.",
    ),
    ExternalLiteral(
        "scripts/notify/pushover.py", "MESSAGE_MAX",
        "The Pushover Message API sets the message character limit.",
    ),
    ExternalLiteral(
        "scripts/prioritize/strip_generated.py", "PACKET_DATA_LIMIT",
        "Git pkt-line sets the payload limit after the four-byte header.",
    ),
    TunableLiteral(
        "scripts/production/hand_made_limit.py", "TAIL_BYTES", "64 * 1024",
        "cli", "hand_made.output_tail_bytes", "# whole: 1..", _CLI_PHASE,
    ),
    InternalLiteral(
        "scripts/production/hand_made_route.py", "CHUNK_BYTES",
        "Copy command output in fixed chunks.",
    ),
    InternalLiteral(
        "scripts/review_links/new_item_dialog.py", "MAX_REQUEST_ID_LENGTH",
        "Bound the request ID that identifies a submission.",
    ),
    InternalLiteral(
        "scripts/review_links/reply_tokens.py", "KEY_BYTES",
        "Reply tokens use a fixed 32-byte key.",
    ),
    InternalLiteral(
        "scripts/review_links/studio_page.py", "MAX_AVATAR_BYTES",
        "Cap a Studio avatar upload at a fixed size.",
    ),
    InternalLiteral(
        "scripts/statusline/rebuild_row.py", "CHANGE_LIMIT",
        "Show a fixed number of changes in the rebuild row.",
    ),
    InternalLiteral(
        "scripts/statusline/rebuild_row.py", "SHORT_REASON_LIMIT",
        "Keep each short reason to a fixed width in the status line.",
    ),
    InternalLiteral(
        "scripts/style/style_footprint.py", "FILE_CHANGED_LINE_LIMIT",
        "Check 5 of docs/style_production.md fixes the size threshold.",
    ),
    ExternalLiteral(
        "scripts/usage/collect.py", "MAX_SQLITE_INTEGER",
        "SQLite INTEGER uses a signed 64-bit range.",
    ),
    InternalLiteral(
        "scripts/usage/context_economy_cache.py", "ANCHOR_BYTES",
        "Use a fixed sample to verify the cached scan position.",
    ),
)


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

FORMAT_FILES: tuple[FileException, ...] = (
    FileException(
        "config/context_deferral.conf",
        "A rule list with repeated kind: value rows that conf.read cannot read.",
        _CONTEXT_DEFERRAL_PHASE,
    ),
)

KEY_FORM_FILES: tuple[FileException, ...] = (
    FileException("config/models.conf", "Catalog keys use model IDs.", ExceptionLifetime.PERMANENT),
)

KEY_FORM_KEYS: tuple[KeyException, ...] = tuple(
    KeyException(f"config/{stem}.conf", key, reason, until)
    for stem, reason, until, keys in (
        ("build_admission", "The build queue uses hyphenated program and repository names.", ExceptionLifetime.PERMANENT, (
            "max_runs.natedev.cargo-liner",
            "peak_hold_seconds.test-affected",
            "program_first_peak_gib.hand-made",
            "program_first_peak_gib.rust-analyzer",
            "program_first_peak_gib.test-affected",
            "program_probe_arguments.rust-analyzer",
        )),
        ("build_folders", "The folder rules use hyphenated repository and build names.", ExceptionLifetime.PERMANENT, (
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
            "removal.path_templates.nates-macbook-pro-2023",
            "roots.gate.nates-macbook-pro-2023",
        )),
        ("build_steps", "The build steps use hyphenated step names.", ExceptionLifetime.PERMANENT, (
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
        )),
        ("copies", "The allow list uses script paths as keys.", ExceptionLifetime.PERMANENT, (
            "allow.agents/shutdown.py",
            "allow.bevy_migration_plan/bevy_migration_ensure_repo.sh",
            "allow.copies/copies.py",
            "allow.lib/build_folders.py",
            "allow.production/add_unit.py",
            "allow.style/add_style.py",
            "allow.worktree/make_worktree.sh",
            "allow.worktree/worktrees.py",
        )),
        ("disks", "The drive labels use mount paths as keys.", ExceptionLifetime.PERMANENT, (
            "name./",
            "name./build",
        )),
        ("mac_test", "The Mac test rules use repository names.", ExceptionLifetime.PERMANENT, (
            "linux_only..claude",
            "linux_only.cargo-liner",
            "offload_exclude..claude",
        )),
        ("producer_map", "The producer map uses project UUIDs as keys.", ExceptionLifetime.PERMANENT, (
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
        ("statusline", "The colors use role, background, and model names.", ExceptionLifetime.PERMANENT, (
            "background.codex-pair",
            "model.claude-fable-5-1",
            "model.claude-opus-5-5",
            "model.gpt-6.2-astra",
            "model.gpt-6.2-sol",
            "role.astra-designer",
            "role.astra-reviewer",
            "role.unit-director",
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
    FileException(f"config/{stem}.conf", reason, RemoveInPhase("Comments and limit lines for every setting"))
    for stem, reason in (
        ("aws_target", "Some numeric rows lack a limit line."),
        ("build_folders", "Some key blocks lack a comment."),
        ("build_steps", "Some key blocks lack a comment."),
        ("console", "Some key blocks lack a comment."),
        ("copies", "Some key blocks lack comments or numeric limits."),
        ("design_roles", "Some key blocks lack a comment."),
        ("memory_feed", "Some numeric rows lack a limit line."),
        ("models", "Some key blocks lack comments or numeric limits."),
        ("production", "Some numeric rows lack a limit line."),
    )
)

BLANK_LINE_FILES: tuple[FileException, ...] = ()
