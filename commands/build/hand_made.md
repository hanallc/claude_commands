---
description: Check a hand command with a model, then run it through the queue.
---

Use this command for a long build or test command that you run by hand, and for a trial of a new feature. Do not start such a command directly.

Run `~/.claude/scripts/lib/py ~/.claude/scripts/production/hand_made.py --why '<why>' --machine natedev [--machine mac] [--repo <checkout>] -- <command...>`.

- `--why` is required. It goes into the record.
- `--machine` is required. Give it once for each machine that can take the run. This phase accepts `natedev` and `mac`; another name is refused.
- `--repo` is the checkout that runs the command. It must be the top folder of a git work tree. The default is the current folder.

The command does four steps:

1. A model reads the command and its program files, and gives yes or no with one reason. The program files come from each command word: the file in the checkout that the word names, and the file that the word starts from PATH when it is in the checkout or outside the system folders. Outside the system folders, the first word gives a `#!` script from its first match on PATH, and a later word gives each file that is not compiled (ELF) from each match on PATH and from its path when it has a slash. The run starts the first word by the path of its first match, so no later match on PATH runs. The program gets that path as its `argv[0]`. The model gives no for a program file that it did not read. These forms are refused before the model call:
   - `python -m` and `uv run -m`: run the module file by its path. Put `--` before a `uv run` command that follows a uv option.
   - `env -C`, `env --chdir`, `env -S`, `uv --directory` and `uv --project`: give the checkout with `--repo`, and give the command words one by one.
   - `env NAME=VALUE`: set the variable outside the hand made command.
   - An empty entry, a relative entry or an entry in the checkout in `PATH`, `LD_LIBRARY_PATH`, `PKG_CONFIG_PATH`, `LIBCLANG_PATH` or `SCCACHE_DIR`: tools then load files from the checkout that the check does not read. Remove the entry.
2. A yes sends one placement request with a place in the line of each named machine. The first free machine takes the whole run. A held natedev, or a Mac that is off or full, takes no run. The Builds page shows the run as `hand made`.
3. At run start, the command reads each checked file again on natedev and finds the program files again, also for a run that goes to the Mac. A changed, removed or new program file ends the attempt with no run.
4. Each attempt writes one `hand_made` row in the build log, also for a no or a refusal. The row names the machine that took the run and each machine that you named.

On the Mac:

- Before the model call, the command finds each named machine that cannot take the run. That machine stays out of the placement; with no machine left, the attempt ends. The Mac cannot take a run when:
  - the checkout has no repository name for a Mac mirror;
  - a checked file is outside the checkout;
  - the copy to the Mac leaves out a checked file (`.git`, `target` or a git-ignored file);
  - a command word names the natedev checkout path;
  - the first word has a slash and is not a checked file in the checkout or a program in a Mac system folder.
- The command copies the checkout to the Mac mirror of its repository, as offload does, with the offload SSH options, and holds the mirror lease until the run ends. The copy leaves out `.git`, `target` and each git-ignored file.
- In the same SSH command, before the start, the Mac runs only a file that natedev read, or a program in a system folder. A word that fails one of these checks ends the attempt with no run, and the refusal names the word:
  - each checked file in the mirror has the checked sha256;
  - another word with a slash names no path in the mirror or on the Mac, unless it is a program in a system folder;
  - another word with no slash names no file in the mirror;
  - a word with no slash has no match on the program `PATH`, or each match is in a system folder or is a compiled Mac file (Mach-O or universal); a wrapper goes on to a later match when a start fails;
  - a later word names no folder with a `__main__.py` in the mirror.
- The Mac shell expands no word that starts with `=`.
- The system folders are `/usr/bin`, `/bin`, `/usr/sbin`, `/sbin`, `/run/current-system/sw/bin` and `/nix/var/nix/profiles/default/bin`. The program `PATH` is the system folders, then `~/.cargo/bin`. A first word with no slash starts only from the program `PATH`.
- The program gets the Mac values of the natedev variable list below, the program `PATH`, and `MAC_PLACEMENT`. It gets no other variable.
- The log and you get the Mac stderr with the stdout.
- A Mac block of another session stops the Mac run before it starts, as for each other Mac kind.
- When the Mac fails before the program starts (a block, a failed copy or a missing mirror folder), the run goes back to the lines of the other named machines. The row names each failed machine in `failed_machines`. With no other named machine, the attempt ends with no run. A failed Mac takes no new run for 180 seconds.
- When the program started and the Mac gave no exit status, the attempt ends; the run does not go to another machine.
- When the Mac work cannot be proved stopped, the attempt ends, and the queue keeps the Mac place until the attempt process ends.

The program gets no input: its standard input is `/dev/null`. On natedev, the program gets only these variables: `HOME`, `USER`, `LOGNAME`, `PATH`, `LANG`, `LANGUAGE`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TZ`, `TMPDIR`, `XDG_RUNTIME_DIR`, `SHELL`, `NIX_PATH`, `LOCALE_ARCHIVE`, `SSL_CERT_FILE` and `NIX_SSL_CERT_FILE`. It also gets each of these variables that is set, and the model sees each one with its value: `PKG_CONFIG_PATH`, `LIBCLANG_PATH`, `LD_LIBRARY_PATH`, `BINDGEN_EXTRA_CLANG_ARGS`, `SCCACHE_DIR`, `RUSTC_WRAPPER`, `CARGO_TARGET_DIR`, `CC`, `CXX`, `NIX_CFLAGS_COMPILE` and `NIX_LDFLAGS`. The run removes every other variable, because a variable can make the program load code that the check does not read. The queue place then adds its own reservation variable.

Before the verdict, the command runs no program that the checkout names. Its git calls turn off the fsmonitor hook and each filter driver, do not read submodule work trees and fetch no missing object. It does not run `rustc`, so a `rust-toolchain.toml` in the checkout starts nothing.

The output of a run goes to the log of the row first, then to you, as it runs. When your output closes, the run continues and the log keeps all the output. The run ends when the program exits, also when a child of the program keeps the output open.

The exit status is the status of the command, or 2 for an attempt that ended with no program status; the last line names the cause. A program can also exit with 2, so the row tells a refusal from a program exit: a refusal has a `refusal` text and no `status`. An error after the program exits, such as the end of the queue place, goes to stderr, and the attempt keeps the program status.

After a no, change the command or the script, then start a new attempt. A check with no answer (timeout, model exit, bad JSON, schema failure, a program file that cannot be read, or program files over the size limit together) also ends the attempt with no run.

The check settings are in the `[cli.hand_made]` section of `~/.claude/config/models.conf`.
