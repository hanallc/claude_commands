# One common way for config files

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Every tunable in claude_commands is a commented, limit-checked `key=value` row in `config/<stem>.conf`, read at use through `conf.read` or `conf_value`, with a blank line between each comment and config section, and a test that refuses a new form.

> **As-built disposition: amend** — `config/README.md`

> **Issues:** /home/natepiano/rust/hanadocs/issues/one common way for config files.md

## Source

2026-11-23 15:06 PST

List item 224, approved by nate 15:05 PST Nov 23 (via morty). Unit config lives only for this project; the showrunner retires it when the item ends.

Work: proposal P807, ~/rust/hanadocs/issues/one common way for config files.md (one common way for config files in claude_commands). Eight steps, estimate about 28 hours. Make a phased plan from it with /plan:to_phased_plan.

nate's words: "i approve - i want to make sure that config files have white space between comment/config sections so humans can read it".
Add this rule to the plan: each config file has a blank line between its comment and config sections. The test of the common form checks it.

Key rule: steps 1, 2, 4, 7 and 8 change no key and can start. Steps 3, 5 and 6 rename or move keys. Each of them waits for nate's go typed in this unit's session.

devtools of sdlc wrote P807. sdlc sends you the hand-over from devtools.
Use Codex workers, not subagents (nate, 11-23 14:04 PST). claude_commands gate and test runs name --targets natedev,mac-linux until slice 1 is on main.
Send the showrunner the time for the first step and for the full item.

Proposal steps to plan phases: step 1 → Phases 1 and 3; step 2 → Phase 2; step 4 → Phases 4 and 5; step 3 → Phase 6; step 5 → Phase 7; step 6 → Phase 8; step 7 → Phases 9 to 12; step 8 → Phase 13.

## Delegation Context

- **Project:** `~/.claude` (claude_commands) — the files in `config/` and the code that reads them.
- **Project started:** 2026-11-23T23:20:41.664+00:00
- **Stack:** Python 3 (basedpyright strict, unittest); bash and zsh scripts; `.conf` `key=value` files with limit comments.
- **Layout:**
  - `config/` — 68 `.conf` files, 5 TOML files, 6 JSON files, 3 `.txt` lists, `.md` prompts, `config/README.md`.
  - `scripts/lib/` — the shared readers and writers (`conf.py`, `conf.sh`, `conf_reads.py`, `tune.py`, `live_settings.py`, `models_registry.py`, `registry_rows.py`).
  - `scripts/<area>/` — the readers that each phase moves.
  - `commands/` — `tune.md`, `default_models.md`, `build/hand_made.md`.
- **Key files:**
  - `scripts/lib/conf.py` — `conf.read(stem)` returns `ConfigRead` (`text`, `number`, `whole`, `words`, `values`, `origin`); limit comments (`_limits` at line 433), host overrides, Git recovery; `CONF_DIR` (line 379); `read_or_repository` (line 366); ignores `[section]` lines (line 402).
  - `scripts/lib/conf.sh` — `conf_value <stem> <key>` (line 23); limit checks near lines 58-71.
  - `scripts/lib/conf_reads.py` — `read_text` and `CONF_READ_LOG`.
  - `scripts/lib/live_settings.py` — `write_live(stem, key, value)` (line 97) and the publish path that `/tune` uses; `editable_setting` (line 74) and `check_value_line` (line 69), the one-line value rule of every live write; `config/live_settings.conf` `readers=`.
  - `scripts/lib/tune.py` — the `/tune` entry point (`change`, line 36).
  - `scripts/lib/models_registry.py` — the `config/models.conf` reader and publisher: `registry_path` (line 262), `launch_settings` (line 185), `check_agent_value` (line 223), `store_registry` (line 476), `save_registry` (line 486), `_snapshot` (line 66).
  - `scripts/lib/registry_rows.py` — `parse_registry`, `rows`, `values` for `config/models.conf`; `space_groups` (line 98) puts the blank lines on each registry write.
  - `scripts/agents/models_config.sh` — the shell resolver for `config/models.conf` (near line 640).
  - `scripts/agents/set_model.py` — the `/default_models` writer.
  - `scripts/agents/sync_codex_catalog.sh` — writes the generated `[codex.agents]` and `[codex.families]` rows.
  - `scripts/lint/memory_test_support.py` — `write_config_fixture` (line 47).
  - `config/README.md` — the config rules and one section for each file.
  - `config/models.conf` — the agent registry; `[assignments]` (line 10), `[cli.codex]` (line 76), `[cli.claude]` (line 80), `[cli.hand_made]` (line 86).
  - `scripts/lib/config_form_exceptions.py` — the typed allow lists of the form test.
- **Test lanes:** `scripts/lib/test_conf.py`, `scripts/lib/test_conf_sh.py`, `scripts/lib/test_models_registry.py`, `scripts/lib/test_tune.py`, and the `test_*.py` beside each changed module. The new form test is `scripts/lib/test_config_form.py`.
- **Build:** none.
- **Test:** a queued run: `~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py run --repo <worktree> --commit HEAD --apply-uncommitted <worktree> --targets natedev,mac-linux -- ./scripts/lib/py scripts/production/gate.py --affected` (while iterating, add `--module <suite>/<test_module>.py` and name `--targets natedev`: the queue runs `--module` only on natedev). A direct run names single test functions only: `./scripts/lib/py -m unittest scripts.<suite>.<test_module>.<Class>.<test>`. `--targets` stays until the queue default is on main (nate, 11-23). Never start `targeted_test.py` by hand (builds, 11-23).
- **Lint:** `basedpyright` on each changed Python file: zero errors, zero warnings. `bash -n` on each changed shell file.
- **Invariants:**
  - Values do not change. A key that moves keeps its value byte for byte (P807 rule 8).
  - A renamed, moved or deleted key merges only after nate's go through the showrunner (nate, 11-23). A new key merges with a working name that bob gives through the showrunner; bob asks nate later, and a later change is a rename (bob, 11-23 16:11 PST, replaces the builds rule of 11-23). A key that no code reads can be deleted with no go (user, 11-19).
  - Each config read happens at use, on each pass. No module-level copy of a value. No default value in code (P807 rule 7).
  - A limit comment is the line directly above its key (`conf.py` reads it there).
  - Each config file has a blank line between its comment and config sections (nate, 11-23). A comment line or `[section]` line never directly follows a key row.
  - Tests set `CONF_DIR` and write fixtures with `write_config_fixture`. Tests never write the live config folder. The one guard is confsweep's audit hook in `scripts/test_times/module_run.py`; write no second guard (showrunner, 11-23 15:21 PST: `devtools-confguard` is withdrawn).
  - `config/models.conf` keeps its sections, its parser and `/default_models` (P807 rule 11).
  - Wait limits are out of this plan: a time that code waits, sleeps, retries after, holds or times out (`*_TIMEOUT*`, `*_WAIT*`, `*_GRACE*`, `*_INTERVAL*`, `*_SECONDS`, `*_S`, `*_MINUTES`, `*_MS`). List item 277 (bob's wait-limits plan) owns them, and the resources unit's inventory is the one list of them (showrunner, 11-23 15:28 PST, after nate refused one more wait key). No phase moves a wait-limit literal or asks nate for a wait-limit key name. An existing config key that holds a wait time still moves with its file (Phases 6-8), with no new name.
  - Collision waits from the devtools hand-over (11-23): `config/the_list.conf` and `scripts/production/the_list.py` wait for item 229 on main (about 11-24 10:00 PST); `scripts/maintenance/herdr_upkeep.py` waits for item 229 on main; `scripts/production/worker.py`, `scripts/direct/assign.py` and `scripts/agents/detached_runs.py` wait for devtools phases busywrite and rundone on main.

## Phases

### Phase 1 — Common form rule and config file check  · status: done

#### As-built

- `config/README.md` `## One common way` holds rules 1-12: format, location (only `CONF_DIR` selects a test folder; no per-file environment variable), full key name, key form, no `[section]` lines, comments and limits, one reader, change and approval (a new key merges with bob's working name; a change, rename or deletion of a key that code reads waits for nate's go), tests, data is not config, the `config/models.conf` exception, and a blank line between a key row and the next comment or section.
- Each `config/*.conf` file except `config/the_list.conf` has a blank line before each comment or `[section]` line that follows a key row.
- `scripts/lib/test_config_form.py` holds `Violation(path, line_number, rule, line)` and one check for each rule, each `(root, files) -> list[Violation]`: `check_format`, `check_key_form`, `check_sections`, `check_comment_limits`, `check_blank_lines`. `test_repository_form` runs them on `git ls-files config/` and fails on an unlisted violation or a stale allow list row. `test_planted_violations` gives exactly one violation for each planted break. The key form check accepts a part that is a machine alias or host from `config/machines.conf` or a target from `config/build_targets.conf`.
- `scripts/lib/config_form_exceptions.py` holds every allow list as typed data: `FileException(path, reason, until)` and `KeyException(path, key, reason, until)`, with `until: ExceptionLifetime | RemoveInPhase`. `ExceptionLifetime` is `PERMANENT` or `NO_REMOVAL_PHASE`; `RemoveInPhase(title)` names the plan phase that removes the row. Lists: `DATA_FILES`, `DATA_SUFFIXES`, `PENDING_TOML_FILES`, `KEY_FORM_FILES`, `KEY_FORM_KEYS`, `SECTION_FILES`, `COMMENT_LIMIT_FILES`, `BLANK_LINE_FILES`.
- The registry writers keep the blank lines: `registry_rows.space_groups(text)` spaces the whole text, and `registry_rows.replay`, `models_registry._publish` and `save_registry` apply it on each write.
- Every live write refuses a value that `str.splitlines()` splits, with `<stem>: <key>: value must fit on one line`: `live_settings.check_value_line` runs first in `editable_setting`, and `tune.py` runs it before the Mac forward.

**Files:**
- `config/README.md` — the common form rules
- `config/*.conf` — blank lines between blocks
- `scripts/lib/test_config_form.py` — the form checks and their tests
- `scripts/lib/config_form_exceptions.py` — the typed allow lists
- `scripts/lib/registry_rows.py` — `space_groups`; `replay` returns spaced text
- `scripts/lib/models_registry.py` — `_publish` and `save_registry` space the text
- `scripts/lib/live_settings.py` — `check_value_line`
- `scripts/lib/tune.py` — the one-line check before the Mac forward
- `scripts/lib/test_models_registry.py`, `scripts/agents/test_sync_codex_catalog.sh`, `scripts/lib/test_tune.py` — writer and refusal rows

**Binds later work:** each phase that removes the last violation of an allow-listed file or key also removes its row, because the tree test fails on a stale row. `scripts/lib/test_models_registry.py` imports `check_blank_lines`.

**Gotchas:**
- `_is_number` uses `decimal.Decimal(...).is_finite()`, because `conf.decimal` refuses a whole number that overflows a float.
- `test_repository_form` does not report the `config/the_list.conf` row as stale while that file is absent.
- `NO_REMOVAL_PHASE` rows: 204 key form rows (uppercase keys; repository, program, step, path and ID name parts) and 16 comment-limit files.

**Ruled out:**
- Moving `check_blank_lines` out of the test module: one import, no second reader.
- Exception data for the `config/the_list.conf` absent-file case: the row and the case go together with the phase "Production and direct literals to config".

### Phase 2 — The `agent` limit kind  · status: done

#### As-built

- A limit line `# agent: claude` or `# agent: codex` directly above a key accepts `agent` or `agent:effort`. `conf.read`, `conf.check` (`/tune`) and `conf_value` refuse a value that the `/default_models` validator `_agents_validate_pair` (`scripts/agents/models_config.sh`) refuses. The refusal is the validator text `ERROR: [<stem>.<key>] ...`.
- `models_registry.check_agent_value(registry, context, family, value)` is the one Python entry to that validator. `conf.py` imports it inside the function, so no import cycle forms.
- `conf.sh` writes no agent check: an `# agent:` key hands the read to Python, so both readers give one result.
- `conf.py` trims and splits lines with ASCII (C locale) whitespace, `_CONF_WHITESPACE`, the same rule as the shell reader. A line indented with U+00A0 is a plain comment in both readers.
- Git recovery uses a committed agent value only when it passes the current check.

**Files:**
- `scripts/lib/conf.py` — `_LIMIT_PREFIXES`, `_agent_limit`, `_repository_override`, the ASCII whitespace parse
- `scripts/lib/conf.sh` — hands an `# agent:` key to Python
- `scripts/lib/models_registry.py` — `check_agent_value`
- `scripts/lib/test_conf.py`, `scripts/lib/test_conf_sh.py` — `AGENT_LIMIT_CASES`, run by Python, bash and zsh

**Binds later work:** the first live `# agent:` keys and their `/tune` refusal belong to the phase "config/cli.conf".

**Gotchas:**
- A codex alias in `[codex.families]` also reads the codex provider cache (`CODEX_MODELS_CACHE_FILE`, default `~/.codex/models_cache.json`). A test with an `# agent: codex` alias row sets that variable to a fixture cache.
- A claude alias in `[claude.families]` can share a name with a catalog model; the validator's order decides.

**Ruled out:** a second pair validator in `conf.py` (an inline copy of the checks gave other answers than `/default_models`).

### Phase 3 — Code checks and the literal inventory  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** `test_config_form.py` also refuses a new raw config reader, a new per-file config environment variable and a new module-level tunable literal, and its allow lists name each case of today with the phase that removes it.

**Spec:**

1. **One reader check.** Non-test Python and shell code under `scripts/` reads a file in `config/` only through `conf.read`, `conf.read_or_repository` or `conf_value`, or through the `config/models.conf` parser (`scripts/lib/models_registry.py`, `scripts/lib/registry_rows.py`, `scripts/agents/models_config.sh`, `scripts/agents/sync_codex_catalog.sh`). Find raw reads with an AST scan for `tomllib`, `configparser` and `conf_reads.read_text`, and a scan for string constants that name a `config/` file or a `.conf` or `.toml` file name. Allow list, each with the phase that removes it:
   - "Raw readers to conf.read": `scripts/notify/review.py`, `scripts/buildlog/rust_release.py`, `scripts/production/stall_watch.py`, `scripts/message/repeat_guard.py`, `scripts/mac_test/nextest_log.py`.
   - "TOML tunables to .conf": `scripts/maintenance/settings.py`, `scripts/maintenance/herdr_upkeep.py`, `scripts/message/session_records.py`, `scripts/accounts/accounts.py`.
   - "Remove section lines": `scripts/astra/design_roles.py`.
   - `permanent`: each reader of a data file (rule 10), with the file it reads.
2. **Environment variable check.** Non-test code reads no environment variable that names a config file, except `CONF_DIR`. Find names that end in `_CONFIG` or `_CONFIG_FILE` in `os.environ`, `os.getenv` and shell `$NAME` or `${NAME}` reads. `MODELS_CONFIG_PUBLISH` is not a path; it is `permanent` with that reason. Allow list:
   - "Raw readers to conf.read": `SEND_REPEAT_CONFIG`, `STALL_WATCH_CONFIG`.
   - "Per-file config environment variables to CONF_DIR": `MODELS_CONFIG_FILE`, `COPIES_CONFIG`, `STAFF_CONFIG`, `AWS_TARGET_CONFIG`, `BUILD_TARGETS_CONFIG`, `SHOWRUNNERS_CONFIG`.
   - "TOML tunables to .conf": `HERDR_UPKEEP_CONFIG`, `AGENT_NOTES_JOB_CONFIG`.
   - "Remove section lines": `DESIGN_ROLES_CONFIG`.
3. **Literal check.** Find module-level constants in non-test Python under `scripts/` whose name matches `*_MAX*`, `MAX_*`, `*_ATTEMPTS*`, `*_LIMIT*`, `*_BYTES`, `*_CHARS` or `*_RETRIES`, and whose value is a number literal or an arithmetic expression of number literals (`64 * 1024`). Skip `test_*.py`, `*_test_support.py` and `fixtures/`. Skip each wait limit (Delegation Context invariant): a name that matches a wait pattern, or a value that is a time. List item 277 owns them; this check makes no list of them. A constant that is not on the allow list fails the test.
4. **Literal inventory.** Classify each found constant in `scripts/lib/config_form_exceptions.py` as one of:
   - `tunable` — a value a person may tune. The row holds the proposed stem and key (rule 4, with its unit), the limit line, and the batch. Use the existing stem of the area when one exists, for example `config/notify.conf` for `scripts/notify/`.
   - `external` — a fact set outside this repository. Example: `scripts/notify/pushover.py:79` `TITLE_MAX = 250` is a Pushover API limit. The reason names the source.
   - `internal` — a fixed fact of the code that a person never tunes, for example a protocol size. The reason says why.

   Batches, by the folder under `scripts/`:
   - "Build tool literals to config": `buildlog`, `build_queue`, `build_targets`, `build_hold`, `mac_test`, `lint`, `copies`.
   - "Message and notice literals to config": `notify`, `message`, `hooks`, `usage`, `whoami`, `maintenance`.
   - "AWS and review tool literals to config": `aws_target`, `review_links`, `style`, `screen_record`, `shot_report`, and each folder not named in another batch.
   - "Production and direct literals to config": `production`, `direct`, `herdr`, `agents`, `accounts`, `worktree`, `bevy_upgrade`, `history`, `orphans`, `prioritize`, `lib`.
5. **Reply.** The reply lists the `tunable` rows as one table for each batch: file and line, constant, value, proposed `<stem>.<key>`, limit line. No wait limit is in a table. The director sends these tables to the showrunner for bob's working names of the new keys.

**Files:**
- `scripts/lib/test_config_form.py` — the three code checks, a tree test and a planted fixture for each
- `scripts/lib/config_form_exceptions.py` — the reader, variable and literal allow lists

**Workers:** 2 writers — the check code and the inventory are separate files.
- `impl` — `scripts/lib/test_config_form.py`: the three checks, their tree tests and planted fixtures
- `test` opens as impl — `scripts/lib/config_form_exceptions.py`: the reader and variable rows from Spec items 1 and 2, and the classified literal inventory from Spec items 3 and 4; hub: `scripts/lib/config_form_exceptions.py` (`impl` messages this slot for a row it needs)

**State:** none

**Constraints from prior phases:**
- Phase 1 created `scripts/lib/test_config_form.py` and `scripts/lib/config_form_exceptions.py`. The tree test fails on a stale row.
- `config_form_exceptions.py` row types: `FileException(path, reason, until)` and `KeyException(path, key, reason, until)`, frozen dataclasses. `until` is `ExceptionLifetime | RemoveInPhase`: `ExceptionLifetime.PERMANENT`, `ExceptionLifetime.NO_REMOVAL_PHASE` (an exception that no plan phase removes), or `RemoveInPhase("<exact plan phase title>")`. New row types of this phase use the same `until` type. Each row this phase adds names the phase that removes it, or `PERMANENT`.
- `test_config_form.py` form: `Violation(path, line_number, rule, line)` (a `NamedTuple` with `diagnostic()`), and one `check_<rule>(root: Path, files: Sequence[Path]) -> list[Violation]` for each rule. `test_repository_form` collects the violations of each rule, matches the allow list rows, and reports each stale row; `test_planted_violations` is one table of `(check, file name, source, line number, rule, line)`. The new code checks follow this form; their file list is the tracked non-test files under `scripts/`.
- `scripts/lib/test_models_registry.py` imports `check_blank_lines` from `test_config_form.py`. Keep its name and signature.

**Acceptance gate:**
- `test_config_form.py` passes on the tree. Each new planted fixture gives exactly one violation.
- Each found constant has one class, and each `tunable` row has a stem, key, limit line and batch.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 4 — Raw readers to conf.read  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** Five modules that parse config text by hand read through `conf.read`, two per-file environment variables go away, and `scripts/notify/review.py` has no code default.

**Spec:**

1. `scripts/notify/review.py` — the raw parse of `REVIEW_TIMEOUT_SECONDS` from `config/notify.conf` (lines 63-71) becomes a `conf.read("notify")` read at use. Remove the fallback to 60 when the read fails (line 73). A bad or missing value now uses the Git recovery of `conf.read`. Keep the key name `REVIEW_TIMEOUT_SECONDS`; a rename is a key change and is not in this plan.
2. `scripts/buildlog/rust_release.py` — the raw text parse near line 208 becomes `conf.read(<stem>)`.
3. `scripts/production/stall_watch.py` — the raw text parse near line 666 becomes `conf.read("stall_watch")`. Remove `STALL_WATCH_CONFIG`; its tests set `CONF_DIR`.
4. `scripts/message/repeat_guard.py` — the path read near line 38 becomes `conf.read("send_repeat_guard")`. Remove `SEND_REPEAT_CONFIG` (line 71) from it and from `scripts/message/notifier.sh`; the tests set `CONF_DIR`.
5. `scripts/mac_test/nextest_log.py` — the path read near line 32 becomes `conf.read(<stem>)`.
6. Each read happens at use. No module-level copy. Values and key names stay the same.
7. Tests write fixtures with `write_config_fixture` in `CONF_DIR`. A test that used a code default now writes the key in its fixture.
8. Remove these five files and the two variable names from the allow lists in `scripts/lib/config_form_exceptions.py`.

**Files:**
- `scripts/notify/review.py`, `scripts/notify/test_review.py`
- `scripts/buildlog/rust_release.py`, `scripts/buildlog/test_rust_release.py`
- `scripts/mac_test/nextest_log.py`, `scripts/mac_test/test_offload.py`, `scripts/mac_test/test_offload_cleanup.py`, `scripts/mac_test/test_offload_forward.py`
- `scripts/production/stall_watch.py`, `scripts/production/test_stall_watch.py`
- `scripts/message/repeat_guard.py`, `scripts/message/test_repeat_guard.py`, `scripts/message/notifier.sh`
- `scripts/lib/config_form_exceptions.py` — rows removed

**Workers:** 2 writers — split by module group.
- `impl` — `scripts/notify/`, `scripts/buildlog/` and `scripts/mac_test/` files above; hub: `scripts/lib/config_form_exceptions.py` (both groups remove rows)
- `test` opens as impl — `scripts/production/stall_watch.py`, `scripts/message/repeat_guard.py`, `scripts/message/notifier.sh` and their tests

**State:** none

**Constraints from prior phases:**
- Phase 3 put these five readers and `SEND_REPEAT_CONFIG` and `STALL_WATCH_CONFIG` on the allow lists in `scripts/lib/config_form_exceptions.py` under "Raw readers to conf.read". The tree test fails on a stale row, so each removed reader also leaves the list.

**Acceptance gate:**
- Each changed module's tests pass on `CONF_DIR` fixtures. `CONF_READ_LOG` shows each stem read.
- `test_config_form.py` passes with the rows removed.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file; `bash -n` passes on `scripts/message/notifier.sh`.

### Phase 5 — Per-file config environment variables to CONF_DIR  · status: todo
**Depends on:** Phase 4

#### Work Order

**Goal:** Code and tests find `models.conf`, `copies.conf`, `staff.conf`, `aws_target.conf`, `build_targets.conf` and `showrunners.conf` through `CONF_DIR` or the repository config folder, with no per-file environment variable.

**Spec:**

0. **Wait.** Start only after devtools phases busywrite and rundone are on main (they change `scripts/direct/assign.py`). Merge main first.
1. For each of `MODELS_CONFIG_FILE`, `COPIES_CONFIG`, `STAFF_CONFIG`, `AWS_TARGET_CONFIG`, `BUILD_TARGETS_CONFIG` and `SHOWRUNNERS_CONFIG`, first confirm that it names a file in `config/`.
2. **Config file.** The reader takes the file from `CONF_DIR` when it is set, else from the repository `config/` folder: the same rule as `conf.read`. Remove the variable from code and tests. Tests write the fixture into `CONF_DIR` with `write_config_fixture`.
3. **Not a config file.** If a variable names a file outside `config/`, leave it. Move its allow list row to `permanent` with the reason "not a config file" and name it in the reply.
4. **Runtime use.** If a variable carries a purpose beyond tests, keep that purpose and name it in the reply. Example to check first: `scripts/lib/models_registry.py` `_snapshot` (line 66) may pin a launched process to a registry snapshot through `MODELS_CONFIG_FILE`. A launched worker must read the same registry that it reads today. If the purpose needs the variable, keep the variable and move its row to `permanent` with the reason.
5. `scripts/lib/models_registry.py` `registry_path` (line 262) is the one place that resolves the `models.conf` path. The other sources call it: `scripts/direct/assign.py`, `scripts/direct/pass_record.py`, `scripts/direct/assign_claude.py`, `scripts/direct/claude_worker.py`, `scripts/lib/waiting_rows.py`, `scripts/whoami/quota_alert.py`, `scripts/agents/models_config.sh`, `scripts/agents/sync_codex_catalog.sh`.
6. Source files for the other variables: `COPIES_CONFIG` in `scripts/lib/build_folders.py` and `scripts/copies/copies.py`; `STAFF_CONFIG` in `scripts/production/staff.py`; `AWS_TARGET_CONFIG` in `scripts/aws_target/config.py`; `BUILD_TARGETS_CONFIG` in `scripts/build_hold/override_guard.py`, `scripts/build_hold/override_guard.sh`, `scripts/mac_test/mac_test.py` and `scripts/buildlog/record.py`; `SHOWRUNNERS_CONFIG` in `scripts/production/showrunners.py`.
7. Test files: list them with `grep -rlw <NAME> scripts`. On the plan base: `MODELS_CONFIG_FILE` 42 files, `COPIES_CONFIG` 20, `STAFF_CONFIG` 21, `AWS_TARGET_CONFIG` 4, `BUILD_TARGETS_CONFIG` 15, `SHOWRUNNERS_CONFIG` 13. `MODELS_CONFIG_PUBLISH=0` stays in tests (`scripts/direct/recorder_fixture.py:196-198`).
8. Remove each removed variable from the allow list in `scripts/lib/config_form_exceptions.py`.

**Files:**
- `scripts/lib/models_registry.py`, `scripts/direct/assign.py`, `scripts/direct/pass_record.py`, `scripts/direct/assign_claude.py`, `scripts/direct/claude_worker.py`, `scripts/lib/waiting_rows.py`, `scripts/whoami/quota_alert.py`, `scripts/agents/models_config.sh`, `scripts/agents/sync_codex_catalog.sh`, `scripts/direct/recorder_fixture.py`
- `scripts/lib/build_folders.py`, `scripts/copies/copies.py`, `scripts/production/staff.py`, `scripts/aws_target/config.py`, `scripts/build_hold/override_guard.py`, `scripts/build_hold/override_guard.sh`, `scripts/mac_test/mac_test.py`, `scripts/buildlog/record.py`, `scripts/production/showrunners.py`
- `scripts/**/test_*.py`, `scripts/**/*_test_support.py` — the files that set each variable (Spec item 7)
- `scripts/lib/config_form_exceptions.py` — rows removed

**Workers:** 2 writers — split by variable.
- `impl` — `MODELS_CONFIG_FILE`: its sources, `scripts/direct/recorder_fixture.py` and its tests; hub: `scripts/lib/config_form_exceptions.py` (both slots remove rows)
- `test` opens as impl — `COPIES_CONFIG`, `STAFF_CONFIG`, `AWS_TARGET_CONFIG`, `BUILD_TARGETS_CONFIG`, `SHOWRUNNERS_CONFIG`: their sources and tests

**State:** none

**Constraints from prior phases:**
- Phase 3 put the six variables on the variable allow list under "Per-file config environment variables to CONF_DIR".
- Phase 4 removed `SEND_REPEAT_CONFIG` and `STALL_WATCH_CONFIG`; their tests use `CONF_DIR` with `write_config_fixture`, which is the pattern for this phase.

**Acceptance gate:**
- The tests of each changed module pass on `CONF_DIR` fixtures.
- `grep -rw` finds each removed variable nowhere under `scripts/`.
- `test_config_form.py` passes with the rows removed.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file; `bash -n` passes on each changed shell file.

### Phase 6 — config/cli.conf  · status: todo
**Depends on:** Phase 2, Phase 5

#### Work Order

**Goal:** The eight CLI tool rows live in `config/cli.conf` with the `cli.<tool>.<family>.<field>` names, every reader and `/default_models` use the new file, and `config/models.conf` has no `[cli.*]` section.

**Spec:**

0. **Wait.** Start only after nate's go for the eight-row move below, typed in this unit's session or sent by the showrunner as nate's words.
1. **New file.** Create `config/cli.conf` with these rows, values byte for byte from `config/models.conf`, one blank line between blocks:

   ```
   # Settings for CLI tools. The full name of a key is cli.<key>.
   # A key is <tool>.<family>.<field> or <tool>.<setting>.

   # Model and effort for the merge task (alias: merge) on the codex family, as model or model:effort.
   # agent: codex
   merge_branch.codex.model=luna:high

   # Model and effort for an interactive terminal session (alias: code, REPL) on the codex family.
   # agent: codex
   interactive.codex.model=sol:high

   # Model and effort for the merge task on the claude family.
   # agent: claude
   merge_branch.claude.model=sonnet:high

   # Model and effort for an interactive terminal session on the claude family.
   # agent: claude
   interactive.claude.model=opus:max

   # Model and effort of the hand made command check (scripts/production/hand_made.py).
   # agent: claude
   hand_made.claude.model=opus:high

   # Role whose account runs the hand made check.
   # one of: units, showrunners, producers
   hand_made.claude.role=units

   # Seconds that one hand made check call can take before the run is refused.
   # range: 5e-324..
   hand_made.check_time_limit_seconds=90

   # Largest sum of program file bytes that the hand made check reads; more is refused.
   # whole: 1..
   hand_made.max_script_bytes=65536
   ```

   Rename map (full names): `[cli.codex] merge_branch` → `cli.merge_branch.codex.model`; `[cli.codex] interactive` → `cli.interactive.codex.model`; `[cli.claude] merge_branch` → `cli.merge_branch.claude.model`; `[cli.claude] interactive` → `cli.interactive.claude.model`; `[cli.hand_made] claude.model` → `cli.hand_made.claude.model`; `[cli.hand_made] claude.role` → `cli.hand_made.claude.role`; `[cli.hand_made] check_time_limit_seconds` → `cli.hand_made.check_time_limit_seconds`; `[cli.hand_made] max_script_bytes` → `cli.hand_made.max_script_bytes`.
2. **models.conf.** Remove the `[cli.codex]`, `[cli.claude]` and `[cli.hand_made]` sections and their comments from `config/models.conf` (lines 74-97 after Phase 1). `[assignments] cli=codex` stays in `config/models.conf`: it picks the family, like the other assignments.
3. **Readers.**
   - `scripts/lib/models_registry.py` `launch_settings` (line 185): a `cli` key reads `conf.read("cli")` key `<task>.<family>.model`.
   - `scripts/agents/models_config.sh` (the resolver near line 640) and `scripts/cli_agent/cli_agent.sh`: read `conf_value cli <task>.<family>.model`.
   - `scripts/production/hand_made.py` `read_settings` (lines 136-162): read `conf.read("cli")` keys `hand_made.claude.model`, `hand_made.claude.role`, `hand_made.check_time_limit_seconds`, `hand_made.max_script_bytes`. Remove the checks that the limit lines now make (model:effort form, role choice, number and more than 0). Keep each refusal message clear; a `conf` limit error becomes a `Refused` with the key name. Remove `SECTION`.
4. **Writer.** `/default_models` (`scripts/agents/set_model.py`) writes a `cli` row to `config/cli.conf` through `scripts/lib/live_settings.py` `write_live("cli", key, value)` and the publish path that `/tune` uses. Other rows keep `store_registry`. `/default_models cli claude` still edits `[assignments] cli` in `config/models.conf`.
5. **Docs.** `commands/default_models.md`: the examples and the paragraph at lines 106-111 name `config/cli.conf` keys, not `[cli.claude]`. `commands/build/hand_made.md`: name `config/cli.conf`. `config/README.md`: rule 6 adds "`# agent: claude` or `# agent: codex` checks an `agent[:effort]` value against that family in `config/models.conf`."; rule 11 adds "The `cli` rows live in `config/cli.conf`."; the `## models.conf` section drops the cli rows; add a `## cli.conf` section with the key form `<tool>.<family>.<field>` or `<tool>.<setting>`, and "A new CLI tool copies a block and changes `<tool>`; it adds a `.role` row only when its code reads one."
6. **Tests.** Fixtures write `cli.conf` in `CONF_DIR`: `scripts/whoami/quota_alert_test_support.py`, `scripts/agents/test_agent_exec.py`, `scripts/agents/test_agent_exec.sh`, `scripts/production/test_hand_made.py` and the other `scripts/production/test_hand_made_*.py` that build a registry, `scripts/lib/test_models_registry.py`, `scripts/lib/test_models_registry_fixture.py`. Add a case: `/default_models cli.merge_branch sonnet` writes `merge_branch.claude.model` in `cli.conf`.
7. **Proof.** Record `bash scripts/cli_agent/cli_agent.sh --status` before the change. After the change it prints the same agents.

**Files:**
- `config/cli.conf` — new
- `config/models.conf` — `[cli.*]` sections removed
- `scripts/lib/models_registry.py`, `scripts/agents/models_config.sh`, `scripts/cli_agent/cli_agent.sh`, `scripts/agents/set_model.py`
- `scripts/production/hand_made.py`
- `commands/default_models.md`, `commands/build/hand_made.md`, `config/README.md`
- `scripts/whoami/quota_alert_test_support.py`, `scripts/agents/test_agent_exec.py`, `scripts/agents/test_agent_exec.sh`, `scripts/production/test_hand_made.py`, `scripts/production/test_hand_made_guest.py`, `scripts/production/test_hand_made_limit.py`, `scripts/production/test_hand_made_mac.py`, `scripts/lib/test_models_registry.py`, `scripts/lib/test_models_registry_fixture.py`

**Workers:** 2 writers — the launcher side and the hand made side.
- `impl` — `config/cli.conf`, `config/models.conf`, `scripts/lib/models_registry.py`, `scripts/agents/models_config.sh`, `scripts/cli_agent/cli_agent.sh`, `scripts/agents/set_model.py`, `commands/default_models.md`, `config/README.md`, `scripts/lib/test_models_registry.py`, `scripts/lib/test_models_registry_fixture.py`, `scripts/agents/test_agent_exec.py`, `scripts/agents/test_agent_exec.sh`, `scripts/whoami/quota_alert_test_support.py`; hub: `config/cli.conf` (both sides read it)
- `test` opens as impl — `scripts/production/hand_made.py`, `commands/build/hand_made.md`, `scripts/production/test_hand_made*.py`

**State:** none

**Constraints from prior phases:**
- Phase 2 added the `# agent: claude|codex` limit kind to `conf.py` and `/tune` (`conf.check`). It calls `models_registry.check_agent_value(registry, context, family, value)`, which runs the `/default_models` validator `_agents_validate_pair` from `scripts/agents/models_config.sh`. `conf.sh` hands an `# agent:` key to Python, so both readers give one result. A refusal is the validator text `ERROR: [<stem>.<key>] ...`.
- A codex alias in `[codex.families]` (`luna`, `sol`) also reads the codex provider cache (`CODEX_MODELS_CACHE_FILE`, default `~/.codex/models_cache.json`), which can point it at another model. A test fixture with an `# agent: codex` alias row sets `CODEX_MODELS_CACHE_FILE` to a fixture cache (see `write_agent_limit_codex_cache` in `scripts/lib/test_conf.py`).
- Git recovery never falls back to a committed agent value that fails the current check.
- Phase 5 made `models_registry.registry_path` the one place that finds `models.conf`, through `CONF_DIR` or the repository.
- Phase 1 rules: a blank line between blocks; the limit line directly above the key.
- Phase 1: the registry writers apply `registry_rows.space_groups(text)` to the whole text on each write (`registry_rows.replay`, `models_registry._publish`, `save_registry`). A blank line stays between a key row and the next comment or section after the `[cli.*]` removal.
- Phase 1: `write_live` and `stage_save` call `live_settings.editable_setting`, which first refuses a value that `str.splitlines()` splits (`live_settings.check_value_line`). The `/default_models` cli write gets the same refusal.
- `config/models.conf` is on the `COMMENT_LIMIT_FILES` allow list in `scripts/lib/config_form_exceptions.py`. If the `[cli.*]` removal takes away its last comment or limit violation, remove that row; the tree test fails on a stale row.

**Acceptance gate:**
- `cli_agent.sh --status` prints the same agents before and after.
- `/tune cli merge_branch.claude.model <value>` refuses an unknown agent (the `agent` limit).
- The models registry, hand made, agent exec and quota alert tests pass.
- `test_config_form.py` passes.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file; `bash -n` passes on each changed shell file.

### Phase 7 — TOML tunables to .conf  · status: todo
**Depends on:** Phase 5

#### Work Order

**Goal:** The 35 tunable keys of the four TOML files are commented, limit-checked rows in `.conf` files, read through `conf.read`, and the four TOML files are gone.

**Spec:**

0. **Wait.** Start only after nate's go for this move and after item 229 is on main (it changes `scripts/maintenance/herdr_upkeep.py`). Merge main first.
1. **Moves**, values byte for byte. A TOML table becomes a dotted prefix, so the full name `<stem>.<table>.<key>` stays the same:
   - `config/herdr_upkeep.toml` → `config/herdr_upkeep.conf`: `job.interval_seconds=120`, `job.timeout_seconds=60`.
   - `config/session_retries.toml` → `config/session_retries.conf`: `registry_reads=4`, `registry_delay_seconds=0.1`.
   - `config/accounts.toml` `[trust] folders = ["~/worktrees"]` → `config/accounts.conf` row `trust.folders=~/worktrees` (a `words` value: space-separated). `config/accounts.conf` has no `trust.folders` key today.
   - `config/agent_notes.toml` → `config/agent_notes.conf`: each `[table] key` becomes `table.key`. 30 keys: `job.interval_seconds`, `job.timeout_seconds`, `mac_usage.interval_seconds`, `mac_usage.codex_interval_seconds`, `parts.<part>.interval_seconds` and `parts.<part>.timeout_seconds` for `notes`, `quota`, `pacer`, `refusals`, `watchdog`, `logins`, `prune`, `folder_size`, `vault`, `vault_save`, then `vault.git_seconds`, `vault.named_paths`, `watchdog.dispatch_seconds`, `watchdog.recovery_seconds`, `watchdog.pass_dispatch_seconds`, `watchdog.result_seconds`.
2. **Comments and limits.** Carry each TOML comment over. Replace prose limits ("Use a positive finite number") with a `# whole:` or `# range:` line that states the limit the reader checks today. One blank line between blocks.
3. **Readers**, each read at use, no module-level copy:
   - `scripts/maintenance/herdr_upkeep.py` (line 28) → `conf.read("herdr_upkeep")`; remove `HERDR_UPKEEP_CONFIG`.
   - `scripts/maintenance/settings.py` (line 88) → `conf.read("agent_notes")`; remove `AGENT_NOTES_JOB_CONFIG`. The notes timer reads values on each pass; keep that.
   - `scripts/message/session_records.py` (line 100) → `conf.read("session_retries")`.
   - `scripts/accounts/accounts.py` (line 874) → `conf.read("accounts").words("trust.folders")`.
4. **Tests** write `CONF_DIR` fixtures with `write_config_fixture`.
5. Delete the four TOML files. Remove their rows from the format, reader and variable allow lists in `scripts/lib/config_form_exceptions.py`.
6. **README.** Replace the TOML descriptions of these files with `.conf` sections.

**Files:**
- `config/herdr_upkeep.toml`, `config/session_retries.toml`, `config/accounts.toml`, `config/agent_notes.toml` — deleted
- `config/herdr_upkeep.conf`, `config/session_retries.conf`, `config/agent_notes.conf` — new
- `config/accounts.conf` — `trust.folders` row
- `scripts/maintenance/herdr_upkeep.py`, `scripts/maintenance/test_herdr_upkeep.py`
- `scripts/maintenance/settings.py`, `scripts/maintenance/test_agent_notes_job.py`, `scripts/maintenance/test_*.py` — the agent notes reader, and the tests that set `AGENT_NOTES_JOB_CONFIG`
- `scripts/message/session_records.py`, `scripts/message/test_session_records.py`
- `scripts/accounts/accounts.py`, `scripts/accounts/home_test_support.py`, `scripts/production/unit_status_test_support.py`
- `scripts/lib/config_form_exceptions.py`, `config/README.md`

**Workers:** 2 writers — split by file pair.
- `impl` — the `agent_notes` and `herdr_upkeep` files and their readers and tests; hub: `scripts/lib/config_form_exceptions.py` (both slots remove rows)
- `test` opens as impl — the `session_retries` and `accounts` files and their readers and tests, `config/README.md`

**State:** none

**Constraints from prior phases:**
- Phase 3 put the four TOML files on the format allow list, their four readers on the reader allow list, and `HERDR_UPKEEP_CONFIG` and `AGENT_NOTES_JOB_CONFIG` on the variable allow list, under "TOML tunables to .conf".
- Phase 2 and Phase 1 rules apply: a limit line directly above each number key; a blank line between blocks.

**Acceptance gate:**
- Each changed module's tests pass on `CONF_DIR` fixtures.
- The `/tune` key-and-limit check accepts `agent_notes job.interval_seconds 120` with no write.
- `test_config_form.py` passes with the rows removed.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 8 — Remove section lines  · status: todo
**Depends on:** Phase 5

#### Work Order

**Goal:** No `.conf` file except `config/models.conf` has a `[section]` line, every key keeps its name and value, and every reader gets the same values as before.

**Spec:**

0. **Wait.** Start only after nate's go for this step.
1. **Files:** `config/build_admission.conf` (`[disk]`, `[memory]`), `config/build_folders.conf` (9 sections), `config/build_steps.conf` (5), `config/gate_rename_deploy.conf` (1), `config/copies.conf` (`[copies]`, `[allow-list]`), `config/design_roles.conf` (`[ownership]`, `[roles]`), `config/lint.conf` (4). Replace each `[section]` line with a plain group comment line, `# <section>`, with a blank line above it.
2. **Key names stay.** On the plan base no key repeats across sections in any of these files, so every key keeps its name. If a reader needs the section as part of the key, that is a rename: stop that file and report it to the director. No rename merges without nate's go.
3. **Readers first.** Confirm how each reader reads its file. Candidates from a plan-base scan (confirm each): `scripts/build_queue/admission_service.py`, `scripts/build_queue/memory_feed.py`, `scripts/build_queue/limits_view.py`, `scripts/lint/memory_admit.py`, `scripts/worktree/worktrees.py`, `scripts/astra/design_roles.py`, `scripts/herdr/worker_panes.py`, `scripts/buildlog/rust_release.py`, `scripts/build_targets/build_targets.py`, `scripts/buildlog/report.py`, `scripts/mac_test/eligibility.py`, `scripts/history/rounds.py`, `scripts/style/style_plan.py`. A reader that reads by section changes to `conf.read` on the same key.
4. **design_roles.** `scripts/astra/design_roles.py` uses `configparser`. It changes to `conf.read("design_roles")` keys `host` and `new_open`. Remove `DESIGN_ROLES_CONFIG`; tests use `CONF_DIR`.
5. **Proof.** Before the change, a script reads every key of each file with `conf.read` and with each section-aware reader, and saves the values. After the change the same script gives the same values.
6. Remove the seven files from the sections allow list, and `design_roles.py` and `DESIGN_ROLES_CONFIG` from the reader and variable allow lists.
7. **README.** Update the sections of `config/README.md` that show these files with `[section]` lines (for example `## lint.conf`).

**Files:**
- `config/build_admission.conf`, `config/build_folders.conf`, `config/build_steps.conf`, `config/lint.conf` — group comments; their section-aware readers from Spec item 3 and their tests also change
- `config/gate_rename_deploy.conf`, `config/copies.conf`, `config/design_roles.conf`
- `scripts/astra/design_roles.py`, `scripts/astra/test_design_roles.py`, `scripts/astra/test_design_roles_sources.py`, `scripts/herdr/worker_panes.py`
- `scripts/lib/config_form_exceptions.py`, `config/README.md`

**Workers:** 2 writers — split by file group.
- `impl` — `config/build_admission.conf`, `config/build_folders.conf`, `config/build_steps.conf`, `config/lint.conf` and their readers and tests
- `test` opens as impl — `config/gate_rename_deploy.conf`, `config/copies.conf`, `config/design_roles.conf`, `scripts/astra/design_roles.py`, `scripts/herdr/worker_panes.py` and their tests, `config/README.md`; hub: `scripts/lib/config_form_exceptions.py` (both slots remove rows)

**State:** none

**Constraints from prior phases:**
- Phase 3 put the seven files on the sections allow list and `design_roles.py` and `DESIGN_ROLES_CONFIG` on the reader and variable allow lists, under "Remove section lines".
- Phase 1 rule 5: a plain comment line names a group of keys; rule 12: a blank line above it.
- A `# <section>` comment gives the next key block a comment. `config/build_folders.conf`, `config/build_steps.conf`, `config/copies.conf`, `config/design_roles.conf` and `config/lint.conf` are on the `COMMENT_LIMIT_FILES` allow list. Remove the row of each file that has no comment or limit violation left; the tree test fails on a stale row.
- The `KEY_FORM_KEYS` rows match by path and key, not by line. They stay, because the keys keep their names.

**Acceptance gate:**
- The proof script gives the same values before and after for each file.
- Each changed reader's tests pass.
- `test_config_form.py` passes with the rows removed.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 9 — Build tool literals to config  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** Each `tunable` literal of the build tool batch is a commented, limit-checked key in `config/`, read at use, with the same value, and its allow list row is gone.

**Spec:**

0. **Wait.** The merge waits for bob's working names of this batch's new keys (the inventory tables from Phase 3), sent by the showrunner. The work can start before it.
1. Batch folders: `scripts/buildlog`, `scripts/build_queue`, `scripts/build_targets`, `scripts/build_hold`, `scripts/mac_test`, `scripts/lint`, `scripts/copies`.
2. For each `tunable` row of this batch in `scripts/lib/config_form_exceptions.py`: add the key to its stem with a comment that says what it controls and its limit line, one blank line between blocks. Use the working name that bob sent through the showrunner.
3. The code reads the key with `conf.read(stem)` at use. Delete the constant. A value that a function default or a module-level object used now comes from the read at use.
4. Values stay the same. A move of an unchanged value needs no value approval; the new name uses bob's working name.
5. Remove the moved rows from the allow list. `external` and `internal` rows stay.
6. Tests that patched a constant now write the key in a `CONF_DIR` fixture.

**Files:**
- `scripts/lib/config_form_exceptions.py` — the batch rows name each source file; the rows leave the list
- `scripts/**/*.py` — the source files of the batch rows, and their tests
- `config/*.conf` — the stems that get keys

**Workers:** 2 writers — split by folder.
- `impl` — `scripts/buildlog`, `scripts/build_queue`, `scripts/copies` and their stems; hub: `scripts/lib/config_form_exceptions.py` (both slots remove rows)
- `test` opens as impl — `scripts/build_targets`, `scripts/build_hold`, `scripts/mac_test`, `scripts/lint` and their stems

**State:** none

**Constraints from prior phases:**
- Phase 3 wrote the literal inventory in `scripts/lib/config_form_exceptions.py`. Each `tunable` row holds the proposed stem, key, limit line and batch "Build tool literals to config".
- Phase 1 rules: key form (rule 4), a limit line directly above each number key, a blank line between blocks.

**Acceptance gate:**
- Each changed module's tests pass.
- `test_config_form.py` passes, and the allow list has no `tunable` row of this batch.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 10 — Message and notice literals to config  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** Each `tunable` literal of the message and notice batch is a commented, limit-checked key in `config/`, read at use, with the same value, and its allow list row is gone.

**Spec:**

0. **Wait.** The merge waits for bob's working names of this batch's new keys, sent by the showrunner. The work can start before it.
1. Batch folders: `scripts/notify`, `scripts/message`, `scripts/hooks`, `scripts/usage`, `scripts/whoami`, `scripts/maintenance`. Examples from P807: `scripts/notify/auto_silence.py:54` `JOB_TIMEOUT_SECONDS = 300`. `scripts/notify/pushover.py:79` `TITLE_MAX` is `external` and stays.
2. For each `tunable` row of this batch in `scripts/lib/config_form_exceptions.py`: add the key to its stem with a comment that says what it controls and its limit line, one blank line between blocks. Use the working name that bob sent through the showrunner.
3. The code reads the key with `conf.read(stem)` at use. Delete the constant.
4. Values stay the same.
5. Remove the moved rows from the allow list. `external` and `internal` rows stay.
6. Tests that patched a constant now write the key in a `CONF_DIR` fixture.
7. Hooks run on each tool call. A hook reads its key once for each run, never at import.

**Files:**
- `scripts/lib/config_form_exceptions.py` — the batch rows name each source file; the rows leave the list
- `scripts/**/*.py` — the source files of the batch rows, and their tests
- `config/*.conf` — the stems that get keys

**Workers:** 2 writers — split by folder.
- `impl` — `scripts/notify`, `scripts/message`, `scripts/maintenance` and their stems; hub: `scripts/lib/config_form_exceptions.py` (both slots remove rows)
- `test` opens as impl — `scripts/hooks`, `scripts/usage`, `scripts/whoami` and their stems

**State:** none

**Constraints from prior phases:**
- Phase 3 wrote the literal inventory; each `tunable` row of batch "Message and notice literals to config" holds the proposed stem, key, limit line.
- Phase 4 moved `scripts/notify/review.py` to `conf.read("notify")`.
- Phase 7 moved the `scripts/maintenance` TOML readers to `conf.read`.

**Acceptance gate:**
- Each changed module's tests pass.
- `test_config_form.py` passes, and the allow list has no `tunable` row of this batch.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 11 — AWS and review tool literals to config  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** Each `tunable` literal of the AWS and review tool batch is a commented, limit-checked key in `config/`, read at use, with the same value, and its allow list row is gone.

**Spec:**

0. **Wait.** The merge waits for bob's working names of this batch's new keys, sent by the showrunner. The work can start before it.
1. Batch folders: `scripts/aws_target`, `scripts/review_links`, `scripts/style`, `scripts/screen_record`, `scripts/shot_report`, and each folder that no other batch names.
2. For each `tunable` row of this batch in `scripts/lib/config_form_exceptions.py`: add the key to its stem with a comment that says what it controls and its limit line, one blank line between blocks. Use the working name that bob sent through the showrunner.
3. The code reads the key with `conf.read(stem)` at use. Delete the constant.
4. Values stay the same.
5. Remove the moved rows from the allow list. `external` and `internal` rows stay.
6. Tests that patched a constant now write the key in a `CONF_DIR` fixture.

**Files:**
- `scripts/lib/config_form_exceptions.py` — the batch rows name each source file; the rows leave the list
- `scripts/**/*.py` — the source files of the batch rows, and their tests
- `config/*.conf` — the stems that get keys

**Workers:** 2 writers — split by folder.
- `impl` — `scripts/aws_target`, `scripts/review_links` and their stems; hub: `scripts/lib/config_form_exceptions.py` (both slots remove rows)
- `test` opens as impl — `scripts/style`, `scripts/screen_record`, `scripts/shot_report`, the other folders of the batch, and their stems

**State:** none

**Constraints from prior phases:**
- Phase 3 wrote the literal inventory; each `tunable` row of batch "AWS and review tool literals to config" holds the proposed stem, key, limit line.
- Phase 5 made `scripts/aws_target/config.py` find `aws_target.conf` through `CONF_DIR`, with no `AWS_TARGET_CONFIG`.

**Acceptance gate:**
- Each changed module's tests pass.
- `test_config_form.py` passes, and the allow list has no `tunable` row of this batch.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 12 — Production and direct literals to config  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** Each `tunable` literal of the production and direct batch is a commented, limit-checked key in `config/`, read at use, with the same value, and `config/the_list.conf` has the blank-line form.

**Spec:**

0. **Wait.** Start only after item 229 is on main (`scripts/production/the_list.py`, `config/the_list.conf`) and devtools phases busywrite and rundone are on main (`scripts/production/worker.py`, `scripts/direct/assign.py`, `scripts/agents/detached_runs.py`). Merge main first. The merge waits for bob's working names of this batch's new keys, sent by the showrunner.
1. Batch folders: `scripts/production`, `scripts/direct`, `scripts/herdr`, `scripts/agents`, `scripts/accounts`, `scripts/worktree`, `scripts/bevy_upgrade`, `scripts/history`, `scripts/orphans`, `scripts/prioritize`, `scripts/lib`. Examples from P807: `scripts/direct/phase_reservation.py:114` `CHECK_TIMEOUT_SECONDS = 90`, `scripts/production/worker_capacity.py:37` `LOCK_WAIT_SECONDS = 60`.
2. For each `tunable` row of this batch in `scripts/lib/config_form_exceptions.py`: add the key to its stem with a comment that says what it controls and its limit line, one blank line between blocks. Use the working name that bob sent through the showrunner. A List key uses the key form that item 229 put on main for `config/the_list.conf`.
3. The code reads the key with `conf.read(stem)` at use. Delete the constant.
4. Values stay the same.
5. Remove the moved rows from the allow list. `external` and `internal` rows stay.
6. **the_list.conf.** Insert one blank line before each comment line that directly follows a key row in `config/the_list.conf`. Change nothing else. Remove its row from the blank-line allow list. Remove the absent-file case for `config/the_list.conf` from the stale-row loop of `test_repository_form` in `scripts/lib/test_config_form.py`.
7. Tests that patched a constant now write the key in a `CONF_DIR` fixture.

**Files:**
- `scripts/lib/config_form_exceptions.py` — the batch rows name each source file; the rows leave the list
- `scripts/**/*.py` — the source files of the batch rows, and their tests
- `config/*.conf` — the stems that get keys
- `config/the_list.conf` — blank lines
- `scripts/lib/test_config_form.py` — the `config/the_list.conf` absent-file case removed

**Workers:** 2 writers — split by folder.
- `impl` — `scripts/production`, `scripts/herdr`, `config/the_list.conf`, `scripts/lib/test_config_form.py` and their stems; hub: `scripts/lib/config_form_exceptions.py` (both slots remove rows)
- `test` opens as impl — `scripts/direct`, `scripts/agents`, `scripts/accounts`, `scripts/worktree`, `scripts/bevy_upgrade`, `scripts/history`, `scripts/orphans`, `scripts/prioritize`, `scripts/lib` except `scripts/lib/test_config_form.py`, and their stems

**State:** none

**Constraints from prior phases:**
- Phase 3 wrote the literal inventory; each `tunable` row of batch "Production and direct literals to config" holds the proposed stem, key, limit line.
- Phase 1 put `config/the_list.conf` on the blank-line allow list. `test_repository_form` does not report that row as stale while the file is absent; that case is code in the test, near the stale-row check of the file exceptions.
- Phase 5 changed `scripts/direct/assign.py`, `scripts/direct/claude_worker.py` and `scripts/production/staff.py` for `CONF_DIR`.

**Acceptance gate:**
- Each changed module's tests pass.
- `test_config_form.py` passes. The allow list has no `tunable` row of this batch and no blank-line row.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 13 — Runtime state out of config  · status: todo
**Depends on:** Phase 12

#### Work Order

**Goal:** `config/accounts.json`, `config/accounts.lock` and `config/showrunners.lock` live under `~/.local/state/`, every reader and writer uses the new paths on natedev and the Mac, and no live process uses an old path after the switch.

**Spec:**

1. **Paths.** `accounts.json` and `accounts.lock` go to `~/.local/state/accounts/`. `showrunners.lock` goes to `~/.local/state/showrunners/`. If the area already keeps state in another `~/.local/state/` folder, use that folder and name it in the reply.
2. **Readers and writers.** Find each with `grep -rln 'accounts\.json\|accounts\.lock\|showrunners\.lock' scripts`. On the plan base, `accounts.json` appears in `scripts/accounts/worker_names.py`, `scripts/whoami/quota_alert_test_support.py` and `scripts/agents/codex_test_support.py`; `scripts/accounts/accounts.py` and `scripts/production/showrunners.py` own the locks. One function in each area gives the path.
3. **Switch.** At no time may two live processes use different paths for the same lock or state file. The reply states how the switch keeps this rule on both machines. Recommended: on its first read, the new code moves an old `accounts.json` to the new path with an atomic rename when the new path is missing. For each lock, measure the longest hold; if a hold is short, the switch takes the new lock path only.
4. **Cleanup.** After the switch, no old file stays in `config/`. Remove `config/accounts.json` from `.gitignore` (line 23 on the plan base) when nothing writes it.
5. **Format rule.** The form test's format check also fails on a `.json` or `.lock` runtime file in `config/` on disk that is not tracked.
6. **Live check.** After the merge and the Mac pull, the director checks on natedev and the Mac that the accounts table reads and a showrunner lock takes and frees.

**Files:**
- `scripts/accounts/accounts.py`, `scripts/accounts/worker_names.py`, `scripts/production/showrunners.py` — the new paths; their tests also change
- `scripts/whoami/quota_alert_test_support.py`, `scripts/agents/codex_test_support.py`
- `.gitignore`
- `scripts/lib/test_config_form.py`, `scripts/lib/config_form_exceptions.py`

**Workers:** 2 writers — the accounts files and the showrunners lock.
- `impl` — `accounts.json` and `accounts.lock`: `scripts/accounts/` files, the test support files, `.gitignore`; hub: `scripts/lib/config_form_exceptions.py` (both slots may add a row)
- `test` opens as impl — `showrunners.lock`: `scripts/production/showrunners.py` and its tests; the format rule in `scripts/lib/test_config_form.py`

**State:** moves `accounts.json`, `accounts.lock` and `showrunners.lock` on natedev and the Mac.

**Constraints from prior phases:**
- Phase 5 made `scripts/production/showrunners.py` find `showrunners.conf` through `CONF_DIR`, with no `SHOWRUNNERS_CONFIG`.
- Phase 7 moved `config/accounts.toml` `[trust] folders` to `config/accounts.conf` `trust.folders`.
- Phase 1 format check reads tracked files only; Spec item 5 adds the untracked runtime file case. `check_format(root, files)` judges only the paths it gets, and `test_repository_form` passes `git ls-files -z config/`. The untracked case needs its own file list from the `config/` folder on disk, and its own planted row in `test_planted_violations`.

**Acceptance gate:**
- The account and showrunner tests pass.
- `test_config_form.py` passes, and a planted untracked `config/x.lock` fails the format check.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.
- The live check of Spec item 6 passes on both machines.
