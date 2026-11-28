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

Key rule: steps 1, 2, 4, 7 and 8 change no key and can start. Steps 3, 5 and 6 rename or move keys. Each of them waits for nate's go typed in this unit's session. nate gave the go for all four moves on 11-24 at 07:50 PST (Questions page).

devtools of sdlc wrote P807. sdlc sends you the hand-over from devtools.
Use Codex workers, not subagents (nate, 11-23 14:04 PST). A claude_commands `--affected` run gives no `--targets`; the default is natedev and mac-linux. A `gate.py --module` run names `--targets natedev` only (builds, 00:07 PST Nov 24).
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
  - `scripts/lib/conf.py` — `conf.read(stem)` returns `ConfigRead` (`text`, `number`, `whole`, `words`, `values`, `origin`, `whole_upper_bound`); limit comments (`_limits` at line 484), host overrides, Git recovery; `CONF_DIR` (`directory`, line 419); `read_or_repository` (line 408); ignores `[section]` lines (line 444).
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
  - `scripts/lib/config_form_exceptions_test_support.py` — the typed allow lists of the form test.
- **Test lanes:** `scripts/lib/test_conf.py`, `scripts/lib/test_conf_sh.py`, `scripts/lib/test_models_registry.py`, `scripts/lib/test_tune.py`, and the `test_*.py` beside each changed module. The new form test is `scripts/lib/test_config_form.py`.
- **Build:** none.
- **Test:** a queued run: `~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py run --repo <worktree> --commit HEAD --apply-uncommitted <worktree> -- ./scripts/lib/py scripts/production/gate.py --affected`, with no `--targets` (the default is natedev and mac-linux). While iterating, add `--module <suite>/<test_module>.py` and name `--targets natedev`: the queue runs `--module` only on natedev. A direct run names single test functions only: `./scripts/lib/py -m unittest scripts.<suite>.<test_module>.<Class>.<test>`. The run target rule is from builds, 00:07 PST Nov 24.
- **Status line check:** a phase that changes fixture or config path selection names `scripts.production.test_unit_phases` `test_unit_count_cache_filters_sources_and_retries_failed_reads` among its direct checks. It proves that the status line question count watches the configured zone file.
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
- Each `config/*.conf` file has a blank line before each comment or `[section]` line that follows a key row.
- `scripts/lib/test_config_form.py` holds `Violation(path, line_number, rule, line)` and one check for each rule, each `(root, files) -> list[Violation]`: `check_format`, `check_key_form`, `check_sections`, `check_comment_limits`, `check_blank_lines`. `test_repository_form` runs them on `git ls-files config/` and fails on an unlisted violation or a stale allow list row. `test_planted_violations` gives exactly one violation for each planted break. The key form check accepts a part that is a machine alias or host from `config/machines.conf` or a target from `config/build_targets.conf`.
- `scripts/lib/config_form_exceptions_test_support.py` holds every allow list as typed data: `FileException(path, reason, until)` and `KeyException(path, key, reason, until)`, with `until: ExceptionLifetime | RemoveInPhase`. `ExceptionLifetime` is `PERMANENT` or `NO_REMOVAL_PHASE`; `RemoveInPhase(title)` names the plan phase that removes the row. Lists: `DATA_FILES`, `DATA_SUFFIXES`, `PENDING_TOML_FILES`, `KEY_FORM_FILES`, `KEY_FORM_KEYS`, `SECTION_FILES`, `COMMENT_LIMIT_FILES`, `BLANK_LINE_FILES`.
- The registry writers keep the blank lines: `registry_rows.space_groups(text)` spaces the whole text, and `registry_rows.replay`, `models_registry._publish` and `save_registry` apply it on each write.
- Every live write refuses a value that `str.splitlines()` splits, with `<stem>: <key>: value must fit on one line`: `live_settings.check_value_line` runs first in `editable_setting`, and `tune.py` runs it before the Mac forward.

**Files:**
- `config/README.md` — the common form rules
- `config/*.conf` — blank lines between blocks
- `scripts/lib/test_config_form.py` — the form checks and their tests
- `scripts/lib/config_form_exceptions_test_support.py` — the typed allow lists
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
- `scripts/lib/config_form_exceptions_test_support.py` holds the row types and their lists. Each row has `until: ExceptionLifetime | RemoveInPhase`; `test_repository_form` fails on a stale row.
  - `ReaderException(path, reads, reason, until)`: `reads` holds the base names of the config files the reader reads, or `*` for a path the code computes. A row covers only reads of those files.
  - `VariableException(name, reason, until)`.
  - `TunableLiteral(path, name, value, stem, key, limit, batch: RemoveInPhase)`, `ExternalLiteral(path, name, reason)` and `InternalLiteral(path, name, reason)` in `LITERAL_INVENTORY`: 35 tunable, 8 external and 24 internal rows. Each tunable row holds bob's working stem and key (11-23 19:17 PST); upper limits use the form `max_<thing>_<unit>`.
- Permanent reader rows also cover `scripts/accounts/new_account.py` (statusline.conf), `scripts/aws_target/hana_render.py` (four files for a guest host), `scripts/direct/pass_record.py` (hashes models.conf and direct.conf), `scripts/lint/lint_config.sh` (lint.conf), `scripts/lib/waiting_rows.py` (models.conf) and the data readers.
- `config/README.md` rule 4: a key part that is a name the code looks up is spelled as that name. Those `KEY_FORM_KEYS` rows are permanent; the uppercase-key rows stay `NO_REMOVAL_PHASE` (sdlc owns that rename).

**Files:**
- `scripts/lib/test_config_form.py` — the three code checks, the format row rule, their tree test and planted fixtures
- `scripts/lib/config_form_exceptions_test_support.py` — the row types, the reader, variable and literal allow lists
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
- `scripts/lib/config_form_exceptions_test_support.py` — eight reader rows and two variable rows removed; the `statusline.sh` row permanent
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

### Phase 5 — Per-file config environment variables to CONF_DIR  · status: done

#### As-built

- `COPIES_CONFIG`, `STAFF_CONFIG`, `AWS_TARGET_CONFIG`, `BUILD_TARGETS_CONFIG`, `SHOWRUNNERS_CONFIG` and `LINT_CONFIG_FILE` do not exist. Code reads those files through `conf.read(stem)` and `conf.path(stem)`, which resolve `CONF_DIR` at use. Tests set `CONF_DIR` with `write_config_fixture`.
- `registry_path(environment=None)` lives in `scripts/lib/registry_rows.py`; `models_registry` re-exports it. It returns `MODELS_CONFIG_FILE` first, then `CONF_DIR/models.conf`, then `$HOME/.claude/config/models.conf`. Every registry reader calls it, `scripts/whoami/codex_pacer.py` at use.
- `MODELS_CONFIG_FILE` stays as a `PERMANENT` variable row: it pins a launched worker to its exact registry. `scripts/agents/models_config.sh` passes a set pin (exported or not) as `--registry`. `scripts/direct/claude_worker.py` sends the `CONF_DIR` that `nix_env` loaded and omits the key when it is absent.
- `scripts/lib/python_startup/sitecustomize.py` `_begin_direct_test()` clears an inherited `MODELS_CONFIG_FILE` once per test process, before the gate limit check. A pin that a test sets later stays. `test_models_config.sh`, `test_sync_codex_catalog.sh` and `test_agent_exec.sh` unset it in their setup. `config_environment()` returns `MODELS_CONFIG_FILE=""`.
- `scripts/aws_target/config.py` `account_settings_path()` always returns `~/.claude/config/aws_target.conf`, whatever `CONF_DIR` is.

**Files:**
- `scripts/lib/registry_rows.py`, `scripts/lib/models_registry.py`, `scripts/lib/waiting_rows.py`, `scripts/whoami/codex_pacer.py` — the one registry resolver and its users
- `scripts/agents/models_config.sh`, `scripts/agents/sync_codex_catalog.sh`, `scripts/direct/claude_worker.py` — the worker pin
- `scripts/lib/python_startup/sitecustomize.py`, `scripts/lib/test_direct_test_limit.py` — the pin clear at test entry
- `scripts/aws_target/config.py` — the installed AWS account file
- `scripts/lib/build_folders.py`, `scripts/copies/copies.py`, `scripts/production/staff.py`, `scripts/production/showrunners.py`, `scripts/build_hold/override_guard.py`, `scripts/build_hold/override_guard.sh`, `scripts/buildlog/record.py`, `scripts/lint/lint_config.sh`, `scripts/direct/verify.sh` — `CONF_DIR` reads
- `scripts/lint/memory_test_support.py` — `config_environment()` clears the pin
- `scripts/lib/config_form_exceptions_test_support.py` — six variable rows removed; `MODELS_CONFIG_FILE` permanent
- `config/README.md`, `docs/as-built/agent-registry.md` — the resolver order and `build_targets.conf` path

**Binds later work:** the registry path comes only from `registry_rows.registry_path`, and the worker pin keeps its priority at runtime. A fixture that sets `HOME` must also set the host build root, the report window and the ledger path it reads, or it reads live values.

**Gotchas:**
- A test run from a worker inherits the pin; without the test-entry clear, a fixture `CONF_DIR` still reads the live registry.
- `scripts/review_links/test_list_page.py` studio tests fail on main too: the studio fixture appends a duplicate `studio_avatar_root` row, and the parser keeps the first.

**Ruled out:**
- A `MODELS_CONFIG_FILE=""` line in each test fixture: one rule copied into 214 files.
- `registry_path` in `conf`: it makes an import cycle.

### Phase 6 — config/cli.conf  · status: todo
**Depends on:** Phase 2, Phase 5

#### Work Order

**Goal:** Eight CLI tool rows move to `config/cli.conf`, which also holds the hand-made output-tail limit, with the `cli.<tool>.<family>.<field>` names, every reader and `/default_models` use the new file, and `config/models.conf` has no `[cli.*]` section.

**Spec:**

0. **Wait.** nate gave the go for all four moves on 11-24 at 07:50 PST (Questions page); the 35 working names stand. Start only after Phases 1 and 2 (the rules and checks) are on main.
1. **New file.** Create `config/cli.conf` with the rows below. Preserve the eight moved values byte for byte. Use the output-tail value from item 8. Put one blank line between blocks:

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
   `live_settings.stage_save` checks the cli value again under its lock. If that second check fails after the registry save, the command names the saved `models.conf` rows and the refused `cli.conf` row, and exits nonzero. A retry of the same command is idempotent: the registry rows already match, so it writes only the `cli.conf` row. Test this case.
5. **Docs.** `commands/default_models.md`: the examples and the paragraph at lines 106-111 name `config/cli.conf` keys, not `[cli.claude]`. `commands/build/hand_made.md`: name `config/cli.conf`. `config/README.md`: rule 6 adds "`# agent: claude` or `# agent: codex` checks an `agent[:effort]` value against that family in `config/models.conf`."; rule 11 adds "The `cli` rows live in `config/cli.conf`."; the `## models.conf` section drops the cli rows; add a `## cli.conf` section with the key form `<tool>.<family>.<field>` or `<tool>.<setting>`, and "A new CLI tool copies a block and changes `<tool>`; it adds a `.role` row only when its code reads one."
6. **Tests.** Fixtures write `cli.conf` in `CONF_DIR`: `scripts/whoami/quota_alert_test_support.py`, `scripts/agents/test_agent_exec.py`, `scripts/agents/test_agent_exec.sh`, `scripts/production/test_hand_made.py` and the other `scripts/production/test_hand_made_*.py` that build a registry, `scripts/lib/test_models_registry.py`, `scripts/lib/test_models_registry_fixture.py`. Add cases through `default_models.sh`: `/default_models cli.merge_branch sonnet` writes `merge_branch.claude.model` in `cli.conf`; the status listing shows the `cli` rows; `/default_models cli claude` changes only `[assignments] cli`; a model replace across functions changes the matching `cli.conf` rows; a value with a line separator is refused and leaves both files unchanged; `hand_made.claude.model=opus` (no effort) is refused by the hand made reader with the key name.
7. **Proof.** Record `bash scripts/cli_agent/cli_agent.sh --status` before the change. After the change it prints the same agents.
8. **Output tail.** `scripts/production/hand_made_limit.py` `TAIL_BYTES` (line 44, `64 * 1024`) becomes the `hand_made.output_tail_bytes=65536` row above (bob's working name `cli.hand_made.output_tail_bytes`), read at use. Its `TunableLiteral` row (batch "config/cli.conf") leaves the inventory. No test reads the live value; each test writes its own fixture config. `scripts/production/hand_made_guest.py` makes an `OutputTail` on the controller for the guest's streamed output, so a read in `OutputTail.write()` would repeat for each chunk. Read the key once on the controller before the attempt starts, and pass the bounded value to each `OutputTail`. Change the natedev and guest routes, and test both with the same fixture.

**Settled (natedev 02:16 PST 11-27):** `/default_models` set-service-tier keeps working for the `cli` function. `_agents_set_service_tier` in `scripts/agents/models_config.sh` (line 1126) writes the `cli.conf` row `<task>.codex.service_tier` through the `/tune` path, for every `cli` task when no subtask is named. `inherit` removes the row. Keep the function, subtask and all-functions forms. The new key merges under this working name; natedev names it to nate after the merge. The removal of `[cli.options] codex_service_tier` rides the P807 go. Test each form and `inherit` through `default_models.sh`.

**Service tier rows (architect after Phase 14):** `live_settings.stage_save` changes the value of a row that exists. A first `service_tier` setting must create its row, and `inherit` must remove it. Add a create and a remove operation to the `/tune` path in `scripts/lib/live_settings.py` and `scripts/lib/waiting_rows.py`, with named states for set and inherit (no bare `None`). Test publication and replay for a create, a remove and the all-functions form through `/default_models`.

**Files:**
- `config/cli.conf` — new
- `config/models.conf` — `[cli.*]` sections removed
- `scripts/lib/models_registry.py`, `scripts/agents/models_config.sh`, `scripts/agents/default_models.sh`, `scripts/cli_agent/cli_agent.sh`
- `scripts/production/hand_made.py`, `scripts/production/hand_made_limit.py`, `scripts/production/hand_made_guest.py`
- `scripts/lib/config_form_exceptions_test_support.py` — the `TAIL_BYTES` row removed
- `commands/default_models.md`, `commands/build/hand_made.md`, `config/README.md`
- `scripts/agents/test_models_config.sh`, `scripts/agents/test_models_conf_publish.py` — the writer tests: the CLI rows in `/default_models` and the mixed model replacement
- `scripts/lib/live_settings.py`, `scripts/lib/waiting_rows.py`, `scripts/lib/test_waiting_rows.py` — row create and remove for the service tier
- `scripts/whoami/quota_alert_test_support.py`, `scripts/agents/test_agent_exec.py`, `scripts/agents/test_agent_exec.sh`, `scripts/production/test_hand_made.py`, `scripts/production/test_hand_made_guest.py`, `scripts/production/test_hand_made_limit.py`, `scripts/production/test_hand_made_mac.py`, `scripts/lib/test_models_registry.py`, `scripts/lib/test_models_registry_fixture.py`

**Workers:** 2 writers — the launcher side and the hand made side.
- `impl` — `config/cli.conf`, `config/models.conf`, `scripts/lib/models_registry.py`, `scripts/agents/models_config.sh`, `scripts/agents/default_models.sh`, `scripts/cli_agent/cli_agent.sh`, `commands/default_models.md`, `config/README.md`, `scripts/lib/test_models_registry.py`, `scripts/lib/test_models_registry_fixture.py`, `scripts/agents/test_agent_exec.py`, `scripts/agents/test_agent_exec.sh`, `scripts/whoami/quota_alert_test_support.py`, `scripts/agents/test_models_config.sh`, `scripts/agents/test_models_conf_publish.py`, `scripts/lib/live_settings.py`, `scripts/lib/waiting_rows.py`, `scripts/lib/test_waiting_rows.py`; hub: `config/cli.conf` (both sides read it)
- `test` opens as impl — `scripts/production/hand_made.py`, `scripts/production/hand_made_limit.py`, `scripts/production/hand_made_guest.py`, `scripts/lib/config_form_exceptions_test_support.py`, `commands/build/hand_made.md`, `scripts/production/test_hand_made*.py`

**State:** none

**Constraints from prior phases:**
- Phase 2 added the `# agent: claude|codex` limit kind to `conf.py` and `/tune` (`conf.check`). It calls `models_registry.check_agent_value(registry, context, family, value)`, which runs the `/default_models` validator `_agents_validate_pair` from `scripts/agents/models_config.sh`. `conf.sh` hands an `# agent:` key to Python, so both readers give one result. A refusal is the validator text `ERROR: [<stem>.<key>] ...`.
- A codex alias in `[codex.families]` (`luna`, `sol`) also reads the codex provider cache (`CODEX_MODELS_CACHE_FILE`, default `~/.codex/models_cache.json`), which can point it at another model. A test fixture with an `# agent: codex` alias row sets `CODEX_MODELS_CACHE_FILE` to a fixture cache (see `write_agent_limit_codex_cache` in `scripts/lib/test_conf.py`).
- Git recovery never falls back to a committed agent value that fails the current check.
- Phase 5 put `registry_path` in `scripts/lib/registry_rows.py`. `models_registry` re-exports it. It checks `MODELS_CONFIG_FILE`, then `CONF_DIR/models.conf`, then `$HOME/.claude/config/models.conf`. Preserve the launch snapshot and worker pin. The unittest and shell test entry points clear inherited pins before tests select their fixtures.
- `conf._agent_limit` reads `CONF_DIR/models.conf`, but `registry_path` honors the worker pin. A CLI model check must read the same catalog as the launch: resolve it through `registry_rows.registry_path`. Test conflicting catalogs (pin and `CONF_DIR`) through both the Python and the shell CLI paths. If `launch_settings` gains a configuration parameter, use the existing `FreshLaunchConfiguration` state, not `None`.
- `models_registry.check_agent_value(registry, ...)` (line 224) reads the catalog file again, while `launch_settings()` can take a captured registry. A CLI model check validates against the same snapshot the launch uses. Test it: replace the catalog file after the capture; the check and the launch still agree (architect after Phase 14).
- Phase 1 rules: a blank line between blocks; the limit line directly above the key.
- Phase 1: the registry writers apply `registry_rows.space_groups(text)` to the whole text on each write (`registry_rows.replay`, `models_registry._publish`, `save_registry`). A blank line stays between a key row and the next comment or section after the `[cli.*]` removal.
- Phase 1: `write_live` and `stage_save` call `live_settings.editable_setting`, which first refuses a value that `str.splitlines()` splits (`live_settings.check_value_line`). The `/default_models` cli write gets the same refusal.
- `config/models.conf` is on the `COMMENT_LIMIT_FILES` allow list in `scripts/lib/config_form_exceptions_test_support.py`. If the `[cli.*]` removal takes away its last comment or limit violation, remove that row; the tree test fails on a stale row.

**Acceptance gate:**
- `cli_agent.sh --status` prints the same agents before and after.
- `/tune cli merge_branch.claude.model <value>` refuses an unknown agent (the `agent` limit).
- The models registry, hand made, agent exec and quota alert tests pass.
- `test_config_form.py` passes.
- `scripts/production/fixture_conf_keys.py --repo <worktree> --base <phase start> --tip <candidate commit>` exits 0. It does not check dotted keys, so the director also reads each `CONF_DIR` fixture of each changed reader, shell and subprocess fixtures included.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file; `bash -n` passes on each changed shell file.

### Phase 7 — TOML tunables to .conf  · status: todo
**Depends on:** Phase 5

#### Work Order

**Goal:** The 35 tunable keys of the four TOML files are commented, limit-checked rows in `.conf` files, read through `conf.read`, and the four TOML files are gone.

**Spec:**

0. **Wait.** nate gave the go for all four moves on 11-24 at 07:50 PST (Questions page); the 35 working names stand. Start only after Phases 1 and 2 (the rules and checks) are on main. Item 229 (it changes `scripts/maintenance/herdr_upkeep.py`) was on main by 11-27. Merge main first.
1. **Moves**, values byte for byte. A TOML table becomes a dotted prefix, so the full name `<stem>.<table>.<key>` stays the same:
   - `config/herdr_upkeep.toml` → `config/herdr_upkeep.conf`: `job.interval_seconds=120`, `job.timeout_seconds=60`.
   - `config/session_retries.toml` → `config/session_retries.conf`: `registry_reads=4`, `registry_delay_seconds=0.1`.
   - `config/accounts.toml` `[trust] folders = ["~/worktrees"]` → `config/accounts.conf` row `trust.folders=~/worktrees`: folder paths separated by `:`, as in `PATH`; an empty value means no folders (showrunner, 11-24 08:03 PST). `config/accounts.conf` has no `trust.folders` key today.
   - `config/agent_notes.toml` → `config/agent_notes.conf`: each `[table] key` becomes `table.key`. 30 keys: `job.interval_seconds`, `job.timeout_seconds`, `mac_usage.interval_seconds`, `mac_usage.codex_interval_seconds`, `parts.<part>.interval_seconds` and `parts.<part>.timeout_seconds` for `notes`, `quota`, `pacer`, `refusals`, `watchdog`, `logins`, `prune`, `folder_size`, `vault`, `vault_save`, then `vault.git_seconds`, `vault.named_paths`, `watchdog.dispatch_seconds`, `watchdog.recovery_seconds`, `watchdog.pass_dispatch_seconds`, `watchdog.result_seconds`.
2. **Comments and limits.** Carry each TOML comment over. Replace prose limits ("Use a positive finite number") with a `# whole:` or `# range:` line that states the limit the reader checks today. One blank line between blocks.
3. **Readers**, each read at use, no module-level copy:
   - `scripts/maintenance/herdr_upkeep.py` (line 28) → `conf.read("herdr_upkeep")`; remove `HERDR_UPKEEP_CONFIG`.
   - `scripts/maintenance/settings.py` (line 88) → `conf.read("agent_notes")`; remove `AGENT_NOTES_JOB_CONFIG`. The notes timer reads values on each pass; keep that. Keep the separate failure paths: `part_cadence()` reads one part, `settings()` reports an invalid part through `InvalidPartSchedule` while other parts run, and `usage_only_intervals()` stays independent. Test that an unusable part leaves unrelated work eligible. The agent-notes lane owns the shared `job_config_fixture` in `scripts/maintenance/test_vault_save.py` and its users in `test_vault_commit.py` and `test_agent_notes_job.py`.
   - `scripts/message/session_records.py` (line 100) → `conf.read("session_retries")`.
   - `scripts/accounts/accounts.py` (line 875) → `conf.read("accounts").value("trust.folders", str, allow_blank=True)`. Map an empty value to no folders. Split a nonempty value on `:`. Keep today's handling of a bad part: `_trusted_folders` (line 887) prints `accounts: anomaly: trust folder is not an absolute path` and skips a part that is empty or not absolute after `~` expansion; it refuses nothing. Add no new refusal (nate's rule of 11-05). Keep the tests of `~/other trees` (`test_accounts.py` line 3087) and the empty list (line 3244). Keep the test that `trust` and `setup` read one configuration before they take the home locks. `conf.py` (line 477) strips text after ` #`, so a folder path that holds ` #` cannot be stored in the row. The live value `~/worktrees` holds none; say so in the reply, and test that the moved row reads the same folders as the TOML file.
   - `config/gate.conf` `scan_roots` (line 35) maps `maintenance/test_vault_commit.py` to `config/agent_notes.toml`. With that file gone, `declared_scan_roots()` refuses the gate configuration. Change the path to `config/agent_notes.conf`, and prove that a change to that file selects `test_vault_commit.py` (architect after Phase 14).
4. **Tests** write `CONF_DIR` fixtures with `write_config_fixture`.
5. Delete the four TOML files. Remove their rows from the format, reader and variable allow lists in `scripts/lib/config_form_exceptions_test_support.py`.
6. **README.** Replace the TOML descriptions of these files with `.conf` sections.

**Files:**
- `config/herdr_upkeep.toml`, `config/session_retries.toml`, `config/accounts.toml`, `config/agent_notes.toml` — deleted
- `config/herdr_upkeep.conf`, `config/session_retries.conf`, `config/agent_notes.conf` — new
- `config/accounts.conf` — `trust.folders` row
- `scripts/maintenance/herdr_upkeep.py`, `scripts/maintenance/test_herdr_upkeep.py`
- `scripts/maintenance/settings.py`, `scripts/maintenance/test_agent_notes_job.py`, `scripts/maintenance/test_*.py` — the agent notes reader, and the tests that set `AGENT_NOTES_JOB_CONFIG`
- `scripts/message/session_records.py`, `scripts/message/test_session_records.py`
- `scripts/whoami/test_agent_notes.py`, `scripts/accounts/test_refusal_watch.py` — they set `AGENT_NOTES_JOB_CONFIG`
- `scripts/accounts/accounts.py`, `scripts/accounts/test_accounts.py`, `scripts/accounts/home_test_support.py`, `scripts/production/unit_status_test_support.py`
- `config/gate.conf` — the `test_vault_commit.py` scan path
- `scripts/lib/config_form_exceptions_test_support.py`, `config/README.md`

**Workers:** 2 writers — split by file pair.
- `impl` — the `agent_notes` and `herdr_upkeep` files and their readers and tests, `config/gate.conf`, with the agent notes fixtures in `scripts/whoami/test_agent_notes.py` and `scripts/accounts/test_refusal_watch.py`; hub: `scripts/lib/config_form_exceptions_test_support.py` (this slot owns every edit to it; the other slot sends its row removals here)
- `test` opens as impl — the `session_retries` and `accounts` files, readers and tests. Own `scripts/accounts/test_accounts.py`, `scripts/production/unit_status_test_support.py`, `scripts/accounts/home_test_support.py`, the trust fixtures, and `config/README.md`.

**State:** none

**Constraints from prior phases:**
- Phase 3 put the four TOML files on the format allow list, their four readers on the reader allow list, and `HERDR_UPKEEP_CONFIG` and `AGENT_NOTES_JOB_CONFIG` on the variable allow list, under "TOML tunables to .conf".
- Phase 14 moved `accounts.json` out of `config/` and changed `scripts/accounts/worker_names.py`, `role_map.py` and `accounts.py` (`resolve_history_label`, `migrate-accounts-state`). The `trust.folders` edit at line 875 does not touch those parts. `test_config_form.py` now fails on an untracked `.json` or `.lock` file in `config/`.
- Phase 2 and Phase 1 rules apply: a limit line directly above each number key; a blank line between blocks.

**Acceptance gate:**
- Each changed module's tests pass on `CONF_DIR` fixtures.
- The `/tune` key-and-limit check accepts `agent_notes job.interval_seconds 120` with no write.
- `test_config_form.py` passes with the rows removed.
- `scripts/production/fixture_conf_keys.py --repo <worktree> --base <phase start> --tip <candidate commit>` exits 0. It does not check dotted keys, so the director also reads each `CONF_DIR` fixture of each changed reader, shell and subprocess fixtures included.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 8 — Remove section lines  · status: todo
**Depends on:** Phase 5

#### Work Order

**Goal:** No `.conf` file except `config/models.conf` has a `[section]` line, every key keeps its name and value, and every reader gets the same values as before.

**Spec:**

0. **Wait.** nate gave the go for all four moves on 11-24 at 07:50 PST (Questions page); the 35 working names stand. Start only after Phases 1 and 2 (the rules and checks) are on main.
1. **Files:** `config/build_admission.conf` (`[disk]`, `[memory]`), `config/build_folders.conf` (8 sections), `config/build_steps.conf` (4), `config/gate_rename_deploy.conf` (1), `config/copies.conf` (`[copies]`, `[allow-list]`), `config/design_roles.conf` (`[ownership]`, `[roles]`), `config/lint.conf` (4). Replace each `[section]` line with a plain group comment line, `# <section>`, with a blank line above it.
2. **Key names stay.** On the plan base no key repeats across sections in any of these files, so every key keeps its name. If a reader needs the section as part of the key, that is a rename: stop that file and report it to the director. No rename merges without nate's go.
3. **Readers first.** Confirm how each reader reads its file. Candidates from a plan-base scan (confirm each): `scripts/build_queue/admission_service.py`, `scripts/build_queue/memory_feed.py`, `scripts/build_queue/limits_view.py`, `scripts/lint/memory_admit.py`, `scripts/worktree/worktrees.py`, `scripts/astra/design_roles.py`, `scripts/herdr/worker_panes.py`, `scripts/buildlog/rust_release.py`, `scripts/build_targets/build_targets.py`, `scripts/buildlog/report.py`, `scripts/mac_test/eligibility.py`, `scripts/history/rounds.py`, `scripts/style/style_plan.py`. Keep readers that already use `conf` in the before-and-after proof. Convert each remaining section-aware reader. Cover `authority_request`, `_allow_new_roles`, and the `authority` CLI branch in `scripts/astra/design_roles.py`.
4. **design_roles.** `scripts/astra/design_roles.py` uses `configparser`. It changes to `conf.read("design_roles")` keys `host` and `new_open`. `host` keeps both uses: routing and the local authority check. Read `host` with `HostOverride.IGNORED` at both uses, so a `host.<machine>` row cannot change the authority. Test that both uses give the same authority when the fixture has a host override row. `new_open` gets the limit line `# one of: true, false`; the code reads `true` as open. Today `getboolean(..., fallback=True)` (line 825) also accepts `yes`, `on` and `1` and opens when the key is missing; after the change a missing or other value is a `conf.read` refusal, with Git recovery, like every key (rule 7: no default in code). The live value is `true`, so behavior stays the same. Test a legacy spelling (`new_open=yes`) and a missing key: each takes the `conf.read` refusal path with Git recovery, not the old open fallback. Remove `DESIGN_ROLES_CONFIG`; tests use `CONF_DIR`, and `scripts/astra/design_role_test_support.py` writes the fixture.
5. **Proof.** Before the change, a script reads every key of each file with `conf.read` and with each section-aware reader, and saves the values. After the change the same script gives the same values.
6. Remove the seven files from the sections allow list. Replace the `design_roles.py` reader row with a `PERMANENT` row for `designer.md` only: line 710 still reads `config/design_roles/designer.md` as data. Remove `DESIGN_ROLES_CONFIG` from `VARIABLE_EXCEPTIONS`.
7. **README.** Update the sections of `config/README.md` that show these files with `[section]` lines (for example `## lint.conf`).
8. **Diagnostics.** `scripts/build_queue/queue_place.py` (line 642) names `build_admission.conf [memory]` in its refusal. Name the key without the section.

**Files:**
- `config/build_admission.conf`, `config/build_folders.conf`, `config/build_steps.conf`, `config/lint.conf` — group comments; their section-aware readers from Spec item 3 and their tests also change
- `config/gate_rename_deploy.conf`, `config/copies.conf`, `config/design_roles.conf`
- `scripts/astra/design_roles.py`, `scripts/astra/test_design_roles.py`, `scripts/astra/test_design_roles_sources.py`, `scripts/astra/design_role_test_support.py`, `scripts/herdr/worker_panes.py`
- `scripts/build_queue/queue_place.py` — the `[memory]` diagnostic
- `scripts/lib/config_form_exceptions_test_support.py`, `config/README.md`

**Workers:** 2 writers — split by file group.
- `impl` — `config/build_admission.conf`, `config/build_folders.conf`, `config/build_steps.conf`, `config/lint.conf` and their readers and tests, with the `scripts/build_queue/queue_place.py` diagnostic
- `test` opens as impl — `config/gate_rename_deploy.conf`, `config/copies.conf`, `config/design_roles.conf`, `scripts/astra/design_roles.py`, `scripts/astra/design_role_test_support.py`, `scripts/herdr/worker_panes.py` and their tests, `config/README.md`; hub: `scripts/lib/config_form_exceptions_test_support.py` (this slot owns every edit to it; the other slot sends its row removals here)

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
- This phase adds no `.conf` keys. The named fixture tests prove each path, format or runtime state change. The director reads each `CONF_DIR` fixture of each changed reader, shell and subprocess fixtures included.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 9 — Comments and limit lines for every setting  · status: todo
**Depends on:** Phase 6, Phase 8

#### Work Order

**Goal:** Every key row of the files on the comment-limit allow list sits in a block that starts with a comment, every number key has a limit line directly above it, and `COMMENT_LIMIT_FILES` is gone.

**Spec:**

1. **Files.** The `FileException` rows of `COMMENT_LIMIT_FILES` in `scripts/lib/config_form_exceptions_test_support.py` at the phase start. After Phase 1 there were 16 files and 207 violations: `config/build_folders.conf` 49, `config/build_steps.conf` 49, `config/console.conf` 35, `config/models.conf` 30, `config/copies.conf` 10, `config/send_repeat_guard.conf` 9, `config/statusline.conf` 7, `config/build_targets.conf` 4, `config/stall_watch.conf` 4, `config/aws_target.conf` 2, `config/design_roles.conf` 2, `config/mac_linux.conf` 2, `config/lint.conf` 1, `config/memory_feed.conf` 1, `config/notify.conf` 1, `config/production.conf` 1. Phase 4 removed the `config/notify.conf` and `config/send_repeat_guard.conf` rows, which leaves 14 files. The main merge of item 149 (limit lines for numeric rows) removed the `build_targets`, `lint`, `mac_linux`, `stall_watch` and `statusline` rows, which leaves 9. Phases 6 and 8 remove more. List the lines with `check_comment_limits` from `scripts/lib/test_config_form.py`.
2. **Comments.** Put a comment line above each key block that has none. It says what the keys control; read the reader code to say it. Keep one blank line between blocks (rule 12).
3. **Limits.** Put a `# whole: A..B` or `# range: A..B` line directly above each key whose value is a number. Put `# one of: a, b` above a key whose reader accepts a closed set of words. The limit states what the reader accepts today; read its checks. When the reader checks nothing, use the limit the code needs to work, for example `# whole: 1..` for a count, and name each such choice in the reply. Every live value passes its limit.
4. **No key or value changes.** `git diff -U0 -- config/` adds only comment lines and blank lines. A limit line on an existing wait-time key states today's check; it adds no key and no name (Delegation Context wait-limit invariant).
5. **models.conf.** The comments go above the registry blocks. The registry writers (`scripts/lib/registry_rows.py`, `scripts/lib/models_registry.py`) and `scripts/agents/sync_codex_catalog.sh` must keep them. Run a registry write and the catalog sync on a fixture with the new comments, then run `check_comment_limits` on the output. The catalog sync fixture adds a model, so the sync publishes a change. If a writer drops a comment, fix that writer and add that run as a test row.
6. Remove each `COMMENT_LIMIT_FILES` row as its file passes. At the end remove `COMMENT_LIMIT_FILES`; the comment and limit rule of `test_repository_form` takes no exceptions.

**Files:**
- `config/*.conf` — comment and limit lines only, in the files of Spec item 1
- `scripts/lib/config_form_exceptions_test_support.py` — `COMMENT_LIMIT_FILES` removed
- `scripts/lib/test_config_form.py` — the comment and limit rule with no exceptions
- `scripts/lib/registry_rows.py`, `scripts/lib/models_registry.py`, `scripts/agents/sync_codex_catalog.sh`, `scripts/lib/test_models_registry.py`, `scripts/agents/test_sync_codex_catalog.sh` — only if a writer drops a comment

**Workers:** 2 writers — split by file. At phase start, the director splits the files that are still in the live `COMMENT_LIMIT_FILES` list (after Phases 6 and 8 some listed files can pass already); the lists below are the plan-base split.
- `impl` — `config/models.conf`, `config/build_folders.conf`, `config/copies.conf`, `config/design_roles.conf`, and any registry writer fix with its test row; hub: `scripts/lib/config_form_exceptions_test_support.py` and `scripts/lib/test_config_form.py` (`test` messages this slot for each row to remove)
- `test` opens as impl — the remaining rows for `config/build_steps.conf`, `config/console.conf`, `config/aws_target.conf`, `config/memory_feed.conf` and `config/production.conf`.

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
- A script checks each stored value of each changed stem against its limit line, host overrides included, with Git recovery off. (`check_comment_limits` checks only that a limit line exists, and a `conf.read` with recovery can return an older value.) `config/models.conf` is checked by section through the model registry reader. The proof also covers the four Phase 10 keys: `head_stall.times_tail_bytes`, `disk.min_outside_directory_bytes`, `report.max_edited_crates`, `nextest.summary_scan_bytes`; and the five Phase 11 keys: `context_usage` `transcript.tail_bytes`, `notify` `pushover.max_source_chars`, `waste_watch` `alert.max_wait_share`, `codex_pacer` `sampling.max_step_percent`, `mac_linux` `ssh.connect_attempts`.
- This phase adds no `.conf` keys. The named fixture tests prove each path, format or runtime state change. The director reads each `CONF_DIR` fixture of each changed reader, shell and subprocess fixtures included.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file; `bash -n` passes on each changed shell file.

### Phase 10 — Build tool literals to config  · status: done

#### As-built

- Four keys replace the batch constants, with the same values. `build_admission` `head_stall.times_tail_bytes=1048576` (`whole: 1..`). `buildlog` `disk.min_outside_directory_bytes=1073741824` (`whole: 1..`). `buildlog` `report.max_edited_crates=3` (`whole: 1..48`, because the cascade row ends at 49 crates). `mac_test` `nextest.summary_scan_bytes=8192` (`whole: 1..1048576`).
- `report.RebuildClasses` holds the one rebuild bin rule: `read()`, `labels()` and `label(crates_compiled, warm)`. `rebuild_timings`, `package_rebuild_rows`, `rebuilds_section` and `profile_step_durations` take `classes`.
- `head_stall.last_admission(times, settings: conf.ConfigRead)` takes the `build_admission` read that its caller already made.
- `disk.outside_directories` reads its limit at use.
- `nextest_log.replay_log` reads the Summary scan limit only after a failure (`if status:`). `run_logged` catches `(OSError, conf.Unusable)`, prints a diagnostic and keeps the nextest status and the log.
- The four `tunable` rows left `scripts/lib/config_form_exceptions_test_support.py`.

**Files:**
- `config/build_admission.conf`, `config/buildlog.conf`, `config/mac_test.conf` — the four keys
- `scripts/build_queue/head_stall.py`, `scripts/buildlog/disk.py`, `scripts/buildlog/report.py`, `scripts/mac_test/nextest_log.py` — readers at use
- `scripts/build_queue/test_head_stall.py`, `scripts/buildlog/test_disk.py`, `scripts/buildlog/test_report.py`, `scripts/buildlog/test_moments.py`, `scripts/mac_test/test_offload_cleanup.py` — `CONF_DIR` fixtures
- `scripts/lib/config_form_exceptions_test_support.py` — four rows removed
- `docs/as-built/build-followups.md`, `docs/as-built/build-targets.md`, `docs/plans/builds-resources.md` — key names replace the constant names

**Binds later work:** the four key names and limits. The follow-up "Build tool limits follow-ups" passes one `RebuildClasses` through each report and adds a Mac replay row with an unusable scan limit.

**Gotchas:**
- A test spy on a changed function survives the change: `test_moments.py` kept a spy on the old `profile_step_durations` call form. Search the tests for each changed function.
- Current docs named the deleted constants. Search `docs/` for each moved name.
- An open upper limit lets `readline` take `2**63` bytes; each byte limit that reaches a read needs an upper bound.

**Ruled out:** a command-line argument for the Summary scan limit. It changes the `run TAIL SUMMARY COMMAND...` form, and macOS gets no copy of the script for each run.

### Phase 11 — Message and notice literals to config  · status: done

#### As-built

- Five keys replace the batch constants, with the same values. `context_usage` `transcript.tail_bytes=524288` (`whole: 1..4194304`). `notify` `pushover.max_source_chars=80` (`whole: 1..256`). `waste_watch` `alert.max_wait_share=0.10` (`range: 0..1`, new file). `codex_pacer` `sampling.max_step_percent=3.0` (`range: 0..100`, new file). `mac_linux` `ssh.connect_attempts=3` (`whole: 1..`, bob's working name).
- `context_usage.latest_reading(path, configuration)` scans `transcript.tail_bytes`, then the 4 MiB `TAIL_BYTES_RETRY`. An unusable size skips only the first scan. Each hook run makes one `conf.read("context_usage")`.
- `pushover.parent_source` and `sender_name` read `pushover.max_source_chars` at use. `post_message` refuses an unusable sender limit: it logs `REFUSED_BEFORE_SENDING: <diagnostic>`, prints one stderr line and returns 2. It builds the footer once and cuts the body to `MESSAGE_MAX - len(footer)`. `cancel` calls `parent_source` only when no `--source` is given.
- `stop_resume._alert_pending_moves` catches `conf.Unusable` from `phone_message_limit` and returns the move results.
- `waste_watch.watch()` reads `alert.max_wait_share` once per pass and passes it to `UnitUsage.wait_alert(max_wait_share)`. `main` reports `conf.Unusable` with exit 1. `codex_pacer` ingest reads `sampling.max_step_percent` only when readings exist; backfill catches `conf.Unusable`.
- `mac_linux.read_settings` reads `ssh.connect_attempts` into `GuestSettings.connect_attempt_limit`. `retry_guest_connect(..., attempt_limit=)` and `GuestEndWait` use it. `SSH_CONNECT_TIMEOUT_SECONDS` and `CONNECT_RETRY_SECONDS` are wait limits and stay in code.
- The batch rows and `_MESSAGE_BATCH` left `scripts/lib/config_form_exceptions_test_support.py`. `config/build_targets.conf` has a blank line before `excluded_notice_seconds.mac`.

**Files:**
- `config/context_usage.conf`, `config/notify.conf`, `config/waste_watch.conf`, `config/codex_pacer.conf`, `config/mac_linux.conf` — the five keys
- `scripts/hooks/context_usage.py`, `scripts/hooks/post-tool-use-context-usage.py`, `scripts/production/stall_watch.py` — the scan size
- `scripts/notify/pushover.py`, `scripts/agents/stop_resume.py` — the sender limit and its refusal
- `scripts/usage/waste_watch.py`, `scripts/whoami/codex_pacer.py` — the wait share and the pacer step
- `scripts/build_targets/mac_linux.py` — the attempt limit
- `scripts/lib/config_form_exceptions_test_support.py` — batch rows removed
- `config/README.md`, `docs/as-built/message-clarity.md`, `docs/as-built/usage-ledger.md`, `docs/as-built/mac-test-offload.md` — the keys and their rules
- `scripts/hooks/test_showrunner_compaction.py`, `scripts/notify/test_pushover.py`, `scripts/notify/test_silence.py`, `scripts/agents/test_stop_resume.py`, `scripts/usage/test_waste_watch.py`, `scripts/whoami/test_codex_pacer.py`, `scripts/build_targets/test_mac_linux.py` — unusable-limit and `CONF_DIR` fixture tests
- `scripts/hooks/test_push_gate.py`, `scripts/whoami/test_mac_token_delivery.py`, `scripts/hooks/test_stop_direct_continue.py`, `scripts/production/test_hand_made_guest.py`, `scripts/build_queue/test_placement.py`, `scripts/production/test_stall_watch.py` — literal fixtures carry the new keys

**Binds later work:** the five key names and limits. The follow-up "Message limit follow-ups" makes `parent_source` never raise and keeps the context alerts when `alert.max_wait_share` is unusable.

**Gotchas:**
- A key added to an existing stem must be in every literal test fixture of that stem. `scripts/production/fixture_conf_keys.py` names each fixture that lacks it.
- A required key in a hook, alert or `finally` path changes failure behavior. Handle `conf.Unusable` at use, or the raise hides other results.
- `runner_commit` (`scripts/build_targets/mac_linux.py`) refuses an uncommitted change under `scripts/`, so the hand-made guest tests run only on a committed tree.

**Ruled out:**
- A host override limited by the base key's limit line: an add-on held for nate, because it adds a refusal.
- A test of the offload doc wording: it would compare constants.

### Phase 12 — AWS and review tool literals to config  · status: done

#### As-built

- Seventeen keys replace the batch constants, with the same values and bob's working names. `cloud_gate` `gpu_size_trial.nextest_log_tail_bytes=1048576`. `review_links` `new_item.max_words_chars=40000`, `max_images=4`, `max_image_bytes=5242880`, `max_total_image_bytes=12582912`, `max_page_data_bytes=65536`, `max_page_rows=48`, `max_page_sections=16`, `max_page_section_rows=128`, `max_bug_page_state_bytes=524288`, `max_page_url_chars=8192`, and `page_capture.max_active=8`; each has `# whole: 1..<shipped value>`. `screen_record` `recording.max_bytes=500000000` and `retention.max_directory_bytes=2147483648`. `shot_report` `transcripts.parallel_scan_bytes=1000000000`. `style_ledger` `write.lock_attempts=3`. `test_times` `module_cost.max_output_replay_bytes=1048576`. The batch rows left `scripts/lib/config_form_exceptions_test_support.py`.
- `conf.ConfigRead.whole_upper_bound(key)` returns the upper bound of the key's `# whole:` limit line. It recovers like `value()`: the repository file with no warning, then Git history with one warning. A missing, open or inverted bound raises `conf.Unusable`. `_resolve` wraps `_resolve_setting(key, convert, ...)`; `_range_bounds` is shared with `_limits`.
- A new Review Send uses the live values, each capped at its upper bound (`new_item_dialog.read_new_item_limits` → `NewItemLimits`, one set per request). Saved items, restart recovery (`read_attached_page_screenshot`), attachment (`_with_screenshot`) and capture recovery (`recover_page_captures`) use the upper bounds, so a lower value never hides accepted work. `NoBugPageState` replaces `BugPageState | None`.
- `serve.py` reads the limit set before the New item parse. An unusable limit answers 503 with a retry message, keeps the draft and logs the key. The request read is capped at `body_read_bytes` (upper bounds); `check_live_body_size` refuses a new request over `body_bytes` (live) with 400 "Invalid New item request size." Both come from `_body_budget`. `ListDraftSend.check_first_send` runs the live check before the first List filing, so a matching saved retry stays exact.
- A deferred capture whose pasted images already fill `new_item.max_images` is refused at Send. A new deferred Send reserves against the live `page_capture.max_active` (`PageCaptures.start/reserve(..., limit=)`).
- The console unavailable view catches `conf.Unusable` from the dialog, logs `review-links New item limits: <error>` and omits the owner controls.
- `config/build_admission.conf` has a blank line between blocks, and `build_hold.py` `RELEASE_NOTICE_RUN_LIMIT` is an `ExternalLiteral` row (argv 128 KiB).

**Files:**
- `config/cloud_gate.conf`, `config/review_links.conf`, `config/screen_record.conf`, `config/shot_report.conf`, `config/style_ledger.conf`, `config/test_times.conf`, `config/README.md` — the keys
- `scripts/lib/conf.py` — `whole_upper_bound`, `_resolve_setting`, `_range_bounds`
- `scripts/review_links/new_item_dialog.py` — `NewItemLimits`, `read_new_item_limits`, `_body_budget`, `read_stored_image_byte_limit`, `NoBugPageState`
- `scripts/review_links/serve.py`, `scripts/review_links/review_submissions.py`, `scripts/review_links/console.py`, `scripts/review_links/review_page.py` — the live and upper-bound reads, the 503 and the body checks
- `scripts/aws_target/gpu_size_trial.py`, `scripts/screen_record/screen_record.py`, `scripts/shot_report/transcripts.py`, `scripts/style/style_ledger.py`, `scripts/test_times/module_cost.py` — read at use
- `scripts/lib/config_form_exceptions_test_support.py` — batch rows removed; `RELEASE_NOTICE_RUN_LIMIT` row
- `docs/as-built/review-links.md`, `docs/as-built/screen-record.md`, `docs/as-built/style-showrunner.md`, `commands/screen_record.md` — the keys and the live and upper-bound rule
- `scripts/lib/test_conf.py`, `scripts/review_links/test_serve.py`, `scripts/review_links/test_console.py`, `scripts/review_links/test_review_submissions.py`, `scripts/review_links/test_new_item_dialog.py`, `scripts/review_links/test_list_page.py`, `scripts/review_links/test_new_item.py`, `scripts/aws_target/test_gpu_size_trial.py`, `scripts/aws_target/test_aws_trial.py`, `scripts/aws_target/fixtures/cloud_gate.conf`, `scripts/screen_record/test_screen_record.py`, `scripts/shot_report/test_transcripts.py`, `scripts/shot_report/test_shot_report.py`, `scripts/style/test_style_ledger_tickets.py`, `scripts/style/ledger_test_support.py`, `scripts/test_times/test_module_cost.py`, `scripts/test_times/test_coverage_overlap.py`, `scripts/production/test_tidy_wrapped.py` — `CONF_DIR` fixtures, recovery rows and limit-drop cases

**Binds later work:** `ConfigRead.whole_upper_bound(key)` is the reader for a limit that guards saved or accepted work; a new request reads the live value.

**Gotchas:**
- The limit line is the code contract for saved Review items: a lower upper bound on a `new_item.*` line can make a saved item fail to open.
- A New item body has two budgets: the live one for a new Send and the upper-bound read cap for a saved retry.

**Ruled out:**
- Accepting a ready screenshot over a lowered live byte limit: a Send is a new submission.
- A flat page data byte check in `read_evidence`: the gap was present before this phase; an add-on after the current plan.
- A browser test of the 503 draft: the existing failNextSend case covers draft retention.
- Wait limits (screen recording duration, Review capture expiry, preview wait, attachment retry) stay in code for item 277.

### Phase 13 — Production and direct literals to config  · status: done

#### As-built

- Eight limits are config keys, read at use with `conf.read(stem)`, with their old values:
  - `config/gate.conf`: `rerun.max_failed_modules=8` (`# whole: 0..`) and `nightly.type_line_bytes=512` (`# whole: 3..4096`).
  - `config/production.conf`: `record_store.history_cache_entries=128` (`# whole: 1..`) and `worker.history_bytes=65536` (`# whole: 1..1048576`).
  - `config/shutdown.conf`: `worker.stop_attempts=2` (`# whole: 1..`).
  - `config/direct.conf`: `plan_history.max_rate_factor=2.0` (`# range: 1..`) and `board.max_note_chars=72` (`# whole: 1..900`).
  - `config/orphans.conf` (new, with a `config/README.md` section): `corpus.max_file_bytes=2000000` (`# whole: 1..`).
- `gate.py` `_run_suites` reads `rerun.max_failed_modules` and `memory_stop_reruns` at every gate start, before hold recovery and before any module. An unusable value raises `GateRefused` with the diagnostic. Only ordinary reruns use the rerun limit; memory retries keep their own count, and hold recovery runs with the limit at 0.
- An unusable nightly display limit keeps the saved red result and its assignment; the note shows the diagnostic in place of the cut text.
- Fallbacks for an unusable value: the ETA low and high cells are blank (`EtaProjectionUnavailable`); the board note shows the activity kind; `/orphans` prints the error and exits 2 before the scan; `record_store.historical_issue_folders` skips only the cache and still collects the Git input stamps into `sources`; `worker.py` history prints one `anomaly:` line; `worker_capacity.stop_worker` returns `WorkerNotStopped` with the diagnostic.
- `inventory.last_tool_call()` and `_LAST_STEP_BYTES` are removed. The nine batch `TunableLiteral` rows and `_PRODUCTION_BATCH` are gone. `test_repository_form` has no `config/the_list.conf` absent-file case.
- The Mac Linux and AWS guest tests give the controller and the candidate different rerun limits and prove the candidate limit applies. The AWS test runs the real `gate_box.GUEST` bootstrap.

**Files:**
- `scripts/production/gate.py` — rerun and nightly limits read at gate start
- `scripts/production/record_store.py`, `scripts/production/worker.py`, `scripts/production/worker_capacity.py` — history cache, history bytes, stop attempts
- `scripts/direct/plan_history.py`, `scripts/direct/progress_history.py` — rate factor and board note length
- `scripts/orphans/orphans.py`, `config/orphans.conf` — corpus file size limit
- `scripts/build_targets/test_mac_linux.py`, `scripts/aws_target/test_cloud_gate.py` — guest route proofs

**Gotchas:**
- `LOCK_WAIT_SECONDS` (`worker_capacity.py`) and `CHECK_TIMEOUT_SECONDS` (`phase_reservation.py`) stay in code; item 277 owns wait limits.

**Ruled out:**
- One rate factor read per ETA report: two reads differ only during a live tune, and the next render is consistent.
- A `worker.py` guard for an oversize `<key>.<host>` row: the cause is conf host-row limits, held as an add-on.

### Phase 14 — Runtime state out of config  · status: done

#### As-built

- `accounts.json` and `accounts.lock` live in `~/.local/state/accounts/` after the move. `worker_names.accounts_state()` returns `MigratedAccountsState` when the new file exists, else `LegacyAccountsState` (`~/.claude/config/accounts.json` and its lock). `accounts_config_file()` returns the state's file.
- `worker_names.roles_lock()` locks the live state's lock, checks that the state did not change, and yields the `AccountsState` it locked; on a change it releases and resolves again. A late opener of the old lock deletes the old lock file after the move. `role_map.write_role_map` and `_write_workers_claude_route` take the path from that state, inside the locks.
- `worker_names.read_accounts_document()` is the lock-free read; on a missing file it resolves the state again and reads the moved file. `role_map._local_role_map` and `accounts.resolve_history_label` use it. `CommandRoleMap.local_state` holds the state of a cached map; a changed state reads the map again.
- `accounts.py migrate-accounts-state` runs the move under the old lock: it renames the old file, or writes `{}` when no old file exists, syncs the new folder (also on a rerun), then deletes `config/accounts.lock`. It prints the new path. When both files exist it refuses with `accounts: cannot migrate account state: Both account state files exist: ...` and keeps both.
- `.gitignore` has no `/config/accounts.json`, `/config/accounts.lock` or `/config/showrunners.lock` row. `test_git_exclude.py` expects `config/accounts.lock` not ignored.
- `test_config_form.py` `check_format` scans `config/` on disk and reports each `.json` or `.lock` file that `git ls-files` does not list, ignored or not (rule `untracked runtime file`). `test_repository_form` reports these with no exception list.

**Files:**
- `scripts/accounts/worker_names.py`, `scripts/accounts/role_map.py`, `scripts/accounts/accounts.py` — state resolver, locks, move command
- `scripts/accounts/test_role_map.py`, `scripts/accounts/test_accounts.py`, `scripts/accounts/test_moves.py` — move, race and rerun tests
- `scripts/agents/codex_test_support.py`, `scripts/whoami/quota_alert_test_support.py`, `scripts/agents/test_agent_exec.py`, `scripts/agents/test_worker_lifecycle.py`, `scripts/direct/test_assign_claude.py` — fixtures on the new path
- `scripts/lib/test_config_form.py`, `scripts/lib/test_git_exclude.py`, `.gitignore`
- `docs/accounts.md`, `docs/as-built/accounts.md`

**Gotchas:**
- Until the move runs on a machine, its `~/.claude/config/accounts.json` is untracked and not ignored, and `test_repository_form` fails in that checkout. Run the move soon after the pull.
- The live check is open: after the merge, both machines pull and restart long-lived account readers, then run `accounts.py migrate-accounts-state`, check the accounts table, worker selection (remote on the Mac) and one historical label, and delete a stale `config/showrunners.lock`. A "Both account state files exist" refusal stops the check: ask which document is current.

**Ruled out:**
- A cleanup of an old lock left by an interrupted late opener: a rerun of the move deletes it.
- A guard for a symlinked `accounts.json`: the live files are regular files, and every writer replaces the file atomically.

### Phase 15 — Context deferral rules to the common form  · status: todo
**Depends on:** Phase 4

#### Work Order

**Goal:** `config/context_deferral.conf` holds only the `window` key as a commented, limit-checked `key=value` row, the hook's phrase patterns live in a data file, and the hook reads `window` through `conf.read`.

**Spec:**

0. **Wait.** nate gave the go for all four moves on 11-24 at 07:50 PST (Questions page); the 35 working names stand. This is Move 4 of the question. Start only after Phases 1 and 2 (the rules and checks) are on main.
1. **window.** `config/context_deferral.conf` keeps one row, `window=200`, with its comment and the limit line `# whole: 1..`. The key name and the value stay.
2. **Patterns.** The `action`, `marker`, `state`, `later` and `exclude` rows move with their comments, in order and byte for byte, to the data file `config/context_deferral_patterns.txt` (rule 10). Each line is `<kind>: <regular expression>`; a kind can repeat.
3. **Reader.** `scripts/hooks/stop-context-deferral.py` (line 44 reads `conf.path("context_deferral")` by hand) reads `window` with `conf.read("context_deferral").whole("window")` at use. It parses the pattern file as data from the same folder (`conf.directory()`), so a test sets both with `CONF_DIR`. The hook runs on each stop: time it before and after, and name the added cost in the reply.
   - **Config faults.** Today any failure permits the stop (`main` at line 123). A `window` that `conf.read` recovers from the committed file applies, as for every conf key. An unusable `window` or an unreadable pattern file permits the stop, as today. Test each case.
   - **Duplicate rows.** Today two `window` rows permit the stop (`test_invalid_entries_allow_the_stop_without_output`). The common parser keeps the first value, so `whole("window")` alone would block the same stop. Keep today's behavior: the hook treats a duplicate `window` row as a config fault and permits the stop, while a single valid `window` recovered from Git still applies. Keep that test. Find the duplicate in the same read that gives `window`. A config fault permits the stop with no output: the `conf.read` diagnostic must not reach stderr (`test_invalid_entries_allow_the_stop_without_output` asserts empty output). The test lane adds a case where a single committed `window` recovered from Git still blocks the stop (architect after Phase 14).
4. **Allow lists.** Remove the `FORMAT_FILES` row of `config/context_deferral.conf`. Replace the `stop-context-deferral.py` reader row with a `PERMANENT` row that reads `context_deferral_patterns.txt` as data. Add `config/context_deferral_patterns.txt` to `DATA_FILES`.
5. **README.** Update the `## context_deferral.conf` section of `config/README.md` (line 159), and name the data file.
6. **Tests.** `scripts/hooks/test_stop_context_deferral.py` writes `context_deferral.conf` with `write_config_fixture` and a pattern file in `CONF_DIR`. The same transcripts give the same decisions before and after.

**Files:**
- `config/context_deferral.conf`, `config/context_deferral_patterns.txt` — new data file
- `scripts/hooks/stop-context-deferral.py`, `scripts/hooks/test_stop_context_deferral.py`
- `scripts/hooks/test_hook_dispatch.py` — writes `context_deferral.conf` and copies `context_deferral_patterns.txt` into `CONF_DIR`; keeps the exit 2 dispatch assertion and its diagnostic
- `scripts/lib/config_form_exceptions_test_support.py`, `config/README.md`

**Workers:** 1 writer + 1 tester — the hook and its data are one change.
- `impl` — `config/context_deferral.conf`, `config/context_deferral_patterns.txt`, `scripts/hooks/stop-context-deferral.py`, `config/README.md`; hub: `scripts/lib/config_form_exceptions_test_support.py`
- `test` — `scripts/hooks/test_stop_context_deferral.py`: the fixture form and the before-and-after decision rows; `scripts/hooks/test_hook_dispatch.py`: both files in `CONF_DIR`

**State:** none

**Constraints from prior phases:**
- Phase 3 put `config/context_deferral.conf` on `FORMAT_FILES` and `scripts/hooks/stop-context-deferral.py` on the reader allow list (`reads=("context_deferral.conf",)`), both with `RemoveInPhase("Context deferral rules to the common form")`.
- Phase 4 made `conf.read` the reader pattern; tests use `CONF_DIR` with `write_config_fixture`.
- Phase 1 rules: a limit line directly above each number key; a blank line between blocks. `check_format` reports a `.conf` line that is not blank, a comment, a section line or a `key=value` row.

**Acceptance gate:**
- The hook tests pass, and the same transcripts give the same decisions.
- `test_config_form.py` passes with the `FORMAT_FILES` row removed.
- `scripts/production/fixture_conf_keys.py --repo <worktree> --base <phase start> --tip <candidate commit>` exits 0. It does not check dotted keys, so the director also reads each `CONF_DIR` fixture of each changed reader, shell and subprocess fixtures included.
- The queued `gate.py --affected` run passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 16 — User zone helper start time  · status: done

#### As-built

- `scripts/lib/conf.py` imports `subprocess` only inside `_git`, the one function that runs Git. `CompletedProcess` is a `TYPE_CHECKING` import. A plain `conf.read(stem)` loads no `subprocess`, `decimal` or `scripts.lib.models_registry`.
- Values, diagnostics, host overrides and Git recovery do not change.
- The configured CLI median is 88.50 ms (phase base 167.26 ms, Phase 4 parent 65.94 ms, all under load 55-75). `dataclasses` (through `inspect`) is the largest remaining cost, about 10 ms cumulative.

**Files:**
- `scripts/lib/conf.py` — lazy `subprocess`
- `scripts/lib/test_user_zone.py` — the configured subtest of `test_cli_import_budget_without_site` asserts `subprocess`, `decimal` and `scripts.lib.models_registry` unloaded
- `scripts/lib/test_build_folders.py` — the Git guard patches `subprocess.run`

**Gotchas:**
- A Git guard patches `subprocess.run`, the attribute every caller shares. A patch of `conf._git` misses a Git call on another path.
- `socket` (host override on each read) loads `math` through `selectors`. `collections.abc` loads before `conf` through `runpy`.

**Ruled out:** lazy `math`, `collections.abc` and `sys` imports: measured, and no module leaves the load list.

### Phase 17 — Build tool limits follow-ups  · status: done

#### As-built

- `report.RebuildClasses()` reads `report.max_edited_crates` at first use and keeps it (`cached_property` on `edited_crate_max`, `edited_label` and `cascade_label`). `labels()` and `label(crates_compiled, warm)` are the one bin rule.
- `report()` makes one `RebuildClasses` and passes it to the rebuild table, the package rows and `moments_section(connection, day, classes)`. `power_profile_section` and `buildlog moments` (`scripts/buildlog/cli.py`) each make their own instance.
- A moments report with an unusable edited-crate limit and no comparison window succeeds; with a comparison window it raises `conf.Unusable`.
- `nextest_log.run_logged` collects the replay error and conf warnings with `conf.diagnostics(report=False)`. It writes them to the output stream and flushes before `main` prints `mac_exit`. It writes to stderr only if that write fails. The `run TAIL SUMMARY COMMAND...` form does not change.

**Files:**
- `scripts/buildlog/report.py`, `scripts/buildlog/cli.py` — lazy classes, one per report
- `scripts/buildlog/test_report.py` — one read for each report pass, with a write between reads
- `scripts/buildlog/test_moments.py` — `test_moments_command_shows_profile_without_a_usable_rebuild_limit`
- `scripts/mac_test/nextest_log.py` — replay diagnostics on the output stream
- `scripts/mac_test/test_offload_cleanup.py` — row `1048577` of `test_nextest_replays_success_and_retains_bounded_failure_output` checks the diagnostic order under `/bin/sh` and `zsh -f`
- `docs/as-built/build-targets.md`, `docs/as-built/build-followups.md` — the new forms

**Gotchas:** over SSH, stdout and stderr have no order. `offload.py` merges them and treats output after `mac_exit=` as `MISSING_MAC_STATUS`. A Mac-side diagnostic goes on the status stream before the status line.

**Ruled out:** a cap in code on the edited-crate maximum: the limit line `whole: 1..48` holds the bound.

### Phase 18 — Message limit follow-ups  · status: done

#### As-built

- `pushover.parent_source()` never raises `conf.Unusable`. With an unusable `pushover.max_source_chars` it prints the diagnostic on stderr and returns `parent pid <ppid>`. Conf warnings of a usable read print once, through `conf.diagnostics(report=False)`.
- `cancel KEY` with no `--source` and an unusable limit sends the cancel request and logs its outcome with source `parent pid <ppid>`.
- `send` takes `arguments["source"] if arguments["source"] is not None else parent_source()`. The parsed source is never `None` there, because `parse` falls back to the template sender, so `parent_source()` runs only in `cancel`. An empty template sender is refused with `no sender` and nothing posts.
- `waste_watch.watch()` holds the wait share as `float | conf.Unusable`. With an unusable `alert.max_wait_share` the pass sends no wait alert, still sends each context alert, and raises the error after the pass, so `main` exits 1 with the diagnostic on stderr. When the state write fails in that pass, the config diagnostic prints before the write error.
- `docs/as-built/usage-ledger.md` states the waste watch rule.

**Files:**
- `scripts/notify/pushover.py` — the `parent_source` fallback
- `scripts/notify/test_pushover.py` — the unusable-limit cancel row of `test_cancel_posts_cancel_by_tag_with_the_token_and_logs`; the send-path row of `test_an_empty_sender_is_refused_and_nothing_is_posted`
- `scripts/usage/waste_watch.py` — the wait-share fault path
- `scripts/usage/test_waste_watch.py` — `test_the_command_sends_context_alerts_when_the_wait_share_is_unusable`, with and without a failed state write
- `docs/as-built/usage-ledger.md` — the rule

**Gotchas:**
- `send` always has a source from the template sender. A change that treats an empty source as absent sends the parent command as sender in place of the `no sender` refusal.

**Ruled out:**
- A sender limit check before silence keeps a push: the release refuses and logs a kept push with an unusable limit.

### Phase 19 — Live config reads by path recover like stem reads  · status: done

#### As-built

- `conf.read(file)` sets `source = path(file)` from the caller's argument first. Then `_live_stem(file)` turns a `.conf` `Path` whose expanded, resolved parent equals `directory().resolve()` into its stem.
- `ConfigRead.file` is that stem, so a missing key or file recovers from the repository, and from Git, as a stem read does. `ConfigRead.source` and the `load` snapshot key stay the caller's path.
- A path outside the live folder does not use the repository. With `CONF_DIR` set, `directory()` is the fixture folder and the repository stays off.
- `mac_linux.read_settings` and `eligibility.read_target_settings` read their settings by path, so a new key in `mac_linux.conf` or `mac_test.conf` recovers before it is on main.

**Files:**
- `scripts/lib/conf.py` — `_live_stem` and the `read()` order
- `scripts/lib/test_conf.py` — stem and live-path rows of `test_missing_live_key_uses_repository_without_changing_live_values` and `test_value_origin_uses_repository_for_an_absent_file`; the outside path of `test_existing_invalid_key_and_explicit_missing_path_do_not_use_repository` is `self.directory / "accounts.conf"`
- `scripts/build_targets/test_mac_linux.py` — `test_read_settings_recovers_the_missing_connect_attempt_limit`
- `config/README.md` — the path rule

**Gotchas:**
- `target_config._settings_path` resolves the settings path. A rewrite of `source` to the stem splits the admission snapshot under a symlinked or relative `CONF_DIR`, and a `x.conf.conf` stem fails `path()`.
- `write_config_fixture` always merges the repository template. A test of a missing key removes the key from the file after the write.

**Ruled out:** a home folder check for outside paths: every stem read already calls `directory()`, and no process without a home folder reads config by path.

### Phase 20 — Tests follow the config moves  · status: done

#### As-built

- Each test that sets `CONF_DIR` writes each config file and key that the code under test reads, with `write_config_fixture` (`scripts/lint/memory_test_support.py`). A test that needs only a fixed zone pins `USER_ZONE`. The 226 tests in 26 modules that the config moves broke pass again.
- `review_items.question_counts` watches `conf.path('user_zone')` as the zone file source of the question count cache.
- The typed allow lists of the config form test live in `scripts/lib/config_form_exceptions_test_support.py`, so `is_test_code` skips them in production scans. The rows are unchanged.
- The observer test in `scripts/accounts/test_refusal_watch.py` reads its path case from a `.conf` copy outside the live folder, so the path read keeps its own `ConfigRead.file`.
- The stall-watch grace tests remove only `prompt_grace_seconds` from the `stall_watch.conf` fixture. The sender keys stay in the fixture.

**Files:**
- `scripts/review_links/review_items.py` — the zone file source of the question count cache
- `scripts/lib/config_form_exceptions_test_support.py` — the typed allow lists of the form test (test data)
- `scripts/accounts/home_test_support.py`, `scripts/review_links/browser_test_support.py` — shared fixtures that write `user_zone.conf` and `review_links.conf`
- `scripts/production/test_unit_phases.py` — `test_unit_count_cache_filters_sources_and_retries_failed_reads` checks that the cache watches the configured zone file
- the other repaired test modules — fixtures and read-log checks

**Binds later work:** Each config move adds the moved file or key to each `CONF_DIR` fixture that reads it. `fixture_conf_keys.py` skips dotted keys, so the director reads each `CONF_DIR` fixture of a dotted-key move.

**Gotchas:**
- With `CONF_DIR` set, the repository is off. A missing fixture file or key raises `conf.Unusable`, or runs Git under a test that patches `subprocess.run`.
- `conf.read` reads with `Path.read_text` and records the stem through `conf.path`. A read log uses `conf_reads.record_reads`, not a spy on `conf_reads.read_text`. `conf_reads.record_reads` takes priority over `CONF_READ_LOG`.

**Ruled out:**
- A stall-watch fallback for absent sender limits: the packaged fallback on main read the same live file, so main sent no alert either.
- A built path that hides the pushover rows from the phone-send scan: the module took the test-helper name.

### Phase 21 — Review of the commits outside any phase  · status: done

#### As-built

- Review of the parts of tip 6f7fe87082 that no phase review covered, checkpoint e82392c2ed.
- bob's names for two new keys: `report.max_edited_crates` and `module_cost.max_output_replay_bytes`. Values did not change. nate approved the 26 key names (item 224).
- `rebuild_row._read_cache` calls `cache_path(config)` again, as main does. Test fixtures write `user_zone.conf` and `review_links.conf`.
- Each main join conflict keeps main's behavior and reapplies the config moves: reads through `conf.read`/`conf.path`, no code defaults.

**Binds later work:** Each new key's value equals the literal it replaces. No key on main changes, is renamed or is deleted. No code default and no new refusal.

### Phase 22 — Report removed config keys that code still reads  · status: done

#### As-built

- `scripts/production/fixture_conf_keys.py` also lists config key reads on the tip, after the fixture lines (List item 424, report form only). The list never changes the exit code and refuses nothing.
- Two kinds: a key that the tip removes or renames in `config/<name>.conf` (a literal in a non-fixture Python source, or a whole shell token), and a fixed-name read of a key that no tip config file holds (`<ConfigRead>.<accessor>("<key>")` in Python; `conf_value`, `conf_get`, `conf.py get` in shell).
- A key is held when a tip config file has it or a key that starts with `<key>.`. A key still held by another file counts only when the source names the removed file.
- Output lines: `removed keys in config/<name>.conf: ...`, `<source>:<line>: reads <key>, which the tip removes from config/<name>.conf`, `<source>:<line>: reads <key>, which no config file on the tip holds`, and the summary `config key reads: <N> of a removed key, <M> of a key no config file holds`. A source that cannot be read or parsed is counted as not scanned.
- `docs/production_format.md` step 3 documents the list.

**Files:**
- `scripts/production/fixture_conf_keys.py` — the read scan
- `scripts/production/test_fixture_conf_keys.py` — its tests

**Binds later work:** Phase 24 closed the routed recall gaps F008-F011.

**Gotchas:** The scan takes about 10 s on the full tree. It reports `scripts/aws_target/hana_source.py` 106 and 107 on main (List 357 owns that repair).

### Phase 23 — Join with combo2 before its merge  · status: done

#### As-built

- The scratch joins merged sdlc combo2 (`90ddc24ca9`, `3b9bec287e`) and then devtools `ca31f2226d` into the branch in `join224`. After devtools reached main, `9f4b36d18c` merged main `5a70285346` with no conflict.
- The resolutions kept combo2's behavior and the branch's config moves through `conf.read`/`conf.path`. No key was added, renamed or deleted. No refusal was added.
- The phase has no checkpoint commit; its work is the join merge commits.

### Phase 24 — Read scan recall follow-ups  · status: done

#### As-built

- Reader names: a name is a reader when any binding of it in its scope, an enclosing function scope or the module is a reader. `global`, `nonlocal` and comprehension walrus bindings go to the scope Python uses. A rare false report is accepted; a missed read is not.
- Key-string variables take the last assignment before the call. A loop over a literal adds its strings and keeps the earlier values.
- Relative imports resolve against the source path's package.
- Shell: `NAME=value` and redirect words before the command are skipped; the next word is the command, even a shell keyword. An interpreter word (`py`, `python*`) before `conf.py get` is accepted.
- The scan uses the shared batch helper in `scripts/style/git_process.py`. The helper reads in memory and can report a missing object without stopping the batch; its default mode still refuses a missing object.
- `docs/production_format.md` step 3 names the shell here-document limit.

**Files:**
- `scripts/production/fixture_conf_keys.py`, `scripts/production/test_fixture_conf_keys.py`
- `scripts/style/git_process.py`, `scripts/style/test_git_process.py`

**Gotchas:** The module is about 890 lines. The full scan takes 6-12 s with load.

### Phase 25 — Refuse a removed config key that main still reads  · status: done

#### As-built

- User words (nate, 12:27 PST 2026-11-25 via morty): "the item 424 check may refuse a change that removes a config key that code on main still reads, and it names the key and the file."
- The check exits 1 when the tip removes key K from config file C and the same source path on the base and the tip reads K from C. The refused line ends with `; main reads it too: refused`. A summary line `config key check: refused <N> reads of a removed key that main still reads` follows.
- Precision over recall for the refusal only. Refusal forms: a Python inline loader call with one positional literal file and no keywords, or a reader name with one binding in the module; a literal key. Shell: `conf_value` or `conf.py get` with a literal file and key, in a source with no `<<` and no local `conf_value` function. Every other read stays advisory.
- A read in dead code still refuses: it is main code that names a removed key.
- The report keeps refused reads (`RefusedKeyRead`) apart from advisory reads. One line and one count per source occurrence.
- Base blobs are scanned only for sources with a refusal candidate; an identical blob reuses the tip result.

**Files:**
- `scripts/production/fixture_conf_keys.py`, `scripts/production/test_fixture_conf_keys.py`, `docs/production_format.md` step 3

**Gotchas:** The advisory scan was built for recall (phase 22). Three review rounds found false refusals until the refusal got its own precision rule.

### Phase 26 — Test fixtures for the conf reads of X  · status: done

#### As-built

- Seven test modules write `user_zone.conf` with `write_config_fixture` in their fixture config directory. `test_change_page` writes `notify.conf` there; its SSH timeout test keeps its assertion.
- The 22 tests that failed on X 9120438c18 pass. No code default, no config change and no weakened assertion.
- Showrunner ruling (16:02 PST): ship X with the live reads as they are. `user_zone()` and notify `review_timeout_seconds` have no code default; a runtime with no config copy raises `conf.Unusable`.

**Files:** `scripts/build_queue/test_kill_action.py`, `test_memory_feed.py`; `scripts/lib/test_main_sync.py`, `test_waiting_rows.py`; `scripts/rebuild/test_reason.py`; `scripts/review_links/test_change_page.py`, `test_new_item.py`; `scripts/worktree_delete/test_perform_deletion_rerun.py`

### Phase 27 — Test fixtures for the Phase 13 and 14 config reads  · status: todo

**Depends on:** Phase 13, Phase 14

#### Work Order

**Goal:** The queued affected run of tip d62d695234 is green again. It was red in 9 test modules.

**Spec:**
1. Each test fixture that builds its own config folder writes each key the code now reads: `record_store.history_cache_entries` (production.conf), `rerun.max_failed_modules` (gate.conf), and the other keys the red log names. Use `write_config_fixture`.
2. A code read of the wrong file or path is fixed where it lives.
3. No code default, no config change, no new refusal and no weakened assertion. A test that pins behavior the branch changed on purpose goes to the showrunner.
4. A failure that main 9cd539b198 also has is reported, not fixed.

**Files:** `scripts/aws_target/test_aws_trial.py`, `scripts/build_targets/test_mac_linux.py`, `scripts/buildlog/test_mac_idle.py`, `test_machine_watch.py`, `scripts/notify/test_pushover.py`, `scripts/production/test_waiting_why.py`, `scripts/review_links/test_list_page.py`, `test_review_submissions.py`, `scripts/statusline/test_statusline.py`

**Workers:** 2 writers, split by test module.
- `impl`: aws_target, build_targets, buildlog, notify.
- `test` opens as impl: production, review_links, statusline.

**Constraints from prior phases:** Phase 20: each config move adds its keys to each `CONF_DIR` fixture that reads them. Phase 21: no code default and no new refusal.

**Acceptance gate:** each failed test passes by test id with no `'<missing>'` warning; then the queued `gate.py --affected` run of the new tip is green.
