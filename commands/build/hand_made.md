---
description: Check a hand command with a model, then run it through the queue.
---

Use this command for a long build or test command that you run by hand, and for a trial of a new feature. Do not start such a command directly.

Run `~/.claude/scripts/lib/py ~/.claude/scripts/production/hand_made.py --why '<why>' --machine <natedev|mac|mac-linux> [--machine ...] [--repo <checkout>] -- <command...>`.

- `--why` is required. It goes into the record.
- `--machine` is required. Give it once for each machine that can take the run. This phase accepts `natedev`, `mac` and `mac-linux` (the Mac Linux guest); another name is refused.
- `--repo` is the checkout that runs the command. It must be the top folder of a git work tree. The default is the current folder.

The command does four steps:

1. A model reads the command and its program files, and gives yes or no with one reason. The program files come from each command word: the file that the word names as a path, and the file that the word starts from PATH. A file in the system folders is not a program file. When the first word is `cargo` and its first match on PATH is the build-hold cargo wrapper of the checker repository (the repository of the `hand_made.py` that runs, `~/.claude`), outside the checkout, the check and the run take the next match that is not the wrapper. The wrapper does the same in the `builds.slice` scope of the run. The first word rule below applies to that match, and the run starts it by its path, so the wrapper does not run. The run does the wrapper folder steps itself (see the read limit below). These cases keep the wrapper as the start file:
   - no next match on PATH: the wrapper is a program file, and the model reads it;
   - a wrapper of another checkout, such as a unit worktree: the wrapper is a program file, and the model reads it;
   - a later word `clean`: the real `cargo clean` can remove the assigned build folder, and the wrapper clean rule needs files that the read limit hides. The attempt ends before the model call with: "a hand made run cannot clean the build output: the build-hold cargo wrapper cleans it with files that the read limit hides; run a normal cargo clean outside hand made";
   - a `--repo ~/.claude` run: the wrapper is in the checkout, which the run writes, so the attempt ends before the model call with the write path refusal below.

   A later word `cargo`, as in `timeout 600 cargo build`, reads the wrapper and each later match that is a program file. Outside the checkout and the system folders, the first word gives only a `#!` script, from its path or from its first match on PATH. A later word gives each file that is not compiled (ELF), from its path or from each match on PATH, also a file with no execute bit. A later word gives the same files for each of its values (see the folder word item below), so `make -f/x/evil.mk` and `make --file=/x/evil.mk` read `/x/evil.mk`. The run starts the first word by the path of its first match (for `cargo`, the start file above), so no later match on PATH runs. The program gets that path as its `argv[0]`. The model gives no for a program file that it did not read. These forms are refused before the model call:
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
     - So `cargo build --target-dir newdir` passes. When git ignores `target`, `cargo build --target-dir target` and `cargo build --target-dir target/new` are refused. `cargo build --config build.target-dir=/tmp/x` is refused. natedev also takes no `--target-dir` other than its one output folder (see the read limit below).
     - A pattern word that is also the name of an ignored folder, as in `rg target src`, is refused. Write the pattern in another form, for example `targe[t]`.
     - Name a checkout file, a tracked folder, a folder in `/nix/store` or a new entry in a tracked folder, or leave out the option so the tool uses its default.
   - An empty entry, a relative entry or an entry in the checkout in `PATH`, `LD_LIBRARY_PATH`, `PKG_CONFIG_PATH`, `LIBCLANG_PATH` or `SCCACHE_DIR`: tools then load files from the checkout that the check does not read. Remove the entry.
   - A `PATH` folder, a start file that the first word finds on `PATH`, a start file named with a slash that is not a checkout file, or a `PATH` match of a later word, whose real path is in a path that the run writes on natedev: the checkout, `CARGO_HOME/registry`, `CARGO_HOME/git`, `SCCACHE_DIR` or the cargo build output folder. A run can put a compiled file there, and a later run starts it with no check. The refusal says: "the real path of <path> is in <bind>, which the run writes, so a run can change a program that a later run starts; use a program and a PATH folder outside the checkout, the build output folder, SCCACHE_DIR and the cargo registry and git folders".
2. A yes sends one placement request with a place in the line of each named machine. The first free machine takes the whole run. A held natedev, a Mac that is off or full, or a busy guest takes no run, and the run waits in its line. The Builds page shows the run as `hand made`.
3. At run start, the command reads each checked file again on natedev, finds the program files and the start file again with the same rule and checks the `__main__` and folder words again, also for a run that goes to the Mac or the guest. A changed start file, a changed, removed or new program file, or a folder word that the check now refuses, ends the attempt with no run. The start file refusal says: "the start file changed after the check: <old> -> <new or none>".
4. Each attempt writes one `hand_made` row in the build log, also for a no or a refusal. The row names the machine that took the run and each machine that you named.

On each machine:

- Before the model call, the command finds each named machine that cannot take the run. The parts below give the causes. That machine stays out of the model call and the placement. With no machine left, the attempt ends with no run.
- When a machine fails before the program starts, the run goes back to the lines of the other named machines. The row names each failed machine in `failed_machines`. With no other named machine, the attempt ends with no run.
- A command word that the Mac or the guest refuses at the start ends the attempt with no run, and the refusal names the word. The run does not go to another machine.

On natedev (Read limit):

- `bwrap` starts the program with a read limit. A path that the limit does not bind does not exist for the program, so no command word can make a tool read it. The root of the limit is read only, so a write to a path that is not bound fails.
- The program reads: `/nix/store`, `/run/current-system`, `/etc`, `/bin`, `/usr`, the rustup folder (`RUSTUP_HOME`, else `~/.rustup`), `CARGO_HOME/bin` and `CARGO_HOME/config.toml` (`CARGO_HOME` is else `~/.cargo`), and each `PATH` folder that is not in a system folder and holds no home folder, temp folder or checkout. The limit does not bind the build-hold cargo wrapper folder or its `lib` folder. A wrapper that the run starts, for example from a later word `cargo`, is a checked file, so it stays readable at its real path. In the `builds.slice` scope of the run, it starts the next `cargo` at once. Its clean rule runs before that and needs files that the limit does not bind, so a run with the installed wrapper as the start file and a later word `clean` is refused before the model call. A run with `DETACHED_RUN_SLICE` set has no `builds.slice` scope. For a build subcommand, such as `build`, `test` or `check`, the wrapper then runs files that the limit does not bind, so the run fails. Another subcommand starts the next `cargo` at once. Each file that the check read, and the file that the run starts, stays readable at its real path; its folder stays hidden.
- Each entry under `/etc`, `/bin` and `/usr` that root does not own is hidden: an empty folder replaces a folder, and `/dev/null` replaces a file.
- The limit does not bind a path that holds the home folder, and it does not bind a path that has a symbolic link in it. The one exception is a read only path whose link goes into `/nix/store`, such as `~/.cargo/config.toml`.
- The program reads and writes: the checkout, `CARGO_HOME/registry`, `CARGO_HOME/git`, the cargo lock files `.package-cache`, `.package-cache-mutate` and `.global-cache` in `CARGO_HOME`, `SCCACHE_DIR` (else `~/.cache/sccache`), and the cargo build output folder.
- The run has one cargo build output folder: `CARGO_TARGET_DIR` when it is set, else the checkout `target`. natedev cannot take a cargo word that names another output folder, such as `--target-dir x` or a `build.target-dir` value in a `--config` word. Only the words of a `cargo` command before its first `--` are checked, so `rg target-dir src` passes. The refusal says: "the run builds in <place>; leave out --target-dir, or give that folder".
- A checkout `target` link is bound only when it names the build folder that the build folder model gives for this checkout (`scripts/build_hold/cargo_folder.py` `assigned_output`). The run then builds in that folder.
- For a `cargo` run, natedev does the three folder steps of the build-hold cargo wrapper with the wrapper code (`cargo_folder.checked_step`), at the attempt start and again before the program starts. The limit checks the output folder after the steps, so a stale `target` link, or a link to the assigned build folder when that folder is missing, gets the repair and the run goes on. The lock of the attempt start check ends with that check. It makes or checks the checkout `target` link to the assigned build folder, holds the build folder lock until the program ends, and writes the use stamp `.lint-sweep-used`, so the disk sweeper keeps the folder. The program gets the lock descriptor. The check reads the cargo folder variables of the program environment.
- The run does the steps only when the wrapper does them (`cargo_folder.folder_step_due`): for a build subcommand such as `build`, `test` or `check`, and not for `install`, a `-h`, `--help`, `-V` or `--version` word, or when `CARGO_FOLDER_CHECKED=1` or `BUILDLOG_STEP_SCOPE` is in the environment of `hand_made.py`. The build subcommands are one list in the wrapper, and `cargo_folder.py` reads that list.
- `folders.check` in `config/build_folders.conf` sets the check mode. With `report`, the live value, a path outside the build folders, such as an unassigned checkout or a `CARGO_TARGET_DIR` outside the build folder, gives the stderr note `cargo: <reason>`, and the run goes on. An unassigned checkout then holds no lock. With `enforce`, it is a refusal. Each other error of the check is a refusal in both modes. A refusal ends the attempt with no run and says: "the build-hold cargo folder check refused the run, so the program did not start: <reason>".
- After the folder steps, the run does not start when the output folder holds the home folder, has a symbolic link in its path, or is a `target` link to another folder. A link to the build folder of another checkout is refused when no folder step repairs it, for example for a program that is not `cargo`, because a build script that the run puts there runs in the next normal build of that checkout. The refusal says: "the run cannot build in <place>, because <reason>; <advice>". With `CARGO_TARGET_DIR` set, the advice is "leave out CARGO_TARGET_DIR". Else it is "a cargo build in a checkout that has a build folder makes the target link to that folder; make such a checkout with worktrees.py make <repo> <unit> <short-name>".
- The program sees the attempt temp folder as `/tmp`, and `TMPDIR` is `/tmp`. The temp folder is `tmp` in the queue place folder, so the end of the place removes it.
- `SCCACHE_SERVER_UDS` names a socket in the temp folder, so the sccache client starts a private sccache server in the limit. The cache folder is the same, so cache hits stay. The private sccache server counts in the program scope memory and ends with the run.
- A hand made run has no network and no display. A run that needs the network or a display is not a hand made run. The program has its own loopback only, so it reaches no socket or port of another process.
- `CARGO_NET_OFFLINE` is `true`. When the program is `cargo`, its status is not 0, and the last 64 KiB of its output has a cargo offline text, the attempt ends as a refusal with no give back. The refusal comes after the program output and says: "a hand made run has no network, so cargo cannot fetch a dependency; a normal run of this line must fetch the dependency first". The output passes at once and unchanged.
- natedev refuses a run when a path that it writes holds, or is in, a tool folder. The paths that it writes are the checkout, the cargo folders, `SCCACHE_DIR` and the output folder. The run could change the check of the next run through such a path. The refusal names the path: "the run writes <path>, which holds or is in the tools that check the run; use a worktree outside those tools, leave out CARGO_TARGET_DIR, and give SCCACHE_DIR a folder outside those tools". A worktree of the same repository passes. The tool folders are:
  - the repository of the checker code;
  - the config folder;
  - the python path cache folder that `scripts/lib/py --cache-folder` gives;
  - the build-hold cargo wrapper folder and its `lib` folder, only when the first `cargo` on `PATH` is the wrapper.
- `~/.ssh`, the ssh agent socket and `~/.config/git` are hidden.
- The rest of the home folder, other repositories, `/home`, `/run/user`, `/build` (except the folders above) and the `/tmp` of other processes are hidden.
- When `bwrap` is not on `PATH`, or it cannot set up the limit, the program does not start, and the attempt ends with a refusal that names `bwrap`. The run never starts without the limit.

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
- The guest fails before the start, and the run goes back, after a retry backoff that starts after the placement, a refused Mac repository claim, an SSH failure before natedev sends the command, a checked file that is not in the commit with the checked text (a file that changed after the check, or a file that git ignores), or a busy guest run lock.
- A word that the guest refuses ends the attempt with no run, as on the Mac. The refusal names `mac-linux refused the run` and the word.
- After natedev sends the command, only a busy guest run lock or a refused word proves that the guest started nothing. Each other stop before the start line, such as a lost SSH link or a bundle that the guest cannot use, makes the start unknown. Then the attempt ends with no run, and the run does not go to another machine, because the program can run there too. The refusal names `mac-linux start unknown`.
- A failure after the program starts, such as a lost SSH link or the guest time limit, is the program status, and the output gives the cause.
- The run holds the guest run lock. After the run, after a failed start and after a start unknown, natedev waits for the guest to prove that the run ended. A guest that took no run lock started nothing and needs no proof. If the guest gives no proof within `unreachable_wait_seconds`, the run does not go to another machine, and the queue keeps the guest place until the attempt process ends. After the program status, the attempt keeps the status and stderr names the guest. After a failed start, the refusal names the first cause and the missing proof. After a start unknown, the refusal names the cause, and stderr names the missing proof.
- The row names `mac-linux` as the machine. It has no memory peak.

The program gets no input: its standard input is `/dev/null`. On natedev, the program gets only these variables: `HOME`, `USER`, `LOGNAME`, `PATH`, `LANG`, `LANGUAGE`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TZ`, `TMPDIR`, `XDG_RUNTIME_DIR`, `SHELL`, `NIX_PATH`, `LOCALE_ARCHIVE`, `SSL_CERT_FILE` and `NIX_SSL_CERT_FILE`. It also gets each of these variables that is set, and when natedev is a named machine, the model sees each one with its value: `PKG_CONFIG_PATH`, `LIBCLANG_PATH`, `LD_LIBRARY_PATH`, `BINDGEN_EXTRA_CLANG_ARGS`, `SCCACHE_DIR`, `RUSTC_WRAPPER`, `CARGO_TARGET_DIR`, `CC`, `CXX`, `NIX_CFLAGS_COMPILE` and `NIX_LDFLAGS`. The run removes every other variable, because a variable can make the program load code that the check does not read. The queue place then adds its own reservation variable. The read limit sets `TMPDIR` to `/tmp` and adds `SCCACHE_SERVER_UDS`, and `bwrap` sets `PWD`.

Before the verdict, the command runs no program that the checkout names. Its git calls turn off the fsmonitor hook and each filter driver, do not read submodule work trees and fetch no missing object. It does not run `rustc`, so a `rust-toolchain.toml` in the checkout starts nothing.

The output of a run goes to the log of the row first, then to you, as it runs. When your output closes, the run continues and the log keeps all the output. The run ends when the program exits, also when a child of the program keeps the output open.

The exit status is the status of the command, or 2 for an attempt that ended with no program status; the last line names the cause. A program can also exit with 2, so the row tells a refusal from a program exit: a refusal has a `refusal` text and no `status`. An error after the program exits, such as the end of the queue place, goes to stderr, and the attempt keeps the program status.

After a no, change the command or the script, then start a new attempt. A check with no answer (timeout, model exit, bad JSON, schema failure, a program file that cannot be read, or program files over the size limit together) also ends the attempt with no run.

Known gap: the check reads command words, not the value rules of each tool, so it cannot close these forms. On natedev, the read limit closes them: the program cannot read a path outside the limit. The Mac and the guest keep these forms until each has its own read limit.

1. A prefix option to an existing file in an outside folder, such as `cc -B/home/u/out/p-`: the tool runs other entries of that folder.
2. A value that the tool resolves against an earlier value, such as `make -C src -C build`.
3. A `/nix/store` folder with a link to a file outside the store.
4. On another machine, a folder that exists there but not on natedev.

The check settings are in the `[cli.hand_made]` section of `~/.claude/config/models.conf`.
