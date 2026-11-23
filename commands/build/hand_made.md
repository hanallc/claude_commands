---
description: Check a hand command with a model, then run it through the queue.
---

Use this command for a long build or test command that you run by hand, and for a trial of a new feature. Do not start such a command directly.

Run `~/.claude/scripts/lib/py ~/.claude/scripts/production/hand_made.py --why '<why>' --machine natedev [--repo <checkout>] -- <command...>`.

- `--why` is required. It goes into the record.
- `--machine` is required. Give it once for each machine that can take the run. This phase accepts only `natedev`; another name is refused.
- `--repo` is the checkout that runs the command. It must be the top folder of a git work tree. The default is the current folder.

The command does four steps:

1. A model reads the command and its program files, and gives yes or no with one reason. The program files come from each command word: the file in the checkout that the word names, and the file that the word starts from PATH when it is in the checkout or outside the system folders. Outside the system folders, the first word gives a `#!` script from its first match on PATH, and a later word gives each file that is not compiled (ELF) from each match on PATH. The run starts the first word by the path of its first match, so no later match on PATH runs. The program gets that path as its `argv[0]`. The model gives no for a program file that it did not read. These forms are refused before the model call:
   - `python -m` and `uv run -m`: run the module file by its path. Put `--` before a `uv run` command that follows a uv option.
   - `env -C`, `env --chdir`, `env -S`, `uv --directory` and `uv --project`: give the checkout with `--repo`, and give the command words one by one.
   - `env NAME=VALUE`: set the variable outside the hand made command.
   - An empty entry, a relative entry or an entry in the checkout in `PATH`, `LD_LIBRARY_PATH`, `PKG_CONFIG_PATH`, `LIBCLANG_PATH` or `SCCACHE_DIR`: tools then load files from the checkout that the check does not read. Remove the entry.
2. A yes waits for a place in the natedev queue. The Builds page shows the run as `hand made`.
3. At run start, the command reads each checked file again and finds the program files again. A changed, removed or new program file ends the attempt with no run.
4. Each attempt writes one `hand_made` row in the build log, also for a no or a refusal.

The program gets no input: its standard input is `/dev/null`. The program gets only these variables: `HOME`, `USER`, `LOGNAME`, `PATH`, `LANG`, `LANGUAGE`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TZ`, `TMPDIR`, `XDG_RUNTIME_DIR`, `SHELL`, `NIX_PATH`, `LOCALE_ARCHIVE`, `SSL_CERT_FILE` and `NIX_SSL_CERT_FILE`. It also gets each of these variables that is set, and the model sees each one with its value: `PKG_CONFIG_PATH`, `LIBCLANG_PATH`, `LD_LIBRARY_PATH`, `BINDGEN_EXTRA_CLANG_ARGS`, `SCCACHE_DIR`, `RUSTC_WRAPPER`, `CARGO_TARGET_DIR`, `CC`, `CXX`, `NIX_CFLAGS_COMPILE` and `NIX_LDFLAGS`. The run removes every other variable, because a variable can make the program load code that the check does not read. The queue place then adds its own reservation variable.

Before the verdict, the command runs no program that the checkout names. Its git calls turn off the fsmonitor hook and each filter driver, do not read submodule work trees and fetch no missing object. It does not run `rustc`, so a `rust-toolchain.toml` in the checkout starts nothing.

The output of a run goes to the log of the row first, then to you, as it runs. When your output closes, the run continues and the log keeps all the output. The run ends when the program exits, also when a child of the program keeps the output open.

The exit status is the status of the command, or 2 for an attempt that ended with no program status; the last line names the cause. A program can also exit with 2, so the row tells a refusal from a program exit: a refusal has a `refusal` text and no `status`. An error after the program exits, such as the end of the queue place, goes to stderr, and the attempt keeps the program status.

After a no, change the command or the script, then start a new attempt. A check with no answer (timeout, model exit, bad JSON, schema failure, a program file that cannot be read, or program files over the size limit together) also ends the attempt with no run.

The check settings are in the `[cli.hand_made]` section of `~/.claude/config/models.conf`.
