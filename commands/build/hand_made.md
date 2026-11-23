---
description: Check a hand command with a model, then run it through the queue.
---

Use this command for a long build or test command that you run by hand, and for a trial of a new feature. Do not start such a command directly.

Run `~/.claude/scripts/lib/py ~/.claude/scripts/production/hand_made.py --why '<why>' --machine <natedev|mac-linux> [--repo <checkout>] -- <command...>`.

- `--why` is required. It goes into the record.
- `--machine` is required. This phase accepts `natedev` and `mac-linux` (the Mac Linux guest). Give one machine for each run; a run that names two machines, or another name, is refused.
- `--repo` is the checkout that runs the command. It must be the top folder of a git work tree. The default is the current folder.

The command does four steps:

1. A model reads the command and its program files, and gives yes or no with one reason. The program files come from each command word: the file that the word names as a path, and the file that the word starts from PATH. A file in the system folders is not a program file. Outside the checkout and the system folders, the first word gives only a `#!` script, from its path or from its first match on PATH. A later word gives each file that is not compiled (ELF), from its path or from each match on PATH. The run starts the first word by the path of its first match, so no later match on PATH runs. The program gets that path as its `argv[0]`. The model gives no for a program file that it did not read. These forms are refused before the model call:
   - `python -m` and `uv run -m`: run the module file by its path. Put `--` before a `uv run` command that follows a uv option.
   - `env -C`, `env --chdir`, `env -S`, `uv --directory` and `uv --project`: give the checkout with `--repo`, and give the command words one by one.
   - `env NAME=VALUE`: set the variable outside the hand made command.
   - A later word that names a folder with a `__main__` entry, such as `__main__.py` or `__main__.pyc`, or a folder that cannot be listed: python runs that file, and the check cannot read it. Run the program file by its path.
   - An empty entry, a relative entry or an entry in the checkout in `PATH`, `LD_LIBRARY_PATH`, `PKG_CONFIG_PATH`, `LIBCLANG_PATH` or `SCCACHE_DIR`: tools then load files from the checkout that the check does not read. Remove the entry.
2. A yes waits for a place on the machine. On natedev, the run waits in the natedev queue, and the Builds page shows it as `hand made`. The guest has no queue line until item 191 B3; see the guest section below.
3. At run start, the command reads each checked file again and finds the program files again. A changed, removed or new program file ends the attempt with no run.
4. Each attempt writes one `hand_made` row in the build log, also for a no or a refusal.

The program gets no input: its standard input is `/dev/null`. The program gets only these variables: `HOME`, `USER`, `LOGNAME`, `PATH`, `LANG`, `LANGUAGE`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TZ`, `TMPDIR`, `XDG_RUNTIME_DIR`, `SHELL`, `NIX_PATH`, `LOCALE_ARCHIVE`, `SSL_CERT_FILE` and `NIX_SSL_CERT_FILE`. It also gets each of these variables that is set, and the model sees each one with its value: `PKG_CONFIG_PATH`, `LIBCLANG_PATH`, `LD_LIBRARY_PATH`, `BINDGEN_EXTRA_CLANG_ARGS`, `SCCACHE_DIR`, `RUSTC_WRAPPER`, `CARGO_TARGET_DIR`, `CC`, `CXX`, `NIX_CFLAGS_COMPILE` and `NIX_LDFLAGS`. The run removes every other variable, because a variable can make the program load code that the check does not read. The queue place then adds its own reservation variable.

Before the verdict, the command runs no program that the checkout names. Its git calls turn off the fsmonitor hook and each filter driver, do not read submodule work trees and fetch no missing object. It does not run `rustc`, so a `rust-toolchain.toml` in the checkout starts nothing.

The output of a run goes to the log of the row first, then to you, as it runs. When your output closes, the run continues and the log keeps all the output. The run ends when the program exits, also when a child of the program keeps the output open.

The exit status is the status of the command, or 2 for an attempt that ended with no program status; the last line names the cause. A program can also exit with 2, so the row tells a refusal from a program exit: a refusal has a `refusal` text and no `status`. An error after the program exits, such as the end of the queue place, goes to stderr, and the attempt keeps the program status.

After a no, change the command or the script, then start a new attempt. A check with no answer (timeout, model exit, bad JSON, schema failure, a program file that cannot be read, or program files over the size limit together) also ends the attempt with no run.

## The Mac Linux guest

With `--machine mac-linux`, natedev checks the files, then sends the run to the guest:

- The guest takes no run when `enabled.mac-linux=off` in `build_targets.conf`, or while it is in retry backoff. The attempt is refused with the cause.
- The run holds the Mac repository claim, as a gate run on the guest does. A refused claim, for example while another run holds it, refuses the attempt with the cause.
- These forms are refused before the model call, because the guest runs its own copy of the checkout:
  - a checkout of a repository other than claude, or a run with no owner unit;
  - an absolute path in the command: on the guest, it names a guest file. Give a path in the checkout or a program name;
  - a program file outside the checkout;
  - a program file with a symbolic link or `..` in its path in the checkout. Give the real path.
- natedev makes one commit of the checkout, with the dirty files. Each checked program file must be in that commit with the checked text. A file that changed after the check, or a file that git ignores, is refused before the guest starts the program.
- The guest runs the command words as given, in its numbered copy of that commit, in its sandbox. It finds the first word in the copy or on its own PATH.
- Before it starts the program, the guest checks each command word, so that it runs only a file that natedev read or a tool:
  - a later word must name no folder with a `__main__` file, such as `__main__.py` or `__main__.pyc`, because python runs that file. An example is `python3 target`;
  - a word with a file that natedev read runs that checked file;
  - another word with a slash must have no `..` and no symbolic link in its path, and must name no guest file. A relative path starts at the copy. An example is a file that an earlier run left in `target/`;
  - another later word with no slash must name no guest file in the copy. A plain folder passes, so `cargo test` runs with a `test` folder;
  - for a word with no slash, each match on the guest PATH must be in a system folder, or be a compiled file outside the copy. For a later word, a readable match with no execute bit counts too, because bash finds a script name on PATH by read access.

  A word that fails is a refusal that names the word and the reason.
- The guest gives the program its own variables. The variable list above applies only to natedev, and the model sees those values only for natedev.
- A failure before the guest starts the program is a refusal with the cause. Examples are an SSH failure, a busy guest run lock and a bundle that the guest cannot use. A failure after the program starts, such as a lost SSH link or the guest time limit, is the program status, and the output gives the cause.
- The run holds the guest run lock. After the run, and after a failed start, natedev waits for the guest to prove that the run ended. A guest that took no run lock, for example because another run holds it, started nothing: the refusal comes at once, with no proof. If the guest gives no proof within `unreachable_wait_seconds`, the attempt keeps the program status and stderr names the guest. A refusal names the first cause and the missing proof.
- The row names `mac-linux` as the machine. It has no memory peak.

The check settings are in the `[cli.hand_made]` section of `~/.claude/config/models.conf`.
