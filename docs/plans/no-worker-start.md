# No worker starts in no-worker mode

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** While no-worker mode is on, no dispatch path starts a worker. The refill of a style plan with `--session-dir` sends no assignment and tells the director to do the ticket directly.

> **As-built disposition: amend** — `docs/as-built/shutdown-and-restart.md`

The showrunner's order on 2026-11-14 09:0x EST (the user saw the owner first): "Fix at the dispatch: assign.sh restore_ticket_worker, and every path that starts a worker, checks the worker_capacity no-worker mode and does not start a worker while it is on. Do not fix it only in style_plan.py. Test: a failing-first test that a refill with --session-dir in no-worker mode starts no worker."

## Facts measured on natedev, 09:05 EST

- The defect: on 2026-11-13 23:5x, `style_plan.py next-phase --session-dir` called `restore_ticket_worker`. It ran `assign.sh --role implement --new-worker style-ticket-1467` and started a Claude worker on claude2 while no-worker mode was on.
- `quota_alert.no_worker_mode()` reads the saved flag from the quota state under the current `XDG_STATE_HOME`. A missing or unreadable state reads as off.
- Only `worker.restore` reads the flag today, and it holds only Codex workers. `assign.py`, `worker.py add`, `worker.py assign` and `worker_panes.py start-or-resume` do not read it.
- `quota_alert` adds about 42 ms to an `assign.py` process that already imports its other modules.

## Delegation Context

- **Project:** the `~/.claude` scripts.
- **Project started:** 2026-11-14T14:04:57.929+00:00
- **Stack:** Python 3.13, basedpyright, `unittest`.
- **Layout:**
  - `scripts/direct/assign.py` — `main()` is the one launcher for worker assignments.
  - `scripts/production/worker.py` — `main()` runs `add`, `assign` and `restore`.
  - `scripts/herdr/worker_panes.py` — `main()` runs `start-or-resume`.
  - `scripts/style/style_plan.py` — `restore_ticket_worker()` submits a ticket's dispatch during a refill.
  - `scripts/whoami/quota_alert.py` — `no_worker_mode()`.
- **Test lane:** `scripts.style.test_style_plan_checkpoint_refill`, `scripts.direct.test_assign_gate`, `scripts.production.test_worker`, `scripts.herdr.test_worker_panes`, `scripts.whoami.test_quota_alert`.
- **Build:** `basedpyright` on each changed Python file must read `0 errors, 0 warnings, 0 notes`.
- **Invariants:**
  - No test reads the live quota state.
  - Work that started before the mode keeps its current rules: `worker.restore` and account moves do not change.

## Phases

### Phase 1 — No worker starts in no-worker mode  · status: done

Checkpoint 30678657892f138a96b97fe6b7ffe15378ca6d5b.
- `scripts/whoami/worker_mode.py` holds `state_path()`, `no_worker_mode()` and `NO_WORKER_START`; `quota_alert` imports `state_path` from it.
- `assign.py`, `worker.py add`, `worker.py assign` and `worker_panes.py start-or-resume` refuse with exit 2 in the mode.
- `restore_ticket_worker` sends no assignment in the mode and prints `<slot>: <refusal text>`.
- `worker.restore` and account moves do not change.
