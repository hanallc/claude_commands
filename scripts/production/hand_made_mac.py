"""The Mac route of hand made: copy the checkout to its Mac mirror, check the files there, and run the argv.

The copy is the offload mirror sync: copies.remote_rsync into the Mac mirror of the repository, under the
mirror lease, with the offload SSH options and with the git-ignored files left out as offload does. In the
same SSH command, before the start, the Mac applies the parity rule to each word (start_script). The program
gets a clean environment: the Mac values of PROGRAM_VARIABLES, with PATH set to MAC_PROGRAM_FOLDERS.
The run obeys the Mac block like every other Mac kind. It sets no signal handler.
"""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field
import functools
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

from scripts.build_hold.owning_session import owning_session
from scripts.build_queue import admission_protocol as wire
from scripts.mac_test import claim_state, eligibility, floor_claims, mac_test, mac_transport
from scripts.mac_test.mac_transport import MacPlacement, RemoteRun, RemoteState
from scripts.production.hand_made_route import (PROGRAM_VARIABLES, Line, MachineFailed, MachineRun, Output, Placed,
                                                Program, Refused, note)

# Root-owned folders of Mac system programs: a program in them starts with no check.
MAC_SYSTEM_FOLDERS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/run/current-system/sw/bin",
                      "/nix/var/nix/profiles/default/bin")
# The PATH of the Mac program: the system folders, then the rustup folder, where only a compiled program starts.
MAC_PROGRAM_FOLDERS = (*MAC_SYSTEM_FOLDERS, "~/.cargo/bin")
# The Mac command that prints the first four bytes of a file in hex.
MAC_FIRST_BYTES = ("/usr/bin/od", "-An", "-N4", "-tx1")
# The first four bytes of a compiled Mac program: Mach-O 32 and 64 bit in each byte order, and universal.
COMPILED_STARTS = ("feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe")
# The Mac command that prints the sha256 of a file, then the file name.
MAC_SHA256 = ("/usr/bin/shasum", "-a", "256")
# The Mac shell of the start script.
MAC_ZSH = "/bin/zsh"
# The line that the Mac prints just before it starts the program.
START = "hand_made_start"
# The line that the Mac prints when it refuses the run before the start.
REFUSED = "hand_made_refused="
# The last line of the remote wrapper, after a blank line.
EXIT_LINE = re.compile(r"mac_exit=\d+\n")
# The queue ends a place when its holder ends, so a place with live Mac work stays until then.
STILL_LIVE = "the Mac run may still be live; the queue ends the place when this run ends"


def ssh_stream(command: list[str], remote: RemoteRun, ssh: ExitStack, write: Callable[[str], None]) -> int:
    return mac_transport.stream_remote(command, remote, ssh, write=write)


@dataclass(frozen=True)
class MirrorUse:
    """The Mac mirror of the checkout, under its lease."""
    # The mirror folder, relative to the Mac home folder.
    path: str
    # The shell text that runs a command on the Mac under the mirror lease.
    use: Callable[[str], str]


def ignored_paths(repo: Path) -> tuple[str, ...]:
    """The git-ignored paths of the checkout, as offload lists them; a folder ends with a slash."""
    from scripts.mac_test import offload

    ignored = offload.ignored_paths(offload.ProcessController(), repo)
    if isinstance(ignored, offload.IgnoredPathListingFailed):
        raise MachineFailed("git did not list the ignored files of the checkout")
    return ignored


def left_out(name: str, ignored: tuple[str, ...]) -> bool:
    """The copy leaves the checkout file out of the mirror: an rsync exclude of the copy matches it.

    Each exclude is anchored at the checkout; an exclude that ends with a slash matches only a folder.
    """
    from scripts.mac_test import offload

    excludes = (*offload.MIRROR_EXCLUDES.split(), *(f"/{path}" for path in (*floor_claims.MIRROR_METADATA, *ignored)))
    for exclude in excludes:
        path = exclude.strip("/")
        if (name == path and not exclude.endswith("/")) or name.startswith(path + "/"):
            return True
    return False


@contextmanager
def copied_checkout(repo: Path, host: str) -> Generator[MirrorUse]:
    """Copy the checkout into its Mac mirror, as offload does, and hold the mirror lease until the end."""
    from scripts.copies import copies
    from scripts.mac_test import offload

    repository = eligibility.repository_identity(repo)
    if not isinstance(repository, eligibility.KnownRepository):
        raise MachineFailed("the checkout has no repository name for a Mac mirror")
    # Offload leaves the git-ignored files out of the mirror, so its --delete keeps them; a copy of them stays.
    ignored = ignored_paths(repo)
    with ExitStack() as lease:
        try:
            options = offload.ssh_options(eligibility.read_connection_settings())
            mirror = copies.RemoteMirror(copies.source_owner(repo), copies.SelectedRepository(repository.name), host,
                                         offload.remote_mirror(repository), offload.holder_session())
            lease.enter_context(copies.remote_lease(mirror))
            with offload.mirror_exclude_file(ignored) as excludes:
                copy = subprocess.run(copies.remote_rsync(repo, mirror, excludes, options),
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
            if copy.returncode != 0:
                detail = copy.stderr.strip().splitlines()
                raise MachineFailed(f"the copy to the Mac mirror failed: {detail[-1] if detail else copy.returncode}")
            copies.register_remote(mirror)
        except (copies.CopyError, OSError, ValueError) as error:
            raise MachineFailed(f"the copy to the Mac mirror failed: {error}") from error
        yield MirrorUse(mirror.path, functools.partial(copies.remote_use_command, mirror))


@dataclass(frozen=True)
class MacTransport:
    """The SSH calls of the Mac route; tests give their own."""
    stream: Callable[[list[str], RemoteRun, ExitStack, Callable[[str], None]], int] = ssh_stream
    stop: Callable[[MacPlacement, RemoteRun], bool] = mac_transport.stop_remote
    mirror: Callable[[Path, str], AbstractContextManager[MirrorUse]] = copied_checkout


def mac_folder(folder: str) -> str:
    """A Mac folder as shell text: a ~/ folder starts at the Mac home folder."""
    return '"$HOME"/' + shlex.quote(folder[2:]) if folder.startswith("~/") else shlex.quote(folder)


def system_program(word: str) -> bool:
    """The word is an absolute path in a Mac system folder."""
    return os.path.isabs(word) and os.path.dirname(os.path.normpath(word)) in MAC_SYSTEM_FOLDERS


# A .. part of a path in a word, also after an = of an option value.
DOT_DOT = re.compile(r"(?:^|[/=])\.\.(?:/|$)")

def mac_refusal(program: Program) -> str:
    """Why the Mac cannot take the program, or no text when it can. It runs before the model call."""
    repo = program.repo
    if not isinstance(eligibility.repository_identity(repo), eligibility.KnownRepository):
        return "the checkout has no repository name for a Mac mirror"
    # The copy sends a link as a link, and a program applies .. after it follows a link, so a word with .. can
    # name a file on the Mac that is not the file natedev read at the normal path.
    dotted = [word for word in program.argv if DOT_DOT.search(word)]
    if dotted:
        return f"the word {dotted[0]} has a .. part, and the Mac gets each link of the checkout as a link"
    names = [Path(os.path.normpath(path)) for path, _ in program.checked]
    # With no .. part, a path that resolves to its normal path has no link in a part.
    linked = [path for (path, _), name in zip(program.checked, names, strict=True)
              if not (name.is_relative_to(repo) and Path(path).resolve() == name)]
    if linked:
        return (f"{linked[0]} is a checked file outside the checkout or a path with a link in it, and the Mac "
                + "gets only the checkout, with each link as a link")
    if names:
        try:
            ignored = ignored_paths(repo)
        except MachineFailed as error:
            return str(error)
        missing = [name for path in names if left_out(name := path.relative_to(repo).as_posix(), ignored)]
        if missing:
            return f"{missing[0]} is a checked file that the copy to the Mac leaves out"
    named = [word for word in program.argv if str(repo) in word]
    if named:
        return f"the word {named[0]} names the natedev checkout, which the Mac does not check"
    first = program.argv[0]
    if first.startswith("-") or "=" in first:
        return f"the first word {first} cannot start on the Mac"
    if "/" not in first:
        return ""
    if os.path.isabs(first):
        return "" if system_program(first) else f"the first word {first} is in no Mac program folder"
    checked = {os.path.normpath(path) for path, _ in program.checked}
    return "" if os.path.normpath(repo / first) in checked else f"the first word {first} is not a checked file"


def start_script(mirror: str, program: Program, placement: MacPlacement) -> str:
    """The Mac zsh text that applies the parity rule, and starts the program with a clean environment.

    The parity rule: the Mac runs only a file that natedev read, or a program in a system folder. For each word:
    - a checked file has the same sha256 on the Mac;
    - another word with a slash names no path in the mirror, unless it is a system program;
    - another word with no slash names no file in the mirror (a folder such as test stays allowed);
    - a word with no slash names no file in a Mac program folder, or each such file is in a system folder or
      is compiled: a wrapper's execvp goes on to a later match when a start fails, and bash opens a script
      on PATH with no execute bit;
    - a later word names no folder with a __main__ entry of any suffix, which python runs, and no folder that
      cannot be listed, as main_folder in mac_linux.py, which hand_made.py uses.
    zsh expands no word that starts with =. A missing mirror ends the script with no line; a refusal prints
    REFUSED; START comes just before the start.
    """
    def refuse(reason: str) -> str:
        return f"{{ print -r -- {shlex.quote(REFUSED + reason)}; exit 1 }}"

    lines = ["setopt no_equals",
             "cd -- " + mac_folder("~/" + mirror) + " || exit 1",
             "system=(" + shlex.join(MAC_SYSTEM_FOLDERS) + ")",
             "folders=(" + " ".join(map(mac_folder, MAC_PROGRAM_FOLDERS)) + ")",
             "compiled=(" + " ".join(COMPILED_STARTS) + ")",
             'trusted() { (( ${system[(Ie)${1:h}]} )) && return; '
             + f'local start=$({shlex.join(MAC_FIRST_BYTES)} -- "$1" 2>/dev/null); '
             + '(( ${compiled[(Ie)${start//[[:space:]]/}]} )) }']
    checked = {os.path.normpath(path) for path, _ in program.checked}
    later = set(program.argv[1:])
    for path, sha256 in program.checked:
        name = os.path.relpath(path, program.repo)
        lines.append(f"sum=$({shlex.join(MAC_SHA256)} -- {shlex.quote(name)} 2>/dev/null); "
                     + f"[[ ${{sum%% *}} == {sha256} ]] || " + refuse(f"{name} on the Mac is not the checked file"))
    for word in dict.fromkeys(program.argv):
        quoted = shlex.quote(word)
        if os.path.normpath(program.repo / word) not in checked and not system_program(word):
            exists = f"-e {quoted} || -L {quoted}" if "/" in word else f"-f {quoted}"
            lines.append(f"[[ {exists} ]] && " + refuse(f"{word} on the Mac is a file that natedev did not read"))
        if word in later:
            lines.append(f"main=({quoted}/__main__*(N)); "
                         + f"[[ -d {quoted} ]] && {{ [[ ! -r {quoted} ]] || (( $#main )) }} && "
                         + refuse(f"{word} on the Mac is a folder that python can run: it has a __main__ file "
                                  + "or cannot be listed"))
        if "/" not in word:
            lines.append(f'for folder in $folders; do [[ ! -f $folder/{quoted} ]] || trusted $folder/{quoted} || '
                         + refuse(f"{word} on the Mac starts a program that is not compiled and not in a system folder")
                         + "; done")
    first = program.argv[0]
    if "/" in first:
        lines.append(f"start={shlex.quote(first)}")
    else:
        lines.append(f"start=$(path=($folders); whence -p -- {shlex.quote(first)}) || "
                     + refuse(f"{first} is in no Mac program folder"))
    lines += [f"vars=(PATH=${{(j.:.)folders}} MAC_PLACEMENT={shlex.quote(mac_transport.placement_value(placement))})",
              f"for name in {' '.join(sorted(PROGRAM_VARIABLES - {'PATH'}))}; do "
              + '[[ -v $name ]] && vars+=("$name=${(P)name}"); done',
              f"print -r -- {START}",
              'exec /usr/bin/env -i "${vars[@]}" "$start" ' + shlex.join(program.argv[1:])]
    return "\n".join(lines) + "\n"


def mac_work(placement: MacPlacement, mirror: MirrorUse, program: Program) -> str:
    """The Mac shell text of the run; the Mac stderr joins the stdout, so the log gets both in order."""
    script = start_script(mirror.path, program, placement)
    return "exec 2>&1\n" + mirror.use(f"exec {MAC_ZSH} -c " + shlex.quote(script))


def check_block() -> None:
    """A Mac block of another session gives the run back, as it refuses each other Mac kind."""
    paths = claim_state.state_paths()
    session = owning_session()
    try:
        block = mac_test.blocking_claim_block(paths, session)
        if not isinstance(block, claim_state.NoMacBlock):
            block = mac_test.lift_expired_claim_block(paths, session, block)
    except (OSError, claim_state.StateReadError) as error:
        raise MachineFailed(f"the Mac block state cannot be read: {error}") from error
    if not isinstance(block, claim_state.NoMacBlock):
        reason = " ".join(block.reason.splitlines())
        raise MachineFailed(f"the Mac is blocked by {mac_test.holder_name(block.holder)}: {reason}")


@dataclass
class MacLines:
    """The Mac output. START and REFUSED stay out, and so does the last wrapper line with its blank line."""
    output: Output
    # The time of the START line: the program starts just after it.
    started_at: float | None = None
    refusal: str = ""
    held: list[str] = field(default_factory=list[str])

    @property
    def started(self) -> bool:
        return self.started_at is not None

    def write(self, line: str) -> None:
        if not self.started and line == START + "\n":
            self.started_at = time.time()
            return
        if not self.started and line.startswith(REFUSED):
            self.refusal = line.removeprefix(REFUSED).strip()
            return
        if EXIT_LINE.fullmatch(line):
            self.held.append(line)
            return
        self.flush()
        if line == "\n":
            self.held.append(line)
        else:
            self.output.write(line.encode())

    def flush(self) -> None:
        for line in self.held:
            self.output.write(line.encode())
        self.held.clear()

    def end(self, exited: bool) -> None:
        if exited and self.held and EXIT_LINE.fullmatch(self.held[-1]):
            _ = self.held.pop()
            if self.held and self.held[-1] == "\n":
                _ = self.held.pop()
        self.flush()


@dataclass
class MacRun:
    """One run of the argv on the Mac."""
    placement: MacPlacement
    command: list[str]
    transport: MacTransport
    ssh: ExitStack
    remote: RemoteRun = field(default_factory=RemoteRun)
    status: int | None = None
    started_at: float | None = None

    @property
    def peak_bytes(self) -> int | None:
        return None

    def run(self, output: Output) -> int:
        lines = MacLines(output)
        try:
            status = self.transport.stream(self.command, self.remote, self.ssh, lines.write)
        finally:
            self.started_at = lines.started_at
        exited = self.remote.state == RemoteState.EXITED
        lines.end(exited)
        if lines.refusal:
            raise Refused(f"the Mac refused the run: {lines.refusal}")
        if not lines.started:
            raise MachineFailed(f"the Mac did not start the program (status {status})")
        if not exited:
            # The program started, so the run does not go to another machine.
            raise Refused(f"the Mac returned no exit status (ssh status {status})")
        self.status = status
        return status

    def stopped(self) -> bool:
        """Close SSH, which ends the Mac work, and prove that no work of the run is left on the Mac."""
        self.ssh.close()
        if self.remote.state != RemoteState.LIVE:
            return True
        try:
            # With no mac_group line, the stop cannot prove the end, so the work counts as live.
            return self.transport.stop(self.placement, self.remote)
        except OSError:
            return False


SSH = MacTransport()


@contextmanager
def mac_route(line: Line, placed: Placed, program: Program, transport: MacTransport = SSH) -> Generator[MachineRun]:
    if not isinstance(placed, wire.Assignment):
        raise MachineFailed("the queue gave the Mac a local grant")
    check_block()
    assignment = placed.assignment
    placement = MacPlacement(assignment.destination.host, assignment.episode, assignment.attempt)
    with transport.mirror(program.repo, placement.host) as mirror, ExitStack() as ssh:
        command = mac_transport.wrapped_command(placement.host, mac_work(placement, mirror, program), "")
        mac = MacRun(placement, command, transport, ssh)
        try:
            yield mac
        except MachineFailed:
            if not mac.stopped():
                raise Refused(STILL_LIVE) from None
            raise
        except BaseException as error:
            if mac.stopped():
                line.close(placed, None, f"the Mac run stopped: {error or type(error).__name__}")
            else:
                note(STILL_LIVE)
            raise
        _ = mac.stopped()
        line.close(placed, mac.status, "" if mac.status is not None else "the Mac run did not start")
