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

Proposal steps to plan phases: step 1 → Phases 1 and 3; step 2 → Phase 2; step 4 → Phases 4 and 5; step 3 → Phase 6; step 5 → Phase 7; step 6 → Phase 8; step 7 → Phases 10 to 13; step 8 → Phase 14. Phase 9 is an add-on from Phase 1 (showrunner, 11-23 17:52 PST). Phase 15 is an add-on from Phase 3 (showrunner, 11-23 19:16 PST).

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
  - `scripts/agents/default_models.sh` — the `/default_models` writer; it calls `agents_set_row` in `scripts/agents/models_config.sh`.
  - `scripts/agents/set_model.py` — the `/set_model` writer: a staff role's model default.
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

### Phase 3 — Code checks and the literal inventory  · status: done

#### As-built

- `scripts/lib/test_config_form.py` has three code checks in the Phase 1 form, over the tracked non-test files under `scripts/`. One parse of each source file (`_CodeScan`) serves all three.
  - `check_config_readers` (rule `reader`): a Python or shell read of a `config/` file outside `conf.read`, `conf.read_or_repository`, `conf_value` and the `config/models.conf` parser. The shell scan (`_SHELL_READ`) finds a line with `awk`, `cat`, `cut`, `grep`, `head`, `mapfile`, `rg`, `sed`, `source` or `tail`, or a `<` redirect, whose operand names a config file directly or through a variable assigned earlier in the file. It does not find `jq --rawfile` or `--slurpfile` reads.
  - `check_config_variables` (rule `variable`): an environment variable read whose name ends in `_CONFIG` or `_CONFIG_FILE`, except `CONF_DIR`.
  - `check_tunable_literals` (rule `literal`): a module-level number constant named `*_MAX*`, `MAX_*`, `*_ATTEMPTS*`, `*_LIMIT*`, `*_BYTES`, `*_CHARS` or `*_RETRIES`. Wait limits are skipped; list item 277 owns them.
- `check_format` also reports each `.conf` line that is not blank, a comment, a `[section]` line or a `key=value` row.
- `scripts/lib/config_form_exceptions.py` holds the row types and their lists. Each row has `until: ExceptionLifetime | RemoveInPhase`; `test_repository_form` fails on a stale row.
  - `ReaderException(path, reads, reason, until)`: `reads` holds the base names of the config files the reader reads, or `*` for a path the code computes. A row covers only reads of those files.
  - `VariableException(name, reason, until)`.
  - `TunableLiteral(path, name, value, stem, key, limit, batch: RemoveInPhase)`, `ExternalLiteral(path, name, reason)` and `InternalLiteral(path, name, reason)` in `LITERAL_INVENTORY`: 35 tunable, 8 external and 24 internal rows. Each tunable row holds bob's working stem and key (11-23 19:17 PST); upper limits use the form `max_<thing>_<unit>`.
- Permanent reader rows also cover `scripts/accounts/new_account.py` (statusline.conf), `scripts/aws_target/hana_render.py` (four files for a guest host), `scripts/direct/pass_record.py` (hashes models.conf and direct.conf), `scripts/lint/lint_config.sh` (lint.conf), `scripts/lib/waiting_rows.py` (models.conf) and the data readers.
- `config/README.md` rule 4: a key part that is a name the code looks up is spelled as that name. Those `KEY_FORM_KEYS` rows are permanent; the uppercase-key rows stay `NO_REMOVAL_PHASE` (sdlc owns that rename).

**Files:**
- `scripts/lib/test_config_form.py` — the three code checks, the format row rule, their tree test and planted fixtures
- `scripts/lib/config_form_exceptions.py` — the row types, the reader, variable and literal allow lists
- `config/README.md` — the rule 4 data name line

**Binds later work:** each phase that converts a reader, removes a variable or moves a literal removes its row in the same change. The `config/context_deferral.conf` `FORMAT_FILES` row and the `stop-context-deferral.py` reader row belong to "Context deferral rules to the common form". The 16 `COMMENT_LIMIT_FILES` rows belong to "Comments and limit lines for every setting".

**Gotchas:**
- `test_repository_form` takes 6 to 12 s; the floor is `ast.parse` of about 181 files.
- The `user_zone.py`, `stall_watch.py` and `rebuild_row.py` reader rows read `*`: each finds its file through an environment variable or a computed constant.
- `scripts/buildlog/rust_release.py` reads `config/lint.conf`.

**Ruled out:** a token-rule redesign of the literal scan (the AST scan with scope analysis is correct and fast enough).

### Phase 4 — Raw readers to conf.read  · status: done

#### As-built

- `scripts/notify/review.py`, `scripts/buildlog/rust_release.py`, `scripts/production/stall_watch.py`, `scripts/message/repeat_guard.py`, `scripts/hooks/mac_files.py`, `scripts/lib/user_zone.py` and `scripts/statusline/rebuild_row.py` read through `conf.read` at use. `scripts/build_hold/cargo` reads `config/build_folders.conf` through `conf_value`.
- `REVIEW_TIMEOUT_SECONDS` and the user zone have no code default. A bad, missing or unknown value uses the Git recovery of `conf.read`.
- `STALL_WATCH_CONFIG`, `SEND_REPEAT_CONFIG` and `USER_ZONE_CONF` do not exist. Tests set `CONF_DIR`.
- `rebuild_row.read(config: str | Path = "statusline")` resolves the path at use, and so do `statusline_rows.py`, `assignment.py` and `for_you.py`. `rebuild_row.DEFAULT_CONFIG` does not exist.
- `scripts/statusline/statusline.sh` reads `statusline.conf` and `user_zone.conf` from `${CONF_DIR:-<repository config folder>}`. Its reader row is permanent.
- The shell reader check finds `jq --rawfile` and `jq --slurpfile` reads (`_SHELL_JQ_FILE_READ`), with a quoted or bare flag, and skips quoted text.
- `config/notify.conf` and `config/send_repeat_guard.conf` carry limit lines and have no `COMMENT_LIMIT_FILES` row.
- A bad live `stall_watch` value prints a diagnostic and gives sender pass result 1. `repeat_guard` fails open on an unusable key.

**Files:**
- `scripts/notify/review.py`, `scripts/buildlog/rust_release.py`, `scripts/production/stall_watch.py`, `scripts/message/repeat_guard.py`, `scripts/hooks/mac_files.py`, `scripts/lib/user_zone.py`, `scripts/lib/zone.py`, `scripts/statusline/rebuild_row.py` — readers through `conf.read`
- `scripts/build_hold/cargo` — `conf_value build_folders` reads
- `scripts/statusline/statusline.sh` — the `CONF_DIR` paths
- `scripts/lib/test_config_form.py` — the `jq` file read check
- `scripts/lib/config_form_exceptions.py` — eight reader rows and two variable rows removed; the `statusline.sh` row permanent
- `scripts/lib/checkout_test_support.py` — fixture trees copy `test_config_form.py` with the registry fixture
- `scripts/aws_target/test_hana_render.py` — the `UNSHIPPED_IMPORTS` row names `lib/models_registry.py`
- `config/notify.conf`, `config/send_repeat_guard.conf` — limit lines

**Binds later work:** each config path resolves at use, because a module-level path goes stale when a test sets `CONF_DIR` after import. Fixture trees copy the helpers in the `checkout_test_support.py` helpers list. The `conf.py` top-level imports add 33 ms to the configured `user_zone` CLI (34 to 67 ms); "User zone helper start time" removes them.

**Gotchas:**
- `scripts/message/notifier.sh` is zsh: check it with `zsh -n`, not `bash -n`.
- Added costs: cargo wrapper fast path +4.6 ms; status line +8.8 ms wall and +0.3 ms CPU.

**Ruled out:**
- A packaged-defaults fallback in `stall_watch` when no committed limit is usable: the old code raised in the same case.
- A Hana row that ships `models_registry.py` with `conf.py`: no guest config has an `# agent:` limit.

### Phase 5 — Per-file config environment variables to CONF_DIR  · status: todo
**Depends on:** Phase 4

#### Work Order

**Goal:** Code and tests find `models.conf`, `copies.conf`, `staff.conf`, `aws_target.conf`, `build_targets.conf`, `showrunners.conf` and `lint.conf` through `CONF_DIR` or the repository config folder, with no per-file environment variable.

**Spec:**

0. **Wait.** Start only after devtools phases busywrite and rundone are on main (they change `scripts/direct/assign.py`). Merge main first.
1. For each of `MODELS_CONFIG_FILE`, `COPIES_CONFIG`, `STAFF_CONFIG`, `AWS_TARGET_CONFIG`, `BUILD_TARGETS_CONFIG`, `SHOWRUNNERS_CONFIG` and `LINT_CONFIG_FILE`, first confirm that it names a file in `config/`.
2. **Config file.** The reader takes the file from `CONF_DIR` when it is set, else from the repository `config/` folder: the same rule as `conf.read`. Remove the variable from code and tests. Tests write the fixture into `CONF_DIR` with `write_config_fixture`.
3. **Not a config file.** If a variable names a file outside `config/`, leave it. Move its allow list row to `permanent` with the reason "not a config file" and name it in the reply.
4. **Runtime use.** If a variable carries a purpose beyond tests, keep that purpose and name it in the reply. Example to check first: `scripts/lib/models_registry.py` `_snapshot` (line 66) may pin a launched process to a registry snapshot through `MODELS_CONFIG_FILE`. A launched worker must read the same registry that it reads today. If the purpose needs the variable, keep the variable and move its row to `permanent` with the reason.
5. `scripts/lib/models_registry.py` `registry_path` (line 262) is the one place that resolves the `models.conf` path. The other sources call it: `scripts/direct/assign.py`, `scripts/direct/pass_record.py`, `scripts/direct/assign_claude.py`, `scripts/direct/claude_worker.py`, `scripts/lib/waiting_rows.py`, `scripts/whoami/quota_alert.py`, `scripts/agents/models_config.sh`, `scripts/agents/sync_codex_catalog.sh`.
6. Source files for the other variables: `COPIES_CONFIG` in `scripts/lib/build_folders.py` and `scripts/copies/copies.py`; `STAFF_CONFIG` in `scripts/production/staff.py`; `AWS_TARGET_CONFIG` in `scripts/aws_target/config.py`; `BUILD_TARGETS_CONFIG` in `scripts/build_hold/override_guard.py`, `scripts/build_hold/override_guard.sh`, `scripts/mac_test/mac_test.py` and `scripts/buildlog/record.py`; `SHOWRUNNERS_CONFIG` in `scripts/production/showrunners.py`; `LINT_CONFIG_FILE` in `scripts/lint/lint_config.sh` (line 22, reads and writes `lint.conf`) and `scripts/direct/verify.sh` (line 743, a build input), and in test files (`scripts/build_queue/admission_test_support.py`, `scripts/direct/assign_launcher_test_support.py`, `scripts/direct/review_launcher_test_support.py` and their tests). A test that writes `lint.conf` through it writes the fixture into `CONF_DIR`.
7. Test files: list them with `grep -rlw <NAME> scripts`. On the plan base: `MODELS_CONFIG_FILE` 42 files, `COPIES_CONFIG` 20, `STAFF_CONFIG` 21, `AWS_TARGET_CONFIG` 4, `BUILD_TARGETS_CONFIG` 15, `SHOWRUNNERS_CONFIG` 13. `MODELS_CONFIG_PUBLISH=0` stays in tests (`scripts/direct/recorder_fixture.py:196-198`).
8. Remove each removed variable from the allow list in `scripts/lib/config_form_exceptions.py`.

**Files:**
- `scripts/lib/models_registry.py`, `scripts/direct/assign.py`, `scripts/direct/pass_record.py`, `scripts/direct/assign_claude.py`, `scripts/direct/claude_worker.py`, `scripts/lib/waiting_rows.py`, `scripts/whoami/quota_alert.py`, `scripts/agents/models_config.sh`, `scripts/agents/sync_codex_catalog.sh`, `scripts/direct/recorder_fixture.py`
- `scripts/lib/build_folders.py`, `scripts/copies/copies.py`, `scripts/production/staff.py`, `scripts/aws_target/config.py`, `scripts/build_hold/override_guard.py`, `scripts/build_hold/override_guard.sh`, `scripts/mac_test/mac_test.py`, `scripts/buildlog/record.py`, `scripts/production/showrunners.py`, `scripts/lint/lint_config.sh`, `scripts/direct/verify.sh`
- `scripts/**/test_*.py`, `scripts/**/*_test_support.py` — the files that set each variable (Spec item 7)
- `scripts/lib/config_form_exceptions.py` — rows removed

**Workers:** 2 writers — split by variable.
- `impl` — `MODELS_CONFIG_FILE`: its sources, `scripts/direct/recorder_fixture.py` and its tests, and `scripts/direct/assign_launcher_test_support.py` with both of its fixture changes (`MODELS_CONFIG_FILE` and `LINT_CONFIG_FILE`); hub: `scripts/lib/config_form_exceptions.py` (this slot owns every edit to it; the other slot sends its row removals here)
- `test` opens as impl — `COPIES_CONFIG`, `STAFF_CONFIG`, `AWS_TARGET_CONFIG`, `BUILD_TARGETS_CONFIG`, `SHOWRUNNERS_CONFIG`, `LINT_CONFIG_FILE`: their sources (with `scripts/lint/lint_config.sh` and `scripts/direct/verify.sh`) and tests, except `scripts/direct/assign_launcher_test_support.py`

**State:** none

**Constraints from prior phases:**
- Phase 3 put the seven variables on the variable allow list under "Per-file config environment variables to CONF_DIR". `CODEX_CONFIG_FILE` is a permanent row: it names the Codex CLI file outside `config/`.
- Phase 4 removed `SEND_REPEAT_CONFIG` and `STALL_WATCH_CONFIG`; their tests use `CONF_DIR` with `write_config_fixture`, which is the pattern for this phase.
- Phase 4: `conf.path(stem)` and `conf.read(stem)` read `CONF_DIR` on each call. A module-level path constant goes stale when a test sets `CONF_DIR` after import, so resolve each path at use (Phase 4 removed `rebuild_row.DEFAULT_CONFIG` for this reason).
- Fixture trees copy a fixed list of test helper modules: `helpers` in `scripts/lib/checkout_test_support.py` (line 26). When a copied test imports a new helper, add the helper to that list.
- `scripts/aws_target/test_hana_render.py` checks that each Hana guest file ships its imports. `UNSHIPPED_IMPORTS` lists the lazy `lib/conf.py` → `lib/models_registry.py` import. A new import between guest files needs a manifest file or a row; a change to a file in `hana_image.files` updates `hana_image.sha256`.

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

0. **Wait.** Start only after nate's go for the eight-row move below, typed in this unit's session or sent by the showrunner as nate's words. If nate refuses this move, the `TAIL_BYTES` literal of item 8 returns to the production and direct batch with a new working name from bob.
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

   # Bytes of program output that the hand made check keeps for its offline review.
   # whole: 1..
   hand_made.output_tail_bytes=65536
   ```

   Rename map (full names): `[cli.codex] merge_branch` → `cli.merge_branch.codex.model`; `[cli.codex] interactive` → `cli.interactive.codex.model`; `[cli.claude] merge_branch` → `cli.merge_branch.claude.model`; `[cli.claude] interactive` → `cli.interactive.claude.model`; `[cli.hand_made] claude.model` → `cli.hand_made.claude.model`; `[cli.hand_made] claude.role` → `cli.hand_made.claude.role`; `[cli.hand_made] check_time_limit_seconds` → `cli.hand_made.check_time_limit_seconds`; `[cli.hand_made] max_script_bytes` → `cli.hand_made.max_script_bytes`.
2. **models.conf.** Remove the `[cli.codex]`, `[cli.claude]` and `[cli.hand_made]` sections and their comments from `config/models.conf` (lines 74-97 after Phase 1). `[assignments] cli=codex` stays in `config/models.conf`: it picks the family, like the other assignments.
3. **Readers.**
   - `scripts/lib/models_registry.py` `launch_settings` (line 185): a `cli` key reads `conf.read("cli")` key `<task>.<family>.model`.
   - `scripts/agents/models_config.sh` (the resolver near line 640) and `scripts/cli_agent/cli_agent.sh`: read `conf_value cli <task>.<family>.model`.
   - `scripts/production/hand_made.py` `read_settings` (lines 136-162): read `conf.read("cli")` keys `hand_made.claude.model`, `hand_made.claude.role`, `hand_made.check_time_limit_seconds`, `hand_made.max_script_bytes`. Remove the checks that the limit lines now make (model:effort form, role choice, number and more than 0). Keep the `model:effort` check of `hand_made.claude.model`: the `# agent: claude` limit accepts a bare model, and `scripts/notify/review.py` `structured_call` always passes `--effort`. Its refusal names the key: `cli.hand_made.claude.model must be model:effort`. Add no new limit form (showrunner, 11-23 19:37 PST). `/tune` accepts a bare model that this check refuses; that gap exists today and stays. Keep each refusal message clear; a `conf` limit error becomes a `Refused` with the key name. Remove `SECTION`.
4. **Writer.** `/default_models` runs `scripts/agents/default_models.sh`, which lists and edits rows through `scripts/agents/models_config.sh` and `models_registry_edit`. For the `cli` task these paths read and write `config/cli.conf` rows `<task>.<family>.model`, not the `[cli.<family>]` sections:
   - listing: `default_models.sh` (line 41, `_models_config_section_values "$fn.$family"`) and `agents_list_function` (line 706);
   - one row: `agents_set_row` (line 1022);
   - a model replaced across functions: `agents_set_model` (line 901);
   - family switch: `/default_models cli claude` still edits only `[assignments] cli` in `config/models.conf`.

   A `cli.conf` row is published through the `/tune` path: `scripts/lib/live_settings.py` `stage_save` with a `WaitingChange`, then the publish that `scripts/lib/tune.py` runs, with the `/default_models` command as the reason. One command that changes rows in both files first checks every new value (`live_settings.check_value_line`, the `# agent:` limit, the registry validator). When one check fails, it writes neither file. Then it writes `config/models.conf` through `models_registry_edit` and `config/cli.conf` through the `/tune` path.
5. **Docs.** `commands/default_models.md`: the examples and the paragraph at lines 106-111 name `config/cli.conf` keys, not `[cli.claude]`. `commands/build/hand_made.md`: name `config/cli.conf`. `config/README.md`: rule 6 adds "`# agent: claude` or `# agent: codex` checks an `agent[:effort]` value against that family in `config/models.conf`."; rule 11 adds "The `cli` rows live in `config/cli.conf`."; the `## models.conf` section drops the cli rows; add a `## cli.conf` section with the key form `<tool>.<family>.<field>` or `<tool>.<setting>`, and "A new CLI tool copies a block and changes `<tool>`; it adds a `.role` row only when its code reads one."
6. **Tests.** Fixtures write `cli.conf` in `CONF_DIR`: `scripts/whoami/quota_alert_test_support.py`, `scripts/agents/test_agent_exec.py`, `scripts/agents/test_agent_exec.sh`, `scripts/production/test_hand_made.py` and the other `scripts/production/test_hand_made_*.py` that build a registry, `scripts/lib/test_models_registry.py`, `scripts/lib/test_models_registry_fixture.py`. Add cases through `default_models.sh`: `/default_models cli.merge_branch sonnet` writes `merge_branch.claude.model` in `cli.conf`; the status listing shows the `cli` rows; `/default_models cli claude` changes only `[assignments] cli`; a model replace across functions changes the matching `cli.conf` rows; a value with a line separator is refused and leaves both files unchanged; `hand_made.claude.model=opus` (no effort) is refused by the hand made reader with the key name.
7. **Proof.** Record `bash scripts/cli_agent/cli_agent.sh --status` before the change. After the change it prints the same agents.
8. **Output tail.** `scripts/production/hand_made_limit.py` `TAIL_BYTES` (line 44, `64 * 1024`) becomes the `hand_made.output_tail_bytes=65536` row above (bob's working name `cli.hand_made.output_tail_bytes`), read at use. Its `TunableLiteral` row (batch "config/cli.conf") leaves the inventory. No test reads the live value; each test writes its own fixture config. `scripts/production/hand_made_guest.py` makes an `OutputTail` on the controller for the guest's streamed output, so a read in `OutputTail.write()` would repeat for each chunk. Read the key once on the controller before the attempt starts, and pass the bounded value to each `OutputTail`. Change the natedev and guest routes, and test both with the same fixture.

**Files:**
- `config/cli.conf` — new
- `config/models.conf` — `[cli.*]` sections removed
- `scripts/lib/models_registry.py`, `scripts/agents/models_config.sh`, `scripts/agents/default_models.sh`, `scripts/cli_agent/cli_agent.sh`
- `scripts/production/hand_made.py`, `scripts/production/hand_made_limit.py`, `scripts/production/hand_made_guest.py`
- `scripts/lib/config_form_exceptions.py` — the `TAIL_BYTES` row removed
- `commands/default_models.md`, `commands/build/hand_made.md`, `config/README.md`
- `scripts/whoami/quota_alert_test_support.py`, `scripts/agents/test_agent_exec.py`, `scripts/agents/test_agent_exec.sh`, `scripts/production/test_hand_made.py`, `scripts/production/test_hand_made_guest.py`, `scripts/production/test_hand_made_limit.py`, `scripts/production/test_hand_made_mac.py`, `scripts/lib/test_models_registry.py`, `scripts/lib/test_models_registry_fixture.py`

**Workers:** 2 writers — the launcher side and the hand made side.
- `impl` — `config/cli.conf`, `config/models.conf`, `scripts/lib/models_registry.py`, `scripts/agents/models_config.sh`, `scripts/agents/default_models.sh`, `scripts/cli_agent/cli_agent.sh`, `commands/default_models.md`, `config/README.md`, `scripts/lib/test_models_registry.py`, `scripts/lib/test_models_registry_fixture.py`, `scripts/agents/test_agent_exec.py`, `scripts/agents/test_agent_exec.sh`, `scripts/whoami/quota_alert_test_support.py`; hub: `config/cli.conf` (both sides read it)
- `test` opens as impl — `scripts/production/hand_made.py`, `scripts/production/hand_made_limit.py`, `scripts/production/hand_made_guest.py`, `scripts/lib/config_form_exceptions.py`, `commands/build/hand_made.md`, `scripts/production/test_hand_made*.py`

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
- `impl` — the `agent_notes` and `herdr_upkeep` files and their readers and tests; hub: `scripts/lib/config_form_exceptions.py` (this slot owns every edit to it; the other slot sends its row removals here)
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
4. **design_roles.** `scripts/astra/design_roles.py` uses `configparser`. It changes to `conf.read("design_roles")` keys `host` and `new_open`. `host` keeps both uses: routing and the local authority check. `new_open` gets the limit line `# one of: true, false`; the code reads `true` as open. Today `getboolean(..., fallback=True)` (line 825) also accepts `yes`, `on` and `1` and opens when the key is missing; after the change a missing or other value is a `conf.read` refusal, with Git recovery, like every key (rule 7: no default in code). The live value is `true`, so behavior stays the same. Remove `DESIGN_ROLES_CONFIG`; tests use `CONF_DIR`, and `scripts/astra/design_role_test_support.py` writes the fixture.
5. **Proof.** Before the change, a script reads every key of each file with `conf.read` and with each section-aware reader, and saves the values. After the change the same script gives the same values.
6. Remove the seven files from the sections allow list. Replace the `design_roles.py` reader row with a `PERMANENT` row for `designer.md` only: line 710 still reads `config/design_roles/designer.md` as data. Remove `DESIGN_ROLES_CONFIG` from `VARIABLE_EXCEPTIONS`.
7. **README.** Update the sections of `config/README.md` that show these files with `[section]` lines (for example `## lint.conf`).

**Files:**
- `config/build_admission.conf`, `config/build_folders.conf`, `config/build_steps.conf`, `config/lint.conf` — group comments; their section-aware readers from Spec item 3 and their tests also change
- `config/gate_rename_deploy.conf`, `config/copies.conf`, `config/design_roles.conf`
- `scripts/astra/design_roles.py`, `scripts/astra/test_design_roles.py`, `scripts/astra/test_design_roles_sources.py`, `scripts/astra/design_role_test_support.py`, `scripts/herdr/worker_panes.py`
- `scripts/lib/config_form_exceptions.py`, `config/README.md`

**Workers:** 2 writers — split by file group.
- `impl` — `config/build_admission.conf`, `config/build_folders.conf`, `config/build_steps.conf`, `config/lint.conf` and their readers and tests
- `test` opens as impl — `config/gate_rename_deploy.conf`, `config/copies.conf`, `config/design_roles.conf`, `scripts/astra/design_roles.py`, `scripts/astra/design_role_test_support.py`, `scripts/herdr/worker_panes.py` and their tests, `config/README.md`; hub: `scripts/lib/config_form_exceptions.py` (this slot owns every edit to it; the other slot sends its row removals here)

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

### Phase 9 — Comments and limit lines for every setting  · status: todo
**Depends on:** Phase 6, Phase 8

#### Work Order

**Goal:** Every key row of the files on the comment-limit allow list sits in a block that starts with a comment, every number key has a limit line directly above it, and `COMMENT_LIMIT_FILES` is gone.

**Spec:**

1. **Files.** The `FileException` rows of `COMMENT_LIMIT_FILES` in `scripts/lib/config_form_exceptions.py` at the phase start. After Phase 1 there were 16 files and 207 violations: `config/build_folders.conf` 49, `config/build_steps.conf` 49, `config/console.conf` 35, `config/models.conf` 30, `config/copies.conf` 10, `config/send_repeat_guard.conf` 9, `config/statusline.conf` 7, `config/build_targets.conf` 4, `config/stall_watch.conf` 4, `config/aws_target.conf` 2, `config/design_roles.conf` 2, `config/mac_linux.conf` 2, `config/lint.conf` 1, `config/memory_feed.conf` 1, `config/notify.conf` 1, `config/production.conf` 1. Phase 4 removed the `config/notify.conf` and `config/send_repeat_guard.conf` rows, which leaves 14 files. Phases 6 and 8 remove more. List the lines with `check_comment_limits` from `scripts/lib/test_config_form.py`.
2. **Comments.** Put a comment line above each key block that has none. It says what the keys control; read the reader code to say it. Keep one blank line between blocks (rule 12).
3. **Limits.** Put a `# whole: A..B` or `# range: A..B` line directly above each key whose value is a number. Put `# one of: a, b` above a key whose reader accepts a closed set of words. The limit states what the reader accepts today; read its checks. When the reader checks nothing, use the limit the code needs to work, for example `# whole: 1..` for a count, and name each such choice in the reply. Every live value passes its limit.
4. **No key or value changes.** `git diff -U0 -- config/` adds only comment lines and blank lines. A limit line on an existing wait-time key states today's check; it adds no key and no name (Delegation Context wait-limit invariant).
5. **models.conf.** The comments go above the registry blocks. The registry writers (`scripts/lib/registry_rows.py`, `scripts/lib/models_registry.py`) and `scripts/agents/sync_codex_catalog.sh` must keep them. Run a registry write and the catalog sync on a fixture with the new comments, then run `check_comment_limits` on the output. If a writer drops a comment, fix that writer and add that run as a test row.
6. Remove each `COMMENT_LIMIT_FILES` row as its file passes. At the end remove `COMMENT_LIMIT_FILES`; the comment and limit rule of `test_repository_form` takes no exceptions.

**Files:**
- `config/*.conf` — comment and limit lines only, in the files of Spec item 1
- `scripts/lib/config_form_exceptions.py` — `COMMENT_LIMIT_FILES` removed
- `scripts/lib/test_config_form.py` — the comment and limit rule with no exceptions
- `scripts/lib/registry_rows.py`, `scripts/lib/models_registry.py`, `scripts/agents/sync_codex_catalog.sh`, `scripts/lib/test_models_registry.py`, `scripts/agents/test_sync_codex_catalog.sh` — only if a writer drops a comment

**Workers:** 2 writers — split by file.
- `impl` — `config/models.conf`, `config/build_folders.conf`, `config/copies.conf`, `config/design_roles.conf`, `config/lint.conf`, and any registry writer fix with its test row; hub: `scripts/lib/config_form_exceptions.py` and `scripts/lib/test_config_form.py` (`test` messages this slot for each row to remove)
- `test` opens as impl — `config/build_steps.conf`, `config/console.conf`, `config/statusline.conf`, `config/build_targets.conf`, `config/stall_watch.conf`, `config/aws_target.conf`, `config/mac_linux.conf`, `config/memory_feed.conf`, `config/production.conf`

**State:** none

**Constraints from prior phases:**
- Phase 1 rule 6 and `check_comment_limits`: a key block is the comment lines directly above a run of key rows. Each key row is in a block that starts with a comment. Each number key has `# whole:` or `# range:` directly above it. `_is_number` counts each finite decimal, of any size.
- `conf.py` reads a limit line only directly above its key and checks the value on each read, so a wrong limit makes the read fail.
- Phase 1: the registry writers apply `registry_rows.space_groups` on each write.
- Phase 2: `# agent: claude` and `# agent: codex` are limit kinds for agent values.
- Phase 4 put limit lines in `config/notify.conf` and `config/send_repeat_guard.conf` and removed their `COMMENT_LIMIT_FILES` rows. Phase 6 removed the `[cli.*]` sections from `config/models.conf`. Phase 8 put `# <section>` comments in place of the `[section]` lines of five of these files, and removed the rows of the files that it made pass.

**Acceptance gate:**
- `test_config_form.py` passes, and `COMMENT_LIMIT_FILES` is gone.
- `git diff -U0 -- config/` shows only added comment and blank lines.
- A script checks each stored value of each changed stem against its limit line, host overrides included, with Git recovery off. (`check_comment_limits` checks only that a limit line exists, and a `conf.read` with recovery can return an older value.) `config/models.conf` is checked by section through the model registry reader. The proof also covers the four Phase 10 keys: `head_stall.times_tail_bytes`, `disk.min_outside_directory_bytes`, `report.edited_crate_max_crates`, `nextest.summary_scan_bytes`.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file; `bash -n` passes on each changed shell file.

### Phase 10 — Build tool literals to config  · status: done

#### As-built

- Four keys replace the batch constants, with the same values. `build_admission` `head_stall.times_tail_bytes=1048576` (`whole: 1..`). `buildlog` `disk.min_outside_directory_bytes=1073741824` (`whole: 1..`). `buildlog` `report.edited_crate_max_crates=3` (`whole: 1..48`, because the cascade row ends at 49 crates). `mac_test` `nextest.summary_scan_bytes=8192` (`whole: 1..1048576`).
- `report.RebuildClasses` holds the one rebuild bin rule: `read()`, `labels()` and `label(crates_compiled, warm)`. `rebuild_timings`, `package_rebuild_rows`, `rebuilds_section` and `profile_step_durations` take `classes`.
- `head_stall.last_admission(times, settings: conf.ConfigRead)` takes the `build_admission` read that its caller already made.
- `disk.outside_directories` reads its limit at use.
- `nextest_log.replay_log` reads the Summary scan limit only after a failure (`if status:`). `run_logged` catches `(OSError, conf.Unusable)`, prints a diagnostic and keeps the nextest status and the log.
- The four `tunable` rows left `scripts/lib/config_form_exceptions.py`.

**Files:**
- `config/build_admission.conf`, `config/buildlog.conf`, `config/mac_test.conf` — the four keys
- `scripts/build_queue/head_stall.py`, `scripts/buildlog/disk.py`, `scripts/buildlog/report.py`, `scripts/mac_test/nextest_log.py` — readers at use
- `scripts/build_queue/test_head_stall.py`, `scripts/buildlog/test_disk.py`, `scripts/buildlog/test_report.py`, `scripts/buildlog/test_moments.py`, `scripts/mac_test/test_offload_cleanup.py` — `CONF_DIR` fixtures
- `scripts/lib/config_form_exceptions.py` — four rows removed
- `docs/as-built/build-followups.md`, `docs/as-built/build-targets.md`, `docs/plans/builds-resources.md` — key names replace the constant names

**Binds later work:** the four key names and limits. The follow-up "Build tool limits follow-ups" passes one `RebuildClasses` through each report and adds a Mac replay row with an unusable scan limit.

**Gotchas:**
- A test spy on a changed function survives the change: `test_moments.py` kept a spy on the old `profile_step_durations` call form. Search the tests for each changed function.
- Current docs named the deleted constants. Search `docs/` for each moved name.
- An open upper limit lets `readline` take `2**63` bytes; each byte limit that reaches a read needs an upper bound.

**Ruled out:** a command-line argument for the Summary scan limit. It changes the `run TAIL SUMMARY COMMAND...` form, and macOS gets no copy of the script for each run.

### Phase 11 — Message and notice literals to config  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** Each `tunable` literal of the message and notice batch is a commented, limit-checked key in `config/`, read at use, with the same value, and its allow list row is gone.

**Spec:**

0. **Names.** The `TunableLiteral` rows hold bob's working names (11-23 19:17 PST). The batch merges with them; nate reviews the names later, and a later change is a rename.
1. Batch folders: `scripts/notify`, `scripts/message`, `scripts/hooks`, `scripts/usage`, `scripts/whoami`, `scripts/maintenance`. Examples from P807: `scripts/notify/auto_silence.py:54` `JOB_TIMEOUT_SECONDS = 300`. `scripts/notify/pushover.py:79` `TITLE_MAX` is `external` and stays.
2. For each `tunable` row of this batch in `scripts/lib/config_form_exceptions.py`: add the key to its stem with a comment that says what it controls and its limit line, one blank line between blocks. Use the stem and key of the row. The config text is the numeric result of `TunableLiteral.value` (`1 << 20` is `1048576`); check that the typed reader returns the original number. A literal of the batch that is a wait limit stays in the code for item 277; name it in the reply.
3. The code reads the key with `conf.read(stem)` at use. Delete the constant.
4. Values stay the same.
5. Remove the moved rows from the allow list. `external` and `internal` rows stay.
6. Tests that patched a constant now write the key in a `CONF_DIR` fixture. No test reads the live value of a moved key; each test writes its own fixture config.
7. Hooks run on each tool call. A hook reads its key once for each run, never at import.
8. **Bounds.** Both context hooks use `scripts/hooks/context_usage.py`. Give `context_usage.transcript.tail_bytes` the limit `# whole: 1..4194304`, so the first scan is never larger than the 4 MiB `TAIL_BYTES_RETRY`, which stays in the code. Each hook reads the key on the host that runs it. Give `notify.pushover.max_source_chars` an upper bound before it reaches the source slices.
9. **References.** Search `docs/` and `scripts/` for each moved name and each changed function. Update current docs, fixtures and test spies. Run every test module with a changed reference.

**Files:**
- `scripts/lib/config_form_exceptions.py` — the batch rows name each source file; the rows leave the list
- `scripts/**/*.py` — the source files of the batch rows, and their tests
- `config/*.conf` — the stems that get keys
- `docs/as-built/usage-ledger.md` — names a moved constant

**Workers:** 2 writers — split by reader.
- `impl` — the context hooks (`scripts/hooks/context_usage.py` and its callers) and Pushover (`scripts/notify/pushover.py`), their tests and stems; hub: `scripts/lib/config_form_exceptions.py` (this slot owns every edit to it; the other slot sends its row removals here)
- `test` opens as impl — waste watch (`scripts/usage/waste_watch.py`) and the Codex pacer (`scripts/whoami/codex_pacer.py`), their tests and stems, and `docs/as-built/usage-ledger.md`

**State:** none

**Constraints from prior phases:**
- Phase 3 wrote the literal inventory; each `tunable` row of batch "Message and notice literals to config" holds the proposed stem, key, limit line.
- The batch has four rows: `scripts/hooks/context_usage.py`, `scripts/notify/pushover.py`, `scripts/usage/waste_watch.py`, `scripts/whoami/codex_pacer.py`. No row is in `scripts/message` or `scripts/maintenance`; Phase 7 owns the `scripts/maintenance` TOML readers.
- Phase 4 moved `scripts/notify/review.py` to `conf.read("notify")`. `config/notify.conf` has `# range: 5e-324..` above `REVIEW_TIMEOUT_SECONDS`.
- `scripts/message/notifier.sh` is a zsh script: check it with `zsh -n`, not `bash -n`.
- Phase 4: resolve each config path at use; a module-level path constant goes stale under a test's `CONF_DIR`.
- Phase 10: a test spy on a changed function (`test_moments.py`) and current docs that named moved constants broke after the move. Search for both before the edit round ends. A size key gets an upper bound when the code passes it to a read or a slice (`readline` overflows at 2**63).

**Acceptance gate:**
- Each changed module's tests pass, and each test module with a changed reference passes.
- `test_config_form.py` passes, and the allow list has no `tunable` row of this batch.
- `context_usage.transcript.tail_bytes` has the limit `# whole: 1..4194304`, and `notify.pushover.max_source_chars` has an upper bound.
- A search of `docs/` and `scripts/` finds no moved constant name outside this plan.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 12 — AWS and review tool literals to config  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** Each `tunable` literal of the AWS and review tool batch is a commented, limit-checked key in `config/`, read at use, with the same value, and its allow list row is gone.

**Spec:**

0. **Names.** The `TunableLiteral` rows hold bob's working names (11-23 19:17 PST). The batch merges with them; nate reviews the names later, and a later change is a rename.
1. Batch folders: `scripts/aws_target`, `scripts/review_links`, `scripts/style`, `scripts/screen_record`, `scripts/shot_report`, and each folder that no other batch names.
2. For each `tunable` row of this batch in `scripts/lib/config_form_exceptions.py`: add the key to its stem with a comment that says what it controls and its limit line, one blank line between blocks. Use the stem and key of the row. The config text is the numeric result of `TunableLiteral.value` (`1 << 20` is `1048576`); check that the typed reader returns the original number. A literal of the batch that is a wait limit stays in the code for item 277; name it in the reply.
3. The code reads the key with `conf.read(stem)` at use. Delete the constant.
4. Values stay the same.
5. Remove the moved rows from the allow list. `external` and `internal` rows stay.
6. Tests that patched a constant now write the key in a `CONF_DIR` fixture. No test reads the live value of a moved key; each test writes its own fixture config.
7. **Saved Review items.** `read_submission()` reads saved items with the same image count and evidence limits as new submissions, so a lower `MAX_IMAGES` makes a saved two-image item fail to load. Apply the configured limits to new submissions only. Keep saved submissions readable under the limits in effect before this phase. Keep the saved JSON form. In the domain record, use `NoBugPageState` for absent page state in place of `BugPageState | None`.
8. **Review request limits.** `scripts/review_links/serve.py` derives `MAX_NEW_ITEM_BODY` at import from three moved constants and uses it before `rfile.read(size)`. `scripts/review_links/review_submissions.py` imports three moved constants for image decoding. The dialog also sends these limits to the browser. Read one Review limit set for each request, and use it for the body size, the parse and the image decode. Cap the derived body size before the read. Render the dialog from the same keys.
9. **References.** Search `docs/` and `scripts/` for each moved name and each changed function. Update current docs, fixtures and test spies. Run every test module with a changed reference.

**Files:**
- `scripts/lib/config_form_exceptions.py` — the batch rows name each source file; the rows leave the list
- `scripts/**/*.py` — the source files of the batch rows, and their tests
- `config/*.conf` — the stems that get keys
- `scripts/review_links/review_submissions.py`, `scripts/review_links/test_serve.py`, `scripts/aws_target/test_gpu_size_trial.py`
- `docs/as-built/screen-record.md`, `docs/as-built/style-showrunner.md` — name moved constants

**Workers:** 2 writers — split by folder.
- `impl` — `scripts/aws_target`, `scripts/review_links` and their stems (`review_submissions.py`, `test_serve.py` and `test_gpu_size_trial.py` included); hub: `scripts/lib/config_form_exceptions.py` (this slot owns every edit to it; the other slot sends its row removals here)
- `test` opens as impl — `scripts/style`, `scripts/screen_record`, `scripts/shot_report`, the other folders of the batch, their stems, `docs/as-built/screen-record.md` and `docs/as-built/style-showrunner.md`

**State:** none

**Constraints from prior phases:**
- Phase 3 wrote the literal inventory; each `tunable` row of batch "AWS and review tool literals to config" holds the proposed stem, key, limit line.
- The batch rows sit in `scripts/aws_target/gpu_size_trial.py`, `scripts/review_links/new_item_dialog.py` (10 rows), `scripts/review_links/serve.py`, `scripts/screen_record/screen_record.py` (2), `scripts/shot_report/transcripts.py`, `scripts/style/style_ledger.py` and `scripts/test_times/module_cost.py`. No row is in `scripts/aws_target/config.py`, which Phase 5 changes for `AWS_TARGET_CONFIG`; this phase does not wait for Phase 5.
- Phase 4: resolve each config path at use; a module-level path constant goes stale under a test's `CONF_DIR`.
- Phase 10: a test spy on a changed function and current docs that named moved constants broke after the move. A size key gets an upper bound when the code passes it to a read or a slice.

**Acceptance gate:**
- Each changed module's tests pass, and each test module with a changed reference passes.
- `test_config_form.py` passes, and the allow list has no `tunable` row of this batch.
- A saved two-image Review item opens after the intake image limit drops to one.
- An oversized request is refused before its body is read. `test_serve.py` keeps its body limit, total image limit and URL slice cases.
- A search of `docs/` and `scripts/` finds no moved constant name outside this plan.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 13 — Production and direct literals to config  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** Each `tunable` literal of the production and direct batch is a commented, limit-checked key in `config/`, read at use, with the same value, and `config/the_list.conf` has the blank-line form.

**Spec:**

0. **Wait.** Start only after item 229 is on main (`scripts/production/the_list.py`, `config/the_list.conf`) and devtools phases busywrite and rundone are on main (`scripts/production/worker.py`, `scripts/direct/assign.py`, `scripts/agents/detached_runs.py`). Merge main first. The `TunableLiteral` rows hold bob's working names (11-23 19:17 PST); the batch merges with them.
1. Batch folders: `scripts/production`, `scripts/direct`, `scripts/herdr`, `scripts/agents`, `scripts/accounts`, `scripts/worktree`, `scripts/bevy_upgrade`, `scripts/history`, `scripts/orphans`, `scripts/prioritize`, `scripts/lib`. Examples from P807: `scripts/direct/phase_reservation.py:114` `CHECK_TIMEOUT_SECONDS = 90`, `scripts/production/worker_capacity.py:37` `LOCK_WAIT_SECONDS = 60`.
2. For each `tunable` row of this batch in `scripts/lib/config_form_exceptions.py`: add the key to its stem with a comment that says what it controls and its limit line, one blank line between blocks. Use the stem and key of the row. The config text is the numeric result of `TunableLiteral.value` (`1 << 20` is `1048576`); check that the typed reader returns the original number. A literal of the batch that is a wait limit stays in the code for item 277; name it in the reply. A List key uses the key form that item 229 put on main for `config/the_list.conf`.
3. The code reads the key with `conf.read(stem)` at use. Delete the constant.
4. Values stay the same.
5. Remove the moved rows from the allow list. `external` and `internal` rows stay.
6. **the_list.conf.** Insert one blank line before each comment line that directly follows a key row in `config/the_list.conf`. Change nothing else. Before the row goes, run the item 229 writer (`listadd`, `scripts/production/the_list.py`) on a fixture copy and run `check_blank_lines` on its output. If the writer breaks the form, fix the writer and add a test row that runs it and then `check_blank_lines`. Remove its row from the blank-line allow list. Remove the absent-file case for `config/the_list.conf` from the stale-row loop of `test_repository_form` in `scripts/lib/test_config_form.py`.
7. Tests that patched a constant now write the key in a `CONF_DIR` fixture. No test reads the live value of a moved key; each test writes its own fixture config.
8. **Unused reader.** Before the literal moves, after the main merge of item 0: confirm that `scripts/agents/inventory.py` `last_tool_call()` still has no caller. Then remove it, its `_LAST_STEP_BYTES` constant and its inventory row. Do not add `production.inventory.last_step_scan_bytes`.
9. **Bounds.** Give the `scripts/production/gate.py` `RERUN_LIMIT` key the lower bound 0, and keep the shipped value 8: `test_gate_holds.py` sets it to zero to prove hold recovery with ordinary reruns off; keep that fixture. Bound `worker.history_bytes` (`scripts/production/worker.py` `WORKER_HISTORY_BYTES`) so that `read(limit + 1)` stays in range. Give the nightly diagnostic and board note slices explicit upper bounds.
10. **Guests.** Selected suites reach the rerun policy in the Mac Linux and AWS guest gates. Prove with a test that each guest gate route reads the candidate's `gate.conf`.
11. **References.** Search `docs/` and `scripts/` for each moved name and each changed function. Update current docs, fixtures and test spies. Run every test module with a changed reference.

**Files:**
- `scripts/lib/config_form_exceptions.py` — the batch rows name each source file; the rows leave the list
- `scripts/**/*.py` — the source files of the batch rows, and their tests
- `config/*.conf` — the stems that get keys
- `config/the_list.conf` — blank lines
- `scripts/lib/test_config_form.py` — the `config/the_list.conf` absent-file case removed
- `scripts/production/the_list.py`, `scripts/production/test_the_list.py` — only if the writer breaks the blank-line form
- `scripts/agents/inventory.py` — `last_tool_call()` removed
- `scripts/production/test_gate_holds.py`, `scripts/production/test_gate_suites_reruns.py`, `scripts/production/test_gate_suites_types.py`, `scripts/production/test_worker.py`
- `docs/as-built/merge-gate-and-test-speed.md`, `docs/plans/builds-natest.md` — name moved constants

**Workers:** 2 writers — split by folder. Before edits start, the director assigns each destination stem to one slot in the prompts.
- `impl` — `scripts/production`, `scripts/herdr`, `config/the_list.conf`, `scripts/lib/test_config_form.py`, every edit to `scripts/lib/config_form_exceptions.py`, and the stems assigned to this slot; hub: `scripts/lib/config_form_exceptions.py` (`test` sends its row removals to `impl`)
- `test` opens as impl — `scripts/direct`, `scripts/agents`, `scripts/accounts`, `scripts/worktree`, `scripts/bevy_upgrade`, `scripts/history`, `scripts/orphans`, `scripts/prioritize`, `scripts/lib` except `scripts/lib/test_config_form.py` and `scripts/lib/config_form_exceptions.py`, their tests, and the stems assigned to this slot

**State:** none

**Constraints from prior phases:**
- Phase 3 wrote the literal inventory; each `tunable` row of batch "Production and direct literals to config" holds the proposed stem, key, limit line.
- Phase 1 put `config/the_list.conf` on the blank-line allow list. `test_repository_form` does not report that row as stale while the file is absent; that case is code in the test, near the stale-row check of the file exceptions.
- Phase 5 changed `scripts/direct/assign.py`, `scripts/direct/claude_worker.py` and `scripts/production/staff.py` for `CONF_DIR`.
- The `TAIL_BYTES` row of `scripts/production/hand_made_limit.py` belongs to the `config/cli.conf` phase (Phase 6), not to this batch.
- Phase 10: a test spy on a changed function and current docs that named moved constants broke after the move. A size key gets an upper bound when the code passes it to a read or a slice.

**Acceptance gate:**
- Each changed module's tests pass, and each test module with a changed reference passes.
- `test_config_form.py` passes. The allow list has no `tunable` row of this batch and no blank-line row.
- `test_gate_holds.py` passes with its zero rerun fixture. A test proves that the Mac Linux and AWS guest gate routes read the candidate's `gate.conf`.
- `last_tool_call()` and its inventory row are gone, and no `last_step_scan_bytes` key exists.
- A search of `docs/` and `scripts/` finds no moved constant name outside this plan.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 14 — Runtime state out of config  · status: todo
**Depends on:** Phase 13

#### Work Order

**Goal:** `config/accounts.json` and `config/accounts.lock` live under `~/.local/state/`, every reader and writer uses the new paths on natedev and the Mac, no live process uses an old path after the switch, and the form test refuses an untracked runtime file in `config/`.

**Spec:**

1. **Paths.** `accounts.json` and `accounts.lock` go to `~/.local/state/accounts/`. No code uses `config/showrunners.lock`: `scripts/production/showrunners.py` keeps its state under `scripts/lib/showrunner_state.state_root()`. Remove the `/config/showrunners.lock` line from `.gitignore`, and delete a stale `config/showrunners.lock` on each machine in the live check. If the area already keeps state in another `~/.local/state/` folder, use that folder and name it in the reply.
2. **Readers and writers.** Find each with `grep -rln 'accounts\.json\|accounts\.lock\|showrunners\.lock' scripts`. On the plan base, `accounts.json` appears in `scripts/accounts/worker_names.py`, `scripts/whoami/quota_alert_test_support.py` and `scripts/agents/codex_test_support.py`; `scripts/accounts/accounts.py` owns the lock. One function gives each path.
3. **Switch.** At no time may two live processes use different paths for the same lock or state file. A process started before the merge reads the old path without the new lock (`_local_role_map`), and it reads a missing file as an empty role map. So the file moves only after no such process runs:
   1. One function gives the live state path: the new path when it exists, else the old path. The lock follows the live file: `config/accounts.lock` while the old file is live, the new lock after. After the code takes a lock, it checks that its file is still the live one; if not, it releases the lock and resolves the path again.
   2. The move is one command in `scripts/accounts/accounts.py`. The live check runs it after both machines pull and every long-lived process that reads accounts restarts. Under the old lock it renames the old file to the new path, then deletes `config/accounts.lock`.
   3. A reader that caches the role map reads it again when the live path changes.

   Tests: the same worker account selection before and after the move; before the move a write takes the old lock and writes the old path; after the move a write takes the new lock and writes the new path; a writer that waits on the old lock during the move resolves the new path after it.
4. **Cleanup.** After the switch, no old file stays in `config/`. Remove `config/accounts.json` from `.gitignore` (line 23 on the plan base) when nothing writes it.
5. **Format rule.** The form test's format check also fails on a `.json` or `.lock` runtime file in `config/` on disk that is not tracked. Test: a fixture Git checkout with an untracked `config/x.json` and `config/x.lock`; the repository check finds both by itself, with no path given by the test.
6. **Live check.** After the merge and the Mac pull, the director checks on natedev and the Mac that the accounts table reads, the worker account selection is unchanged, and no `config/accounts.json`, `config/accounts.lock` or `config/showrunners.lock` remains.

**Files:**
- `scripts/accounts/accounts.py`, `scripts/accounts/worker_names.py` — the new paths; their tests also change
- `scripts/whoami/quota_alert_test_support.py`, `scripts/agents/codex_test_support.py`
- `.gitignore`
- `scripts/lib/test_config_form.py`, `scripts/lib/config_form_exceptions.py`

**Workers:** 2 writers — the account state switch and the format rule.
- `impl` — `accounts.json` and `accounts.lock`: `scripts/accounts/` files, the test support files, `.gitignore`
- `test` opens as impl — the untracked runtime file rule and its fixture checkout in `scripts/lib/test_config_form.py`; hub: `scripts/lib/config_form_exceptions.py` (`impl` messages this slot for a row)

**State:** moves `accounts.json` and `accounts.lock` on natedev and the Mac; deletes a stale `config/showrunners.lock`.

**Constraints from prior phases:**
- Phase 7 moved `config/accounts.toml` `[trust] folders` to `config/accounts.conf` `trust.folders`.
- Phase 1 format check reads tracked files only; Spec item 5 adds the untracked runtime file case. `check_format(root, files)` judges only the paths it gets, and `test_repository_form` passes `git ls-files -z config/`. The untracked case needs its own file list from the `config/` folder on disk, and its own planted row in `test_planted_violations`.

**Acceptance gate:**
- The account tests pass, including the switch tests of Spec item 3.
- `test_config_form.py` passes, and the fixture checkout with untracked `config/x.json` and `config/x.lock` fails the format check twice.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.
- The live check of Spec item 6 passes on both machines.

### Phase 15 — Context deferral rules to the common form  · status: todo
**Depends on:** Phase 4

#### Work Order

**Goal:** `config/context_deferral.conf` holds only the `window` key as a commented, limit-checked `key=value` row, the hook's phrase patterns live in a data file, and the hook reads `window` through `conf.read`.

**Spec:**

0. **Wait.** Start only after nate's go for this move (Move 4 of the question; showrunner, 11-23 19:16 PST), typed in this unit's session or sent by the showrunner as nate's words.
1. **window.** `config/context_deferral.conf` keeps one row, `window=200`, with its comment and the limit line `# whole: 1..`. The key name and the value stay.
2. **Patterns.** The `action`, `marker`, `state`, `later` and `exclude` rows move with their comments, in order and byte for byte, to the data file `config/context_deferral_patterns.txt` (rule 10). Each line is `<kind>: <regular expression>`; a kind can repeat.
3. **Reader.** `scripts/hooks/stop-context-deferral.py` (line 44 reads `conf.path("context_deferral")` by hand) reads `window` with `conf.read("context_deferral").whole("window")` at use. It parses the pattern file as data from the same folder (`conf.directory()`), so a test sets both with `CONF_DIR`. The hook runs on each stop: time it before and after, and name the added cost in the reply.
4. **Allow lists.** Remove the `FORMAT_FILES` row of `config/context_deferral.conf`. Replace the `stop-context-deferral.py` reader row with a `PERMANENT` row that reads `context_deferral_patterns.txt` as data. Add `config/context_deferral_patterns.txt` to `DATA_FILES`.
5. **README.** Update the `## context_deferral.conf` section of `config/README.md` (line 159), and name the data file.
6. **Tests.** `scripts/hooks/test_stop_context_deferral.py` writes `context_deferral.conf` with `write_config_fixture` and a pattern file in `CONF_DIR`. The same transcripts give the same decisions before and after.

**Files:**
- `config/context_deferral.conf`, `config/context_deferral_patterns.txt` — new data file
- `scripts/hooks/stop-context-deferral.py`, `scripts/hooks/test_stop_context_deferral.py`
- `scripts/lib/config_form_exceptions.py`, `config/README.md`

**Workers:** 1 writer + 1 tester — the hook and its data are one change.
- `impl` — `config/context_deferral.conf`, `config/context_deferral_patterns.txt`, `scripts/hooks/stop-context-deferral.py`, `config/README.md`; hub: `scripts/lib/config_form_exceptions.py`
- `test` — `scripts/hooks/test_stop_context_deferral.py`: the fixture form and the before-and-after decision rows

**State:** none

**Constraints from prior phases:**
- Phase 3 put `config/context_deferral.conf` on `FORMAT_FILES` and `scripts/hooks/stop-context-deferral.py` on the reader allow list (`reads=("context_deferral.conf",)`), both with `RemoveInPhase("Context deferral rules to the common form")`.
- Phase 4 made `conf.read` the reader pattern; tests use `CONF_DIR` with `write_config_fixture`.
- Phase 1 rules: a limit line directly above each number key; a blank line between blocks. `check_format` reports a `.conf` line that is not blank, a comment, a section line or a `key=value` row.

**Acceptance gate:**
- The hook tests pass, and the same transcripts give the same decisions.
- `test_config_form.py` passes with the `FORMAT_FILES` row removed.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 16 — User zone helper start time  · status: done
**Depends on:** Phase 4

#### Work Order

**Goal:** The configured `scripts/lib/user_zone.py` CLI starts as fast as it did before Phase 4, with the same output and behavior.

**Spec:**

1. **Measure first.** Time the configured CLI: no `USER_ZONE`, a `user_zone.conf` fixture in `CONF_DIR`, `python3 -S`. Take the median of 20 runs at the Phase 4 parent `d5c0a1b746` and at the phase base, with `build_slots.py run --commit <hash>` as in Phase 4. Phase 4 measured 34 ms before and 67 ms after. List the loaded modules and their cost with `python3 -X importtime`.
2. **Lazy imports.** In `scripts/lib/conf.py`, move each module-level import that a plain `conf.read(stem)` does not need into the function that uses it. Example: `subprocess` serves only `_git` (Git recovery). `decimal` and `scripts.lib.models_registry` already load inside `_agent_limit`; keep them there.
3. **No behavior change.** Values, diagnostics, host overrides and Git recovery stay the same. Showrunner, 11-24: lazy imports in `conf.py` only.
4. Keep annotation types visible to basedpyright with `if TYPE_CHECKING:` imports.
5. A module that module-level code needs (a dataclass, an enum, a lock, `socket` for the host name on each read) stays. Name each one with its cost in the reply.
6. `scripts/lib/test_user_zone.py` `test_cli_import_budget_without_site`: the configured subtest asserts that each module moved by item 2 stays unloaded.

**Files:**
- `scripts/lib/conf.py`
- `scripts/lib/test_user_zone.py`, `scripts/lib/test_conf.py`

**Workers:** 1 writer + 1 tester — one module and its import budget test.
- `impl` — `scripts/lib/conf.py`, `scripts/lib/test_conf.py`
- `test` — `scripts/lib/test_user_zone.py`: the configured import budget rows, and the timing before and after

**State:** none

**Constraints from prior phases:**
- Phase 4 moved `scripts/lib/user_zone.py` to `conf.read("user_zone").value("zone", <check>)`. `test_cli_import_budget_without_site` has a pinned subtest (`USER_ZONE=Europe/Paris`, `pathlib` unloaded) and a configured subtest (`CONF_DIR` fixture, `decimal` and `scripts.lib.models_registry` unloaded).
- `conf.py` imports at module level: `collections.abc`, `contextlib`, `contextvars`, `dataclasses`, `enum`, `math`, `os`, `pathlib`, `re`, `socket`, `subprocess`, `sys`, `threading.Lock`, `typing` and `scripts.lib.conf_reads`.
- `scripts/aws_target/test_hana_render.py` checks the imports of the Hana guest files (`UNSHIPPED_IMPORTS` has a `lib/conf.py` row). A change to a file in `scripts/aws_target/hana_image.files` updates `hana_image.sha256`.

**Acceptance gate:**
- The configured CLI median of 20 runs is within 5 ms of the 34 ms measured before Phase 4, or the reply names each remaining import, the module-level code that needs it, and its cost.
- `test_user_zone.py` and `test_conf.py` pass. The configured subtest asserts that the moved modules stay unloaded.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 17 — Build tool limits follow-ups  · status: todo
**Depends on:** Phase 10

#### Work Order

**Goal:** One build report uses one edited-crate limit in all its sections, and a Mac nextest run with an unusable Summary scan limit keeps its log, its status and a clear diagnostic.

**Spec:**

1. **One limit for each report.** `scripts/buildlog/report.py` `report()` reads `RebuildClasses` for the rebuilds section, and `moments_section()` reads a second one. A `/tune` write between the two reads can give the rebuild table and the moments groups different labels. Pass the one `RebuildClasses` value from `report()` through every section. The standalone moments command (`scripts/buildlog/cli.py`, line 222) reads its own value.
2. **Report test.** Add a row to the existing report test: one report pass reads `report.edited_crate_max_crates` once, and both sections use the same labels.
3. **Unusable scan limit.** Add a row to `scripts/mac_test/test_offload_cleanup.py` `test_nextest_replays_success_and_retains_bounded_failure_output`: an unusable `nextest.summary_scan_bytes` (above its `# whole: 1..1048576` limit) and no recovery source. Check the diagnostic, the retained log path and the original nextest status, and that no Summary line is sent. Keep the `run TAIL SUMMARY COMMAND...` interface. Change `scripts/mac_test/nextest_log.py` only if the row fails.

**Files:**
- `scripts/buildlog/report.py`, `scripts/buildlog/cli.py`
- `scripts/buildlog/test_report.py`, `scripts/buildlog/test_moments.py`
- `scripts/mac_test/test_offload_cleanup.py`, `scripts/mac_test/nextest_log.py` — the code only if the new row fails

**Workers:** 1 writer + 1 tester — split by folder.
- `impl` — `scripts/buildlog/report.py`, `scripts/buildlog/cli.py`, `scripts/buildlog/test_report.py`, `scripts/buildlog/test_moments.py`
- `test` — `scripts/mac_test/test_offload_cleanup.py`, and `scripts/mac_test/nextest_log.py` if the new row shows a defect

**State:** none

**Constraints from prior phases:**
- Phase 10: `RebuildClasses` (a frozen dataclass in `report.py`) holds the edited-crate maximum and the labels. `RebuildClasses.read()` reads `conf.read("buildlog").whole("report.edited_crate_max_crates")`. `label(crates_compiled, warm)` is the one bin rule. `rebuild_timings`, `package_rebuild_rows`, `rebuilds_section` and `profile_step_durations(connection, start, end, classes, *, hana_only=False)` take `classes`. `moments_section(connection, day)` reads `RebuildClasses.read()` once and passes it to `grouped()`.
- Phase 10: `test_moments.py` spies on `report.profile_step_durations`. Keep each spy target current.
- Phase 10: `config/mac_test.conf` has `# whole: 1..1048576` above `nextest.summary_scan_bytes=8192`. `replay_log` reads the key under `if status:` and calls `parse.bounded_displayed_lines(log, scan_bytes)`. `run_logged` catches `(OSError, conf.Unusable)`. The macOS target gets no per-run script copy, so the `run TAIL SUMMARY COMMAND...` form must not change.
- Tests write keys in a `CONF_DIR` fixture with `write_config_fixture`.

**Acceptance gate:**
- `test_report.py`, `test_moments.py` and `test_offload_cleanup.py` pass. The report test proves one read for each report pass.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.
