# The unit-board plugin

## What it shows

The `unit-board@nate-local` plugin has no pane and registers no command.
Use `/build:page` for running steps, waiting steps, memory use, and Mac idle alerts.
Use `/build:bump <position|session|folder>` to set a waiting unit's priority.
Use `/build:bump clear [unit]` to remove priority.
Only the user and current, active showrunner sessions may change priority.
Use `build_queue.py [position|session|folder]` to read cards as JSON.
The reader treats `clear` as a filter and sends no priority mutation.
Use Hana console, Accounts to see the accounts board.
`/showrunner:units` prints the Units table through `scripts/production/unit_phases.py`.

## The plugin

`hooks/register.tsx` registers no hook.
The tests prove that startup, commands, and turn completion open no pane and start no work.

Run `command claude plugin test mods/unit-board` for the plugin tests.
Run `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/unit-board` for types.
The type check needs the engine declarations in `.claude-plugin/types/`.
A fresh checkout without those files reports `TS2688`.
