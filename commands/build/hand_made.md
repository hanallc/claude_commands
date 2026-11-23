---
description: Check a hand command with a model, then run it through the queue.
---

Use this command for a long build or test command that you run by hand, and for a trial of a new feature. Do not start such a command directly.

Run `~/.claude/scripts/lib/py ~/.claude/scripts/production/hand_made.py --why '<why>' --machine natedev [--repo <checkout>] -- <command...>`.

- `--why` is required. It goes into the record.
- `--machine` is required. Give it once for each machine that can take the run. This phase accepts only `natedev`; another name is refused.
- `--repo` is the checkout that runs the command. It must be the top folder of a git work tree. The default is the current folder.

The command does four steps:

1. A model reads the command and its program files, and gives yes or no with one reason. The program files come from each command word: the file in the checkout that the word names, each file that is not compiled (ELF) that a later word with a slash names outside the checkout, the same files for each value of a later word (see the folder word item below), so `make -f/x/evil.mk` and `make --file=/x/evil.mk` read `/x/evil.mk`, and the file that the word starts from PATH when it is in the checkout or outside the system folders. Outside the system folders, the first word gives a `#!` script from its first match on PATH, and a later word gives each file that is not compiled (ELF) from each match on PATH. The run starts the first word by the path of its first match, so no later match on PATH runs. The program gets that path as its `argv[0]`. The model gives no for a program file that it did not read. These forms are refused before the model call:
   - `python -m` and `uv run -m`: run the module file by its path. Put `--` before a `uv run` command that follows a uv option.
   - `env -C`, `env --chdir`, `env -S`, `uv --directory` and `uv --project`: give the checkout with `--repo`, and give the command words one by one.
   - `env NAME=VALUE`: set the variable outside the hand made command.
   - A later word that names a folder with a `__main__` entry, such as `__main__.py` or `__main__.pyc`, or a folder that cannot be listed: python runs that file, and the check cannot read it. Run the program file by its path.
   - A value that starts with `@` and names any entry with the rest, as in `cc @/x/rsp`: a response file, a FIFO or a link gives the tool words that the check does not read. Put the words in the command line. Only a value such as `@missing`, that names no entry, passes. A lone `@`, as in `git log @`, is no response file, and git revision words such as `@{u}..` and `HEAD@{1}` pass.
   - A folder word from which a tool can run a file that the check does not read. For example, `make -C target` runs `target/Makefile`. The values of a later word are the word, each part of it between whitespace and the characters `= , : ; " '`, and, for a word with one `-` such as `-Ctarget`, each part after its first two characters that follows only letters and digits, and for each value that starts with `@`, the rest of it. A value that names an existing folder is a folder word. A value that names no entry is a new entry in its parent folder, because a tool can take it as a prefix, as `cc -B/home/u/p-` runs `/home/u/p-as`. A value that names a regular file keeps the file rules, and a value with a missing parent passes. A value that names a FIFO, a socket or a device is refused, because the check reads only regular files and another process can write into a FIFO; `/dev/null` passes, as in `cc -o /dev/null`.
     - A value with a `..` part is refused. `git log HEAD..main` has no `..` part and passes. The check normalizes a value with no `..` part before it reads the entry, so a long run of `./` parts names its folder. A value that the check cannot read, for example a path over the path limit or a link loop, is refused.
     - A folder in the checkout passes when git does not ignore it, it is not in `.git`, its path from the checkout has no link, and no folder on that path has a `.git` entry (file or folder) of another repository, such as a submodule top. The folder gets the trust of the checkout top, so ignored files in it stay allowed, as they are in the top. A path that git cannot check is refused.
     - A folder outside the checkout passes only when its real path is under `/nix/store/`, because the store is read-only and a recursive tool such as `pytest /home` walks each folder under the one it gets.
     - A new entry outside the checkout also passes when its parent folder is root-only: owned by root on each part of its path as written, with no link and no group or other write. A folder above the last part can have group or other write with the sticky bit, as `/nix/store` has. So `cc -I/usr/include` passes when `/usr/include` does not exist, and `/tmp/newdir` and `cc -B/home/u/p-` are refused.
     - So `cargo build --target-dir newdir` passes. When git ignores `target`, `cargo build --target-dir target` and `cargo build --target-dir target/new` are refused. `cargo build --config build.target-dir=/tmp/x` is refused.
     - A pattern word that is also the name of an ignored folder, as in `rg target src`, is refused. Write the pattern in another form, for example `targe[t]`.
     - Name a checkout file, a tracked folder, a folder in `/nix/store` or a new entry in a tracked folder, or leave out the option so the tool uses its default.
   - An empty entry, a relative entry or an entry in the checkout in `PATH`, `LD_LIBRARY_PATH`, `PKG_CONFIG_PATH`, `LIBCLANG_PATH` or `SCCACHE_DIR`: tools then load files from the checkout that the check does not read. Remove the entry.
2. A yes waits for a place in the natedev queue. The Builds page shows the run as `hand made`.
3. At run start, the command reads each checked file again, finds the program files again and checks the `__main__` and folder words again. A changed, removed or new program file, or a folder word that the check now refuses, ends the attempt with no run.
4. Each attempt writes one `hand_made` row in the build log, also for a no or a refusal.

The program gets no input: its standard input is `/dev/null`. The program gets only these variables: `HOME`, `USER`, `LOGNAME`, `PATH`, `LANG`, `LANGUAGE`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TZ`, `TMPDIR`, `XDG_RUNTIME_DIR`, `SHELL`, `NIX_PATH`, `LOCALE_ARCHIVE`, `SSL_CERT_FILE` and `NIX_SSL_CERT_FILE`. It also gets each of these variables that is set, and the model sees each one with its value: `PKG_CONFIG_PATH`, `LIBCLANG_PATH`, `LD_LIBRARY_PATH`, `BINDGEN_EXTRA_CLANG_ARGS`, `SCCACHE_DIR`, `RUSTC_WRAPPER`, `CARGO_TARGET_DIR`, `CC`, `CXX`, `NIX_CFLAGS_COMPILE` and `NIX_LDFLAGS`. The run removes every other variable, because a variable can make the program load code that the check does not read. The queue place then adds its own reservation variable.

Before the verdict, the command runs no program that the checkout names. Its git calls turn off the fsmonitor hook and each filter driver, do not read submodule work trees and fetch no missing object. It does not run `rustc`, so a `rust-toolchain.toml` in the checkout starts nothing.

The output of a run goes to the log of the row first, then to you, as it runs. When your output closes, the run continues and the log keeps all the output. The run ends when the program exits, also when a child of the program keeps the output open.

The exit status is the status of the command, or 2 for an attempt that ended with no program status; the last line names the cause. A program can also exit with 2, so the row tells a refusal from a program exit: a refusal has a `refusal` text and no `status`. An error after the program exits, such as the end of the queue place, goes to stderr, and the attempt keeps the program status.

After a no, change the command or the script, then start a new attempt. A check with no answer (timeout, model exit, bad JSON, schema failure, a program file that cannot be read, or program files over the size limit together) also ends the attempt with no run.

Known gap: the check reads command words, not the value rules of each tool, so it cannot close these forms. A complete fix limits what the run can read. That is a follow-up.

1. A prefix option to an existing file in an outside folder, such as `cc -B/home/u/out/p-`: the tool runs other entries of that folder.
2. A value that the tool resolves against an earlier value, such as `make -C src -C build`.
3. A `/nix/store` folder with a link to a file outside the store.
4. On another machine, a folder that exists there but not on natedev.

The check settings are in the `[cli.hand_made]` section of `~/.claude/config/models.conf`.
