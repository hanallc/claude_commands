# Remove the banned-word system

> **Status: Source changes complete. Deployment and state cleanup are pending.**
> **As-built disposition: amend** — `docs/as-built/hook-dispatch.md` and `docs/as-built/merge-gate-and-test-speed.md`

## Delegation Context

- **Project:** Devtools installs Claude hooks and commands and generates Codex skills from the commands.
- **Stack:** Python, Zsh, Markdown, JSON, and the separate `nate_style` Git repository.
- **Layout:** `settings.json` registers events. `scripts/hooks/` dispatches them. `config/review/` supplies review prompts. `scripts/style/` reads style rules and tracks findings.
- **Key files:** Each Work Order lists the source files, line references, and deployment paths for its changes.
- **Test lanes:** Python `unittest` modules beside the affected files. No Rust crate changes.
- **Build and test:** Run each named module with `scripts/lib/py -m unittest MODULE` from the repository root.
- **Lint:** Run `basedpyright` on changed Python files and their importers. Run `git diff --check`.
- **Invariants:** The 2026-11-18 request retires the word list and its enforcement. Preserve the other style rules, shared hooks, and recorded decisions.

## Phases

### Phase 1 — Disable the hook routes · status: done

Commit `56fd8993f` disabled the routes and replaced the hooks with temporary stubs.

**Depends on:** none

#### Work Order

**Goal:** Both hook events continue to work during deployment, including in sessions with older settings.

**Spec:**

1. Remove the entries for `post-tool-use-banned-words.py` and `stop-assistant-prose-banned-words.py` from `hook_dispatch.py`.
2. Remove the four command overrides from `settings.json` and remove direct registrations if an installed copy still has them.
3. Retain the shared PostToolUse and Stop dispatch commands, their other hooks, and the separate Berth and continuation registrations.
4. Replace the two active script bodies with silent exit-zero stubs. Keep `post-tool-use-banned-words-block.py` as a stub during deployment.
5. Install the stubs and dispatcher on natedev and the Mac before deploying their settings. Hooks run only from `~/.claude`, so the main merge and the normal publish are the whole deploy; unit worktrees take the change at their next main merge.
6. Check settings on both machines for direct routes. Wait for sessions with old snapshots to end or restart before Phase 2 deletes the stubs.

The repo and installed settings already use the dispatcher. The second PostToolUse script is a retired path for older sessions. The settings have no direct registration for any of the three scripts on natedev.

**Files:**

- `settings.json` — remove the overrides at lines 235–238. Preserve the event registrations at lines 252–267 and 404–413.
- `scripts/hooks/hook_dispatch.py` — remove the slots at lines 110 and 117.
- `scripts/hooks/post-tool-use-banned-words.py` — replace the tool-output enforcement at lines 2 and 47–166.
- `scripts/hooks/post-tool-use-banned-words-block.py` — keep the legacy stub at line 2 until Phase 2.
- `scripts/hooks/stop-assistant-prose-banned-words.py` — replace the prose logger and counter writer at lines 2–5 and 67–78.
- `scripts/hooks/test_hook_dispatch.py` — update the route table at lines 43 and 48. Remove the guide fixture and counter comparison at lines 53, 78–99, 159–161, and 200–258. Keep dispatcher equivalence at lines 289 and 331. Check that both events run without invoking any retired path.
- `scripts/hooks/test_post_tool_use_banned_words.py` — replace the enforcement tests at lines 20–143 with direct exit-zero coverage of both PostToolUse stubs.
- `scripts/hooks/test_stop_banned_words.py` — replace the logger tests at lines 16–47 and below with direct exit-zero coverage of the Stop stub.
- `docs/as-built/hook-dispatch.md` — remove the two dispatch rows or entries at lines 32 and 34.

**Workers:** 2 writers. One owns routing and documentation, and one owns hook tests.

- `impl` — own settings, the dispatcher, the three scripts, and the as-built file. Hub `settings.json`.
- `test` opens as impl — own the three hook test modules.

**State:** Deploy settings and stubs. On natedev, `~/.claude/settings.json:236–239` has the four overrides and lines 267 and 420 call the dispatcher. The `settings.json` in each of `~/.claude1`, `~/.claude2`, `~/.claude3`, `~/.claude4`, and `~/.claude5` links to that same file. Preserve those links through the normal settings deployment. Verify each account on both machines.

**Constraints from prior phases:**

**Acceptance gate:** Run `scripts.hooks.test_hook_dispatch`, `scripts.hooks.test_post_tool_use_banned_words`, and `scripts.hooks.test_stop_banned_words` separately. Check both installed dispatch tables and parse each settings file. Prove that all three legacy paths exit 0 without writing a count or a log. Confirm that no active session retains a direct registration before Phase 2.

### Phase 2 — Remove the commands, scanner, and counters · status: done

Commit `89e4f9075` removed the commands, scanner, and counters.

**Depends on:** Phase 1

#### Work Order

**Goal:** The tools, review prompts, and generated skills stop using the scanner and counters.

**Spec:**

1. Move `READ_ONLY_PROGRAMS` and `is_read_only_command` into `tool_call_text.py`. Keep `scan_text` behavior for the Bash build-hold guard.
2. Remove the scanner from the style review prompt. Keep the Rust guide checklist, report markers, severity, and source locations.
3. Use a checklist row with `Skip (no Rust change)` for non-Rust changes. Keep the existing marked Pass result.
4. Remove the counter summary and its now-unused Python setup from the Rust style loader. Keep rule discovery, scopes, links, and guide output.
5. Delete the dedicated modules, command files, registration tests for the stubs, config, and type-check entries listed below.
6. Deploy the command deletions and run the existing Claude-to-Codex sync. Confirm that it prunes the four generated skills.

**Files:**

- `scripts/hooks/tool_call_text.py` — remove the scanner import at line 23. Preserve `scan_text` and `mask_removed_text` at lines 62–87.
- `scripts/hooks/pre-tool-use-build-hold-guard.py` — keep the caller at lines 19 and 103. Keep its shell entry at `scripts/hooks/pre-tool-use-build-hold-guard.sh:18` and the registrations at `settings.json:298` and `scripts/hooks/codex_hooks.py:92`. Its as-built description remains at `docs/as-built/build-memory-admission.md:1392–1396`.
- `scripts/hooks/banned_words_lib.py` — delete after moving the helper at lines 72–95. Lines 26–45 name the state, config, and comment marker.
- `scripts/hooks/banned_word_hook.py` — delete the toggle and status command at lines 16–80.
- `scripts/hooks/{post-tool-use-banned-words.py,post-tool-use-banned-words-block.py,stop-assistant-prose-banned-words.py,test_post_tool_use_banned_words.py,test_stop_banned_words.py}` — delete the Phase 1 stubs and their tests.
- `config/banned_words.conf` — delete the switch documented at lines 1–17.
- `commands/{banned_word_hook.md,add_banned_word.md,revert_banned_count.md,banned_word_analysis.md}` — delete the four entry points at lines 17, 8, 13, and 18 respectively.
- `scripts/banned_word_analysis/banned-word-gradient.py` — delete the renderer and inline Pillow dependency at lines 1–6, 36–38, and 61.
- `scripts/claude_to_codex/{run_sync.sh,sync.py}` — keep the sync. The command scan and pruning at `sync.py:220–256` and `run_sync.sh:63` handle deployment.
- `config/review/style.md` — delete the scan at lines 14–16 and the scanner result and failure wording at lines 27–29. Keep the caller and diff input in `scripts/lint/style_review.py:199`.
- `commands/lint_config.md` — remove the scanner from the description at line 53.
- `scripts/lint/lint_config.sh` — use the same description at line 30.
- `scripts/rust_style/load-rust-style.sh` — remove the counter output at lines 427–469 and the Python setup at lines 4–9. Remove the obsolete matcher comment at lines 331–335. Keep generic formatting.
- `scripts/lint/test_style_review.py` — remove the scanner assertion at line 228 and the scanner test at lines 255–271. Keep the cold-cache fixture at lines 273–297 and check that an old counter file has no effect.
- `scripts/lib/test_user_zone.py` — remove the log timestamp exception at line 34.
- `pyrightconfig.json` — remove the execution environment for the analysis directory at lines 111–114.
- `scripts/production/merge_gate.py` — remove `pillow==13.0.0` and its comment at lines 104–107. Keep the Blender stub requirement. Check the pin fixture at `scripts/production/test_merge_gate_suites_types.py:164–172`.
- `docs/as-built/merge-gate-and-test-speed.md` — update the type-check and interpreter descriptions at lines 563 and 950.
- `docs/as-built/roles-and-names.md` — remove the obsolete scanner note at line 55.

**Workers:** 2 writers. One owns hook and command deletion, and one owns the review and type-check changes.

- `impl` — own the hook files, the four commands, the analysis renderer, the switch, and the timezone fixture.
- `test` opens as impl — own the review prompt, style loader, lint descriptions, type-check files, and associated tests and docs.

**Constraints from prior phases:** Both dispatcher events work without the retired routes. Installed settings and active sessions have no direct routes that need the stubs.

**Acceptance gate:** Run `scripts.hooks.test_hook_dispatch`, `scripts.hooks.test_build_hold_guard`, `scripts.hooks.test_codex_hooks`, `scripts.lint.test_style_review`, `scripts.lib.test_user_zone`, and `scripts.production.test_merge_gate_suites_types` separately. Check a Rust and a non-Rust review report. Confirm that the loader still prints the guide and leaves an old counter untouched. Check the installed command list and generated skill directory for all four deletions.

### Phase 3 — Retire the rule and remove deployed state · status: done (source)

Commit `992b17d51` removed the source instructions and settings keys.
After merge, deploy the source changes and regenerate the installed Codex skills.
The style repository owner removes the rule and the charter reference.
The director handles the tickets and state cleanup.

**Depends on:** Phase 2

#### Work Order

**Goal:** Current prompts, rule catalogs, and installed state no longer contain the word-list system.

**Spec:**

1. Remove the word-list section from the source `CLAUDE.md` and deploy it to both machines. Update any independent project or account copy.
2. Remove the explicit word-list clauses from the retained commands and review charter. Keep their other review, naming, and editing rules.
3. Retire `~/rust/nate_style/rust/forbidden-words.md` in the style repository after the consumers ship. Preserve its Git history and recorded findings.
4. Remove the retired rule from live ordering and the live family inventory. Preserve the edges between the remaining rules.
5. Keep generic rule discovery and `pre_filter` coverage. The loader and style catalog will stop selecting the rule when the active file leaves.
6. Use the normal ticket workflow to close or replan open work for this rule. Preserve unrelated work and closed history.
7. Refresh the generated Codex skills for the retained commands that changed.
8. Perform the one-time state cleanup below after deployment confirms there are no writers or readers.

The external rule is a real input to the global catalog. Its frontmatter at `~/rust/nate_style/rust/forbidden-words.md:1–11` classifies it. Its body at lines 13–28 contains the word list policy. No separate rule needs that policy once its live consumers retire. Retain the other style files and the independent guidance on plain language.

**Files:**

- `CLAUDE.md` — remove the section at lines 3–4. Verify `~/.claude/CLAUDE.md:3–4` and the same lines in `~/.claude1/CLAUDE.md`, `~/.claude2/CLAUDE.md`, `~/.claude3/CLAUDE.md`, `~/.claude4/CLAUDE.md`, and `~/.claude5/CLAUDE.md`. The account paths link to the shared file. Preserve the link behavior at `scripts/accounts/accounts.py:98–105,819`. Update active worktree copies through their normal update.
- `commands/adversarial.md` — remove the direct file read at line 24.
- `commands/adhoc_review.md` — remove the vocabulary clauses at lines 9 and 169.
- `commands/apply_example_layout.md` — remove the explicit list rule at line 123. Keep the guide load at line 27.
- `commands/module_review.md` — remove the explicit list rule at line 93. Keep the guide load at line 89.
- `commands/plan/phase_review.md` — remove the vocabulary clause at line 426.
- `docs/user_facing_explanation.md` — remove the list reference at line 21. Keep the separate requirement to define terms at line 86.
- `scripts/style/order_waste.py` — remove the rule and its edges at lines 51, 71–72, 96, 137, 145, and 171. Preserve the order of remaining rules.
- `scripts/style/test_order_waste.py` — substitute an active rule in the ordering fixture at line 147. Replace the retired rule assertion at line 367 with the relevant remaining edge.
- `scripts/style/test_style_ledger_tickets.py` — use a neutral rule with the same scope in the stale-judgment test at lines 2104–2106. Keep the generic `pre_filter` fixture at lines 1229–1231.
- `scripts/style/test_style_ledger.py` — keep the generic `pre_filter` test at lines 1850–1859.
- `docs/style_production.md` — update the live family and ordering guidance at lines 27 and 131. Keep the measured history at lines 172–173.
- `docs/plans/natedev-verify-next.md` — remove the obsolete exemption marker at line 5. Keep the quoted words.

**Workers:** 2 writers. One owns instruction text and the other owns ordering and fixtures.

- `impl` — own `CLAUDE.md`, command and explanation edits, and the deployment checklist. The style repo owner applies the external rule and charter changes.
- `test` opens as impl — own style ordering, its tests, and the style production guide.

**State:** At deploy on each machine, remove these files once if present. Confirm the same paths are absent after a second deploy check.

- `~/.claude/state/forbidden-word-counts.json`, `~/.claude/state/forbidden-word-counts.json.bak`, and `~/.claude/state/forbidden-word-counts.lock`.
- `~/.claude/state/.forbidden-word-counts.json.<pid>.tmp` for abandoned writes (`scripts/hooks/banned_words_lib.py:256`).
- `~/.claude/state/forbidden-word-prose.log` and the installed `~/.claude/config/banned_words.conf`.
- The default `${TMPDIR:-/tmp}/banned_word_gradient.png` from `scripts/banned_word_analysis/banned-word-gradient.py:61`, if present.

Use only the named files and the numeric temporary-file pattern. Preserve shared state and logs. Keep user-specified report output paths. The implementation adds no startup cleanup. The statusline scripts and `config/statusline.conf` have no consumer.

**Constraints from prior phases:** Scanner and command deletion has shipped. The remaining style review uses the ordinary Rust guide. The loader has no counter display. The external change removes the explicit reference in `~/rust/nate_style/review-charter.md:23` and preserves other edits there. The loader discovers files at `scripts/rust_style/load-rust-style.sh:229–248`. Focused evaluation uses `scripts/style/style_rules.py:18–22,259`. The ledger's global catalog discovers the same files at `scripts/style/rules.py:509–524`. Its ticket workflow has `open` and `drop` at `scripts/style/style_ledger.py:9686,9839–9840`. Keep these generic readers, `commands/rust_style.md:9`, `commands/focused_eval.md:56`, and `scripts/hooks/test_direct_command.py:125,170`. Keep the loader documentation at `commands/succinct_style.md:9`, `docs/direct/write_prompt_contract.md:21`, and `docs/as-built/style-showrunner.md:84,1065,1075`. `docs/plans/builds-natest.md:1021` records the old loader setup.

**Acceptance gate:** Run `scripts.style.test_order_waste`, `scripts.style.test_rules`, `scripts.style.test_style_ledger`, and `scripts.style.test_style_ledger_tickets` separately. Run `zsh scripts/rust_style/load-rust-style.sh --list-files` and `scripts/lib/py -m scripts.style.rules list --json`. Check that both lists show the remaining rules and omit the retired rule. Check that historical reports still open. Search the source, installed settings and instructions, and the style repository with `rg -in 'banned|forbidden.words|forbidden-words'`. Check filenames with the same pattern. Require zero active references. The plan and these recorded results may still match:

- `docs/plans/build-followups-style-pilot.md:116,783` and `docs/plans/build-followups-style-next.md:11` record the prior rule and stale-judgment behavior.
- `docs/plans/builds-natest-streamline-a.md:135,2075–2144,2829,2840` and `docs/plans/builds-natest-streamline-b.md:55` record test and command measurements.
- `docs/plans/sdlc-roles-design.md:148,161,168` and `docs/plans/sdlc-devtools-rerun.md:104` record earlier inventories.
- `docs/style_production.md:172–173` records earlier ordering results. Generic test input words and the requirement to define technical terms can also match.
