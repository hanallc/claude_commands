# Clone producers and retire showrunners

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `agents clone NAME NEW_NAME` makes a producer clone, retire removes the record, and `agents retire` takes a showrunner with no units.

> **As-built disposition: amend** — docs/as-built/shutdown-and-restart.md, docs/as-built/roles-and-names.md

List items 536, 538, 539 and 540 (nate, 11-27, via morty and sdlc).

## Delegation Context

- **Project:** ~/.claude tooling — the `agents` command (`scripts/agents/shutdown.py`) and its staff record.
- **Stack:** Python 3, basedpyright (zero errors and zero warnings), unittest.
- **Layout:**
  - `scripts/agents/` — `shutdown.py` (the `agents` parser and dispatch), `clone_session.py`, `producer_lifecycle.py`.
  - `scripts/production/staff.py` — staff rows, kinds and the snapshot.
  - `scripts/production/broadcast.py` — role messages (`--producers`).
  - `commands/agents.md`, `commands/producer/retire.md` — command texts.
- **Key files:**
  - `scripts/agents/shutdown.py:24-27` usage text, `:765` clone parser, `:802` retire parser, `:847-850` clone dispatch, `:931-937` retire dispatch, `:2072` `retire_named_unit`.
  - `scripts/agents/clone_session.py:170` `_arguments` (opening prompt), `:184` `_admit_clone` (staff row), `:235` `run`.
  - `scripts/agents/producer_lifecycle.py:346` `retire` (producer retirement).
  - `scripts/agents/test_clone_session.py` — clone tests; every call uses `--as` today.
  - `scripts/agents/test_shutdown_cli.py`, `scripts/agents/test_producer_lifecycle.py` — retire tests.
- **Test lanes:** `scripts/agents/test_*.py`, `scripts/production/test_*.py`.
- **Build:** none.
- **Test:** single test methods by name, each under `systemd-run --user --scope --collect -q -p MemoryMax=2G`. A whole module goes through the queue (`copies.py run … gate.py --affected`).
- **Lint:** basedpyright on the changed files.
- **Invariants:**
  - One way to do a thing: no alias for a removed form.
  - The clone of a session that is not a producer stays as it is today. Add no refusal.
  - Never touch the live staff record or a live session in a test. Use the fixture roots the tests already use.

## Phases

### Phase 1 — `agents clone NAME NEW_NAME`

**Status:** todo

**Work Order:**

1. Parser (`shutdown.py:765`): take the new name as the second positional word. Remove `--as`.
2. Usage text (`shutdown.py:27`): `agents clone NAME NEW_NAME`.
3. `commands/agents.md:21` and `docs/as-built/shutdown-and-restart.md:40`: the new form.
4. Tests: change every `["clone", X, "--as", Y]` call in `test_clone_session.py` to `["clone", X, Y]`. Add one test: `--as` is refused by the parser (exit 2).
5. One commit holds the code, the texts and the tests.

**Done when:** `agents clone alpha alpha-copy` clones; `agents clone alpha --as alpha-copy` exits 2; no text names `--as` for clone.

### Phase 2 — A clone of a producer is a producer

**Status:** todo

**Work Order:**

1. When the source row is a producer, admit the clone as a producer row, not a session row. It has no productions below it: no showrunner or unit reports to it, and it holds no document of its own.
2. Mark the row as a clone of its source (a typed field on the staff row, for example `clone_of: StaffId`). Read old rows without the field as not a clone.
3. Opening prompt (`clone_session.py:170`) for a producer clone: it is a clone of SOURCE; it does not continue the ongoing work of SOURCE; it does new actions that nate gives it. Keep the current prompt for every other source.
4. Role messages reach the clone: `broadcast.py --producers` and `/announce` include it. Add a test.
5. A clone is offered as a producer as each other producer is, New item included (nate, 11-27 09:48). Add a test that the New item producer choices include it.
6. A clone of a session that is not a producer stays as it is today.

**Done when:** a producer clone has a producer row with the clone mark, gets the clone opening, receives `--producers` messages, and is offered as a producer.

### Phase 3 — Retire removes the record

**Status:** todo

nate, 11-27 09:48 (item 540): "we don't need to resue - we can remove that functionality … remove the 38". Conversation files stay on disk for search.

**Work Order:**

1. `agents retire NAME` removes the staff record, the name and the tab, for a producer and for a unit. For a unit it also removes the worktree and the branch. The conversation files stay on disk.
2. Retire of a producer clone asks for no handoff note, because a clone holds no List items; one command removes it (morty, 11-27 09:45). Retire of another producer keeps its handoff checks.
3. Remove the retired state: `RetiredProducer` and its snapshot split (`staff.py:451-570`, `:705-740`, `:1547`, `:2080`), the removed-worktree unit mark as a retired state, `agents resume NAME --retired` (`shutdown.py:24`, `:651`, `:792`, `:938-943`; `stop_resume.py:1708`; `add_unit.py:1370`), and the readers in `roles_audit.py` and `unit_config.py`.
4. Texts: `commands/agents.md:3, 20, 25-26, 104, 118, 126`, `commands/producer/retire.md:14`, `docs/production_format.md:221-222`, as-built `showrunner-automation.md:42-44`, `shutdown-and-restart.md:544`, `herdr-adoption.md:287`, `roles-and-names.md`.
5. Tests: remove or rewrite the `--retired` tests (`test_producer_lifecycle.py:632`, `test_reboot.py:248`, `test_shutdown_cli.py:2145, 2157, 2358, 2437, 2451`, `test_shutdown_status.py:225`, and the roles_audit and unit_config tests). Add tests: retire removes a unit's record, worktree and branch; retire removes a producer's record; old staff files with a retired mark still read.
6. An old record that carries the retired mark: `agents retire NAME` removes it through the same path. After this phase is live, the director runs it for the 38 retired unit records of 11-27 and reports the result.

**Done when:** retire leaves no record, name, tab, worktree or branch; no text or code names `--retired`; the 38 old records are gone.

### Phase 4 — `agents retire` takes a showrunner

**Status:** todo

**Work Order:**

1. Retire dispatch (`shutdown.py:931`): route a showrunner name to a new showrunner path. Today it falls to `retire_named_unit`, which refuses it.
2. Refuse while units report to the showrunner. The refusal names each unit and says to retire or transfer them first. A retired unit (its worktree removed) does not count.
3. Without units: retire it through the Phase 3 path: stop its session, close its tab, and remove its record. Keep its document and merge branch.
4. Texts: `commands/agents.md` retire line and `docs/as-built/shutdown-and-restart.md` name the showrunner case.

**Done when:** `agents retire <showrunner with units>` exits non-zero and names the units; `agents retire <showrunner with none>` retires it. No new restore path for a showrunner.
