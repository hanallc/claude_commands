---
description: Check a hand command with a model, then run it through the queue.
---

Use this command for a long build or test command that you run by hand, and for a trial of a new feature. Do not start such a command directly.

Run `~/.claude/scripts/lib/py ~/.claude/scripts/production/hand_made.py --why '<why>' --machine <natedev|mac|mac-linux> [--machine ...] [--repo <checkout>] -- <command...>`.

- `--why` is required. It goes into the record.
- `--machine` is required. Give it once for each machine that can take the run. This phase accepts `natedev`, `mac` and `mac-linux` (the Mac Linux guest); another name is refused.
- `--repo` is the checkout that runs the command. It must be the top folder of a git work tree. The default is the current folder.

The command does four steps:

1. A model reads the command and its program files, and gives yes or no with one reason. The program files come from each command word: the file that the word names as a path, and the file that the word starts from PATH. A file in the system folders is not a program file. Outside the checkout and the system folders, the first word gives only a `#!` script, from its path or from its first match on PATH. A later word gives each file that is not compiled (ELF), from its path or from each match on PATH, also a file with no execute bit. The run starts the first word by the path of its first match, so no later match on PATH runs. The program gets that path as its `argv[0]`. The model gives no for a program file that it did not read. These forms are refused before the model call:
   - `python -m` and `uv run -m`: run the module file by its path. Put `--` before a `uv run` command that follows a uv option.
   - `env -C`, `env --chdir`, `env -S`, `uv --directory` and `uv --project`: give the checkout with `--repo`, and give the command words one by one.
   - `env NAME=VALUE`: set the variable outside the hand made command.
   - A later word that names a folder with a `__main__` entry, such as `__main__.py` or `__main__.pyc`, or a folder that cannot be listed: python runs that file, and the check cannot read it. Run the program file by its path.
   - An empty entry, a relative entry or an entry in the checkout in `PATH`, `LD_LIBRARY_PATH`, `PKG_CONFIG_PATH`, `LIBCLANG_PATH` or `SCCACHE_DIR`: tools then load files from the checkout that the check does not read. Remove the entry.
2. A yes sends one placement request with a place in the line of each named machine. The first free machine takes the whole run. A held natedev, a Mac that is off or full, or a busy guest takes no run, and the run waits in its line. The Builds page shows the run as `hand made`.
3. At run start, the command reads each checked file again on natedev and finds the program files again, also for a run that goes to the Mac or the guest. A changed, removed or new program file ends the attempt with no run.
4. Each attempt writes one `hand_made` row in the build log, also for a no or a refusal. The row names the machine that took the run and each machine that you named.

On each machine:

- Before the model call, the command finds each named machine that cannot take the run. The parts below give the causes. That machine stays out of the model call and the placement. With no machine left, the attempt ends with no run.
- When a machine fails before the program starts, the run goes back to the lines of the other named machines. The row names each failed machine in `failed_machines`. With no other named machine, the attempt ends with no run.

On the Mac:

- The Mac cannot take a run when:
  - the checkout has no repository name for a Mac mirror;
  - a command word has a `..` part, also after the `=` of an option value: the copy sends each link of the checkout as a link, so on the Mac the word can name another file;
  - a checked file is outside the checkout, or its path has a link in a part;
  - the copy to the Mac leaves out a checked file (`.git`, `target` or a git-ignored file);
  - a command word names the natedev checkout path;
  - the first word starts with `-` or has an `=`;
  - the first word has a slash and is not a checked file in the checkout or a program in a Mac system folder.
- The command copies the checkout to the Mac mirror of its repository, as offload does, with the offload SSH options, and holds the mirror lease until the run ends. The copy leaves out `.git`, `target` and each git-ignored file.
- In the same SSH command, before the start, the Mac runs only a file that natedev read, or a program in a system folder. A word that fails one of these checks ends the attempt with no run, and the refusal names the word:
  - each checked file in the mirror has the checked sha256;
  - another word with a slash names no path in the mirror or on the Mac, unless it is a program in a system folder;
  - another word with no slash names no file in the mirror;
  - for a word with no slash, each file with that name in a folder of the program `PATH` is in a system folder or is a compiled Mac file (Mach-O or universal). This counts each file, also a file with no execute bit: a wrapper goes on to a later match when a start fails, and bash opens a script on `PATH` with no execute bit;
  - a later word names no folder with a `__main__` entry of any suffix, such as `__main__.py` or `__main__.pyc`, and no folder that cannot be listed.
- The Mac shell expands no word that starts with `=`.
- The system folders are `/usr/bin`, `/bin`, `/usr/sbin`, `/sbin`, `/run/current-system/sw/bin` and `/nix/var/nix/profiles/default/bin`. The program `PATH` is the system folders, then `~/.cargo/bin`. A first word with no slash starts only from the program `PATH`.
- The program gets the Mac values of the natedev variable list below, the program `PATH`, and `MAC_PLACEMENT`. It gets no other variable.
- The log and you get the Mac stderr with the stdout.
- A Mac block of another session stops the Mac run before it starts, as for each other Mac kind.
- The Mac fails before the start, and the run goes back, after a block, a failed copy, or a wrapper exit status with no start line, for example from a missing mirror folder.
- When the connection ends before a start line or the wrapper exit status comes, the start is unknown. The attempt ends with an error, and the run does not go to another machine.
- When the program started and the Mac gave no exit status, the attempt ends; the run does not go to another machine.
- When the Mac work cannot be proved stopped, the attempt ends, and the queue keeps the Mac place until the attempt process ends.

On the Mac Linux guest:

- The run comes to the guest through the `mac-linux` queue line, the line of a gate run on the guest. The guest takes one run at a time.
- A guest that is off (`enabled.mac-linux=off` in `build_targets.conf`) cannot take the run.
- A guest in retry backoff can take the run. Its line waits for the backoff end, and another named machine can take the run in that time.
- The guest also cannot take the run when:
  - the checkout is of a repository other than claude, or the run has no owner unit;
  - a command word is an absolute path: on the guest, it names a guest file. Give a path in the checkout or a program name;
  - a checked file is outside the checkout;
  - a checked file has a symbolic link or `..` in its path in the checkout. Give the real path.
- The run holds the Mac repository claim, as a gate run on the guest does.
- natedev makes one commit of the checkout, with the dirty files. Each checked program file must be in that commit with the checked text.
- The guest runs the command words as given, in its numbered copy of that commit, in its sandbox. It finds the first word in the copy or on its own PATH.
- Before it starts the program, the guest checks each command word, so that it runs only a file that natedev read or a tool. A word that fails stops the run before the start, and the cause names the word and the reason:
  - a later word must name no folder with a `__main__` file, such as `__main__.py` or `__main__.pyc`, because python runs that file. An example is `python3 target`;
  - a word with a file that natedev read runs that checked file;
  - another word with a slash must have no `..` and no symbolic link in its path, and must name no guest file. A relative path starts at the copy. An example is a file that an earlier run left in `target/`;
  - another later word with no slash must name no guest file in the copy. A plain folder passes, so `cargo test` runs with a `test` folder;
  - for a word with no slash, each match on the guest PATH must be in a system folder, or be a compiled file outside the copy. For a later word, a readable match with no execute bit counts too, because bash finds a script name on PATH by read access.
- The guest gives the program its own variables. The natedev variable list below does not apply.
- The guest fails before the start, and the run goes back, after a retry backoff that starts after the placement, a refused Mac repository claim, an SSH failure before natedev sends the command, a checked file that is not in the commit with the checked text (a file that changed after the check, or a file that git ignores), a busy guest run lock, or a word that the guest refuses.
- After natedev sends the command, only a busy guest run lock or a refused word proves that the guest started nothing. Each other stop before the start line, such as a lost SSH link or a bundle that the guest cannot use, makes the start unknown. Then the attempt ends with no run, and the run does not go to another machine, because the program can run there too. The refusal names `mac-linux start unknown`.
- A failure after the program starts, such as a lost SSH link or the guest time limit, is the program status, and the output gives the cause.
- The run holds the guest run lock. After the run, after a failed start and after a start unknown, natedev waits for the guest to prove that the run ended. A guest that took no run lock started nothing and needs no proof. If the guest gives no proof within `unreachable_wait_seconds`, the run does not go to another machine, and the queue keeps the guest place until the attempt process ends. After the program status, the attempt keeps the status and stderr names the guest. After a failed start, the refusal names the first cause and the missing proof. After a start unknown, the refusal names the cause, and stderr names the missing proof.
- The row names `mac-linux` as the machine. It has no memory peak.

The program gets no input: its standard input is `/dev/null`. On natedev, the program gets only these variables: `HOME`, `USER`, `LOGNAME`, `PATH`, `LANG`, `LANGUAGE`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TZ`, `TMPDIR`, `XDG_RUNTIME_DIR`, `SHELL`, `NIX_PATH`, `LOCALE_ARCHIVE`, `SSL_CERT_FILE` and `NIX_SSL_CERT_FILE`. It also gets each of these variables that is set, and when natedev is a named machine, the model sees each one with its value: `PKG_CONFIG_PATH`, `LIBCLANG_PATH`, `LD_LIBRARY_PATH`, `BINDGEN_EXTRA_CLANG_ARGS`, `SCCACHE_DIR`, `RUSTC_WRAPPER`, `CARGO_TARGET_DIR`, `CC`, `CXX`, `NIX_CFLAGS_COMPILE` and `NIX_LDFLAGS`. The run removes every other variable, because a variable can make the program load code that the check does not read. The queue place then adds its own reservation variable.

Before the verdict, the command runs no program that the checkout names. Its git calls turn off the fsmonitor hook and each filter driver, do not read submodule work trees and fetch no missing object. It does not run `rustc`, so a `rust-toolchain.toml` in the checkout starts nothing.

The output of a run goes to the log of the row first, then to you, as it runs. When your output closes, the run continues and the log keeps all the output. The run ends when the program exits, also when a child of the program keeps the output open.

The exit status is the status of the command, or 2 for an attempt that ended with no program status; the last line names the cause. A program can also exit with 2, so the row tells a refusal from a program exit: a refusal has a `refusal` text and no `status`. An error after the program exits, such as the end of the queue place, goes to stderr, and the attempt keeps the program status.

After a no, change the command or the script, then start a new attempt. A check with no answer (timeout, model exit, bad JSON, schema failure, a program file that cannot be read, or program files over the size limit together) also ends the attempt with no run.

Known gap, open until its fix is on main: the check reads no file for a command word that names a folder, but a tool can run a file from that folder, for example `make -C target` runs `target/Makefile` and `node target` runs `target/index.js`.

The check settings are in the `[cli.hand_made]` section of `~/.claude/config/models.conf`.
