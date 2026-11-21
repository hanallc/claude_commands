"""Read fields and promotion destinations from staff-owned production documents."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from stat import S_ISDIR
from typing import NamedTuple, cast, override
from urllib.parse import unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from scripts.agents import remote
from scripts.buildlog.repository import remote_identity
from scripts.lib import git_files
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.production import staff, showrunners
from scripts.production.production_names import (
    BACKUP_CHECKOUT, BACKUP_ROOT, GUARD_READY_FILE, LIVE_DOCUMENT_SUFFIX, MERGE_CHECKOUT_FIELD, NOTES_DIRECTORY,
)


WRAPPED = "> **Status: PRODUCTION — wrapped.**"
DEMOTED = ("> **Status: ended {date}.** The user demoted this production to the one unit {child} under {parent}. "
           "That unit's plan is `{plan}`. This doc is a record and no session reads it as live state.")
GUARD_REPOSITORY = Path(__file__).resolve().parents[2]


def production_backup_root() -> Path:
    """Return the folder that contains the production backup repositories."""
    return Path.home() / BACKUP_ROOT


def production_backup_checkout(owner: staff.StaffId) -> Path:
    """Return the local Git backup for this production."""
    return production_backup_root() / owner / BACKUP_CHECKOUT


class Refusal(Exception):
    """An invalid request that must leave the production unchanged."""


class ProductionPathsError(Refusal):
    """A production path cannot be resolved safely."""


@dataclass(frozen=True)
class LegacyProduction:
    """The document has no Merge checkout field."""


@dataclass(frozen=True)
class EnrolledPaths:
    main: Path
    live_doc: Path
    log: Path
    notes: Path
    integration: Path


@dataclass(frozen=True)
class ProtectedCheckout:
    path: Path
    repository: Path  # Canonical Git common directory.
    branch: str


@dataclass(frozen=True)
class LiveFileConflict:
    live_path: Path
    incoming_path: Path

    @override
    def __str__(self) -> str:
        return f"{self.live_path}: conflicts with incoming {self.incoming_path}"


@dataclass(frozen=True)
class ProductionDocument:
    doc: Path
    name: str
    merge_branch: str
    checkout: Path
    log: Path
    zone: ZoneInfo
    merge_checkout: Path | LegacyProduction = field(default_factory=LegacyProduction, kw_only=True)

    @property
    def repository_checkout(self) -> Path:
        """Return the checkout for published repository reads."""
        return self.merge_checkout if isinstance(self.merge_checkout, Path) else self.checkout

    def for_owner(self, owner: staff.StaffRow) -> Production:
        return Production(self.doc, self.name, self.merge_branch, self.checkout, self.log, self.zone, owner,
                          merge_checkout=self.merge_checkout)


@dataclass(frozen=True)
class Production(ProductionDocument):
    owner: staff.StaffRow


def cell_value(value: str) -> str:
    """Read the value before its commentary."""
    quoted = re.search(r"`([^`]+)`", value)
    return quoted.group(1) if quoted else value.split(" — ", 1)[0].strip()


class FieldPresent(NamedTuple):
    text: str


class FieldAbsent(NamedTuple):
    pass


class NoPromotion(NamedTuple):
    pass


class NoMac(NamedTuple):
    pass


class MacCheckout(NamedTuple):
    path: PurePosixPath
    """As written in the doc; a leading ~ is the Mac's home, so the Mac's shell expands it."""


class PromoteTo(NamedTuple):
    checkouts: tuple[Path, ...]
    mac: NoMac | MacCheckout


def optional_field(lines: list[str], field: str) -> FieldPresent | FieldAbsent:
    prefix = f"- **{field}:**"
    for line in lines:
        if line.startswith(prefix):
            return FieldPresent(line[len(prefix):].removeprefix(" "))
    return FieldAbsent()


def production_field(lines: list[str], field: str) -> str:
    """Read a Production Context field without its commentary."""
    present = optional_field(lines, field)
    value = cell_value(present.text) if isinstance(present, FieldPresent) else ""
    if not value:
        raise Refusal(f"production doc lacks {field}")
    return value


def read_production(reference: str | staff.StaffRow, record: staff.StaffSnapshot | None = None) -> Production:
    """Read the document from the showrunner row."""
    try:
        row = reference if isinstance(reference, staff.StaffRow) else showrunners.owner_for(reference, record)
        if not isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            raise Refusal(f"{row.name}: no production document")
        document = read_new_production(row.doc.path, row.name)
        return document.for_owner(row)
    except (OSError, UnicodeError, KeyError, ValueError) as error:
        raise Refusal(f"cannot read production doc: {error}") from error


def read_new_production(doc: Path, name: str, *, resume_branch: str = "") -> ProductionDocument:
    """Read a new production document with its specified staff name."""
    try:
        doc = doc.expanduser().resolve(strict=True)
        lines = doc.read_text(encoding="utf-8").splitlines()
        merge_checkout = optional_field(lines, MERGE_CHECKOUT_FIELD)
        if isinstance(merge_checkout, FieldPresent):
            return _read_enrolled(doc, name, lines, merge_checkout, resume_branch=resume_branch)
        merge_branch = production_field(lines, "Merge branch")
        checkout = Path(production_field(lines, "Showrunner checkout")).expanduser().resolve()
        log = Path(production_field(lines, "Log"))
        if log.is_absolute():
            raise Refusal("production Log must be relative to Showrunner checkout")
        return ProductionDocument(doc, name, merge_branch, checkout, checkout / log, user_zone())
    except (OSError, UnicodeError, KeyError, ValueError) as error:
        raise Refusal(f"cannot read production doc: {error}") from error


def _read_enrolled(doc: Path, name: str, lines: list[str], declared: FieldPresent,
                   *, resume_branch: str = "") -> ProductionDocument:
    try:
        main_path = Path(cell_value(declared.text))
        if not main_path.is_absolute():
            raise Refusal(f"{MERGE_CHECKOUT_FIELD} must be absolute")
        main = git_files.at(main_path.resolve(strict=True))
        if main.git_dir != main.common_dir or git_files.head(main) != git_files.OnBranch("main"):
            raise Refusal(f"{main.toplevel}: expected the main checkout on main")
        if not doc.is_relative_to(main.toplevel) or git_files.locate(doc.parent) != main:
            raise Refusal(f"{doc}: document must be in {main.toplevel}")
        repository = optional_field(lines, "Repository")
        if isinstance(repository, FieldPresent):
            if Path(cell_value(repository.text)).expanduser().resolve(strict=True) != main.toplevel:
                raise Refusal(f"Repository must name {main.toplevel}")
        integration = git_files.at(Path(production_field(lines, "Showrunner checkout")).expanduser().resolve(strict=True))
        branch = production_field(lines, "Merge branch")
        if integration == main or integration.common_dir != main.common_dir:
            raise Refusal("Showrunner checkout must be a separate worktree of the same repository")
        head = git_files.head(integration)
        if head != git_files.OnBranch(branch) and (not resume_branch or head != git_files.OnBranch(resume_branch)):
            raise Refusal(f"{integration.toplevel}: expected the checkout on {branch}")
        relative_log = Path(production_field(lines, "Log"))
        log = (main.toplevel / relative_log).resolve()
        if relative_log.is_absolute() or not log.is_relative_to(main.toplevel) or log == main.toplevel or log == doc:
            raise Refusal("Log must name a separate file below the main checkout")
        return ProductionDocument(doc, name, branch, integration.toplevel, log, user_zone(),
                                  merge_checkout=main.toplevel)
    except (Refusal, OSError, UnicodeError, RuntimeError, ValueError) as error:
        raise ProductionPathsError(f"{doc}: {error}") from error


def local_production_documents() -> tuple[staff.StaffRow, ...]:
    """Read local showrunner rows with live production documents."""
    try:
        machine = remote.local_machine()
        return tuple(row for row in staff.local_snapshot().find(kind="showrunner")
                     if isinstance(row.doc, staff.LiveDoc) and row.doc.machine == machine)
    except staff.StaffAbsent:
        return ()
    except (OSError, ValueError) as error:
        raise ProductionPathsError(f"cannot read the local staff record: {error}") from error


def _notes_path(main: Path, owner: staff.StaffRow) -> Path:
    notes = main / NOTES_DIRECTORY / owner.id
    if notes.resolve() != notes or not notes.is_relative_to(main):
        raise ProductionPathsError(f"{notes}: notes must use a canonical path below the main checkout")
    return notes


def _enrolled_paths(document: ProductionDocument, main: Path, owner: staff.StaffRow) -> EnrolledPaths:
    return EnrolledPaths(main, document.doc, document.log, _notes_path(main, owner), document.checkout)


def legacy_document_path(doc: Path) -> Path:
    """Return the tracked document path for history queries."""
    if doc.name.endswith(LIVE_DOCUMENT_SUFFIX):
        return doc.with_name(doc.name.removesuffix(LIVE_DOCUMENT_SUFFIX) + ".md")
    return doc


def live_paths_lock_file(checkout: Path) -> Path:
    """Use one lock for live paths across worktrees and state directories."""
    return git_files.locate(checkout).common_dir / "production-live-paths.lock"


def document_history_file(checkout: git_files.Checkout, owner: staff.StaffId) -> Path:
    return checkout.common_dir / f"production-document-paths-{owner}.json"


def former_live_documents(history: Path) -> tuple[Path, ...]:
    """Read live document paths in rename order."""
    try:
        data = cast(object, json.loads(history.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return ()
    except (ValueError, UnicodeError) as error:
        raise OSError(f"{history}: cannot read document history: {error}") from error
    if not isinstance(data, list):
        raise OSError(f"{history}: expected a list of document paths")
    paths: list[Path] = []
    for value in cast(list[object], data):
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise OSError(f"{history}: expected absolute document paths")
        paths.append(Path(value))
    return tuple(paths)


def remember_document_rename(main: Path, owner: staff.StaffId, source: Path, destination: Path) -> None:
    """Record the live paths before moving a production document."""
    history = document_history_file(git_files.at(main), owner)
    paths = former_live_documents(history)
    updated = tuple(dict.fromkeys((*paths, source, destination)))
    if updated != paths:
        write_text_atomic(history, json.dumps([str(path) for path in updated]) + "\n")


def _bound_owner(doc: Path, *previous: Path) -> staff.StaffRow:
    rows = [(row, row.doc.path.resolve()) for row in local_production_documents()
            if isinstance(row.doc, staff.LiveDoc)]
    owners = [row for row, path in rows if path == doc] or [row for row, path in rows if path in previous]
    if len(owners) != 1:
        raise ProductionPathsError(f"{doc}: expected one showrunner with this document")
    return owners[0]


def _resolve_enrolled_paths(doc: Path, lines: list[str], declared: FieldPresent) -> EnrolledPaths:
    document = _read_enrolled(doc, doc.stem, lines, declared)
    main = document.merge_checkout
    assert isinstance(main, Path)
    if doc.name.endswith(LIVE_DOCUMENT_SUFFIX):
        # The move writes the live document before it updates the staff binding.
        old = legacy_document_path(doc).relative_to(main)
        owner = _bound_owner(doc, main / old, document.checkout / old)
    else:
        owner = _bound_owner(doc)
    return _enrolled_paths(document, main, owner)


def resolve_production_paths(doc: Path) -> EnrolledPaths | LegacyProduction:
    """Resolve an enrolled document and its owner from the local staff record."""
    try:
        live_doc = doc.expanduser().resolve(strict=True)
        lines = live_doc.read_text(encoding="utf-8").splitlines()
        declared = optional_field(lines, MERGE_CHECKOUT_FIELD)
        if isinstance(declared, FieldAbsent):
            return LegacyProduction()
        return _resolve_enrolled_paths(live_doc, lines, declared)
    except (OSError, UnicodeError, RuntimeError, ValueError) as error:
        raise ProductionPathsError(f"{doc}: {error}") from error


def _existing_notes_directory(notes: Path) -> Path | None:
    try:
        status = notes.stat()
    except FileNotFoundError:
        return None
    if not S_ISDIR(status.st_mode):
        raise ProductionPathsError(f"{notes}: expected a notes directory")
    return notes


def staged_notes_directory(doc: Path) -> Path | None:
    """Return the production's notes directory in main when it exists."""
    try:
        live_doc = doc.expanduser().resolve(strict=True)
        lines = live_doc.read_text(encoding="utf-8").splitlines()
        declared = optional_field(lines, MERGE_CHECKOUT_FIELD)
        if isinstance(declared, FieldPresent):
            return _existing_notes_directory(_resolve_enrolled_paths(live_doc, lines, declared).notes)
        if isinstance(optional_field(lines, "Repository"), FieldAbsent):
            return None
        main = Path(production_field(lines, "Repository")).expanduser().resolve()
        notes = _existing_notes_directory(_notes_path(main, _bound_owner(live_doc)))
        if notes is None:
            return None
        main_git = git_files.at(main)
        source = git_files.locate(live_doc.parent)
        if main_git.git_dir != main_git.common_dir or source.common_dir != main_git.common_dir:
            raise ProductionPathsError(f"{notes}: expected the main checkout of the production repository")
        return notes
    except ProductionPathsError:
        raise
    except (Refusal, OSError, UnicodeError, RuntimeError, ValueError) as error:
        raise ProductionPathsError(f"{doc}: {error}") from error


def _known_live_document(doc: Path, row: staff.StaffRow, checkout: git_files.Checkout) -> bool:
    return (doc.name.endswith(LIVE_DOCUMENT_SUFFIX)
            or doc in former_live_documents(document_history_file(checkout, row.id)))


def _repository_live_paths(checkout: git_files.Checkout) -> dict[Path, Path]:
    paths: dict[Path, Path] = {}
    for row in local_production_documents():
        assert isinstance(row.doc, staff.LiveDoc)
        doc = row.doc.path.resolve()
        try:
            existing = next(parent for parent in doc.parents if parent.is_dir())
            if git_files.locate(existing).common_dir != checkout.common_dir:
                continue
            lines = doc.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            if _known_live_document(doc, row, checkout):
                raise
            continue
        except git_files.NotACheckout:
            continue
        declared = optional_field(lines, MERGE_CHECKOUT_FIELD)
        if isinstance(declared, FieldPresent):
            production = _read_enrolled(doc, row.name, lines, declared)
            main = production.merge_checkout
            assert isinstance(main, Path)
            enrolled = _enrolled_paths(production, main, row)
            for live in (enrolled.live_doc, enrolled.log, enrolled.notes):
                paths[live] = live.relative_to(main)
    return paths


def live_file_conflicts(repository: Path, incoming: str) -> tuple[LiveFileConflict, ...]:
    """Compare protected paths with the incoming tree entries."""
    try:
        checkout = git_files.locate(repository)
        live_paths = _repository_live_paths(checkout)
        if not live_paths:
            return ()
        tip = subprocess.run(
            ["git", "-C", str(checkout.toplevel), "rev-parse", "--verify", "--end-of-options", f"{incoming}^{{tree}}"],
            capture_output=True, text=True, check=False)
        if tip.returncode:
            raise ProductionPathsError(f"{repository}: cannot resolve incoming tree: {tip.stderr.strip()}")
        tree = subprocess.run(
            ["git", "-C", str(checkout.toplevel), "ls-tree", "-r", "-z", "--name-only", tip.stdout.strip()],
            capture_output=True, text=True, errors="surrogateescape", check=False)
        if tree.returncode:
            raise ProductionPathsError(f"{repository}: cannot read incoming tree: {tree.stderr.strip()}")
        entries = tuple(Path(name) for name in tree.stdout.split("\0") if name)
        return tuple(LiveFileConflict(live, entry) for live, relative in sorted(live_paths.items())
                     for entry in entries if entry.is_relative_to(relative) or relative.is_relative_to(entry))
    except (OSError, UnicodeError, RuntimeError, ValueError) as error:
        raise ProductionPathsError(f"{repository}: cannot check live files: {error}") from error


def require_no_live_file_conflicts(checkout: Path, incoming: str) -> None:
    try:
        conflicts = live_file_conflicts(checkout, incoming)
    except ProductionPathsError as error:
        raise OSError(str(error)) from error
    if conflicts:
        raise OSError("\n".join(str(conflict) for conflict in conflicts))


def _registered_production_documents() -> tuple[staff.StaffRow, ...]:
    try:
        rows = staff.authority_snapshot().find(kind="showrunner")
        return tuple(row for row in rows if isinstance(row.doc, staff.LiveDoc))
    except (OSError, ValueError, staff.StaffRefused) as error:
        raise ProductionPathsError(f"cannot read the staff record: {error}") from error


def protected_checkouts() -> frozenset[ProtectedCheckout]:
    """Read the registered main and integration checkouts on this host."""
    return _protected_checkouts(_registered_production_documents())


def _protection_host(machine: str) -> remote.MachineAlias | None:
    local = remote.local_machine()
    if machine.casefold() == local.casefold():
        return None
    try:
        target = remote.machine_alias(machine)
        current = remote.machine_alias(local)
    except (OSError, ValueError) as error:
        raise ProductionPathsError(f"cannot resolve staff machine {machine}: {error}") from error
    if isinstance(target, remote.MachineNameRefused):
        raise ProductionPathsError(target.reason)
    return None if target == current else target


def _protected_checkouts(rows: tuple[staff.StaffRow, ...]) -> frozenset[ProtectedCheckout]:
    protected: set[ProtectedCheckout] = set()
    for row in rows:
        assert isinstance(row.doc, staff.LiveDoc)
        if _protection_host(row.doc.machine) is not None:
            continue
        try:
            doc = row.doc.path.resolve()
            try:
                lines = doc.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                if doc.name.endswith(LIVE_DOCUMENT_SUFFIX):
                    raise
                try:
                    parent = next(folder for folder in doc.parents if folder.is_dir())
                    checkout = git_files.locate(parent)
                except git_files.NotACheckout:
                    pass
                else:
                    if _known_live_document(doc, row, checkout):
                        raise
                lines = []
            declared = optional_field(lines, MERGE_CHECKOUT_FIELD)
            if isinstance(declared, FieldPresent):
                document = _read_enrolled(doc, row.name, lines, declared)
                main = document.merge_checkout
                assert isinstance(main, Path)
                repository = git_files.at(main).common_dir
                protected.add(ProtectedCheckout(main, repository, "main"))
                protected.add(ProtectedCheckout(document.checkout, repository, document.merge_branch))
            else:
                protected.update(_legacy_checkouts(row, lines))
        except (OSError, UnicodeError, RuntimeError, ValueError) as error:
            raise ProductionPathsError(f"{row.doc.path}: {error}") from error
    return frozenset(protected)


@dataclass(frozen=True)
class LocalOrigin:
    machine: str
    path: Path


@dataclass(frozen=True)
class NetworkOrigin:
    identity: str


def _protection_command(host: remote.MachineAlias | None, *args: str) -> subprocess.CompletedProcess[str]:
    command = list(args) if host is None else [
        "ssh", "-o", "BatchMode=yes", host.alias, shlex.join(args),
    ]
    return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False)


_SCP_ORIGIN = re.compile(r"(?:(?P<user>[^@/\s:]+)@)?(?P<host>\[[^\]]+\]|[^:/\s@]+):(?P<path>.+)")
_REMOTE_HELPER = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*::")
_SSH_CONFIG = r"""
cd -- "$1" || exit 2
shift
[ -z "${GIT_SSH_COMMAND-}${GIT_SSH-}${GIT_SSH_VARIANT-}" ] || exit 2
git config --get-regexp '^(core\.sshcommand|ssh\.variant)$' >/dev/null
[ "$?" -eq 1 ] || exit 2
exec ssh -G "$@"
"""


def _network_origin(url: str, checkout: Path, host: remote.MachineAlias | None) -> NetworkOrigin:
    scp = _SCP_ORIGIN.fullmatch(url) if "://" not in url else None
    if scp:
        alias = scp["host"].strip("[]")
        user = scp["user"] or ""
        path = scp["path"]
        if not path.startswith("/"):
            path = "/~/" + path
        port = None
    else:
        address = urlsplit(url)
        if address.scheme in ("https", "http", "git") and address.hostname and address.path:
            identity = remote_identity(url, str(checkout))
            return NetworkOrigin(identity.lower() if identity.startswith("https://github.com/") else identity)
        if address.scheme != "ssh" or not address.hostname or not address.path or address.query or address.fragment:
            raise ProductionPathsError(f"{checkout}: cannot resolve an origin URL")
        alias = address.hostname
        user = unquote(address.username or "")
        port = address.port
        path = unquote(address.path)
    repository_path = PurePosixPath(path)
    if ".." in repository_path.parts:
        raise ProductionPathsError(f"{checkout}: cannot resolve the SSH repository path")
    path = repository_path.as_posix()
    options = [*(("-l", user) if user else ()), *(("-p", str(port)) if port is not None else ())]
    config = _protection_command(host, "sh", "-c", _SSH_CONFIG, "origin-ssh", str(checkout), *options, "--", alias)
    fields = {parts[0]: parts[1] for line in config.stdout.splitlines()
              if len(parts := line.split(maxsplit=1)) == 2}
    server = fields.get("hostname", "").lower().rstrip(".")
    service = fields.get("port", "")
    if config.returncode or not server or not service.isdecimal() or not 0 < int(service) < 65536:
        raise ProductionPathsError(f"{checkout}: cannot resolve SSH origin {alias}")
    if server == alias.lower().rstrip("."):
        # ssh -G accepts an undefined alias. Require a server name in that case.
        lookup = _protection_command(host, "python3", "-c",
                                     "import socket, sys; socket.getaddrinfo(sys.argv[1], None)", server)
        if lookup.returncode:
            raise ProductionPathsError(f"{checkout}: cannot resolve SSH origin {alias}")
    if (server, service) in (("github.com", "22"), ("ssh.github.com", "443")):
        return NetworkOrigin("https://github.com/" + path.removeprefix("/~/").lstrip("/").rstrip("/")
                             .lower().removesuffix(".git"))
    authority = f"[{server}]" if ":" in server else server
    return NetworkOrigin(urlunsplit(("ssh", f"{authority}:{int(service)}", path.rstrip("/"), "", "")))


def _origins(checkout: Path, host: remote.MachineAlias | None = None, *,
             push_only: bool = False) -> frozenset[LocalOrigin | NetworkOrigin]:
    origins: set[LocalOrigin | NetworkOrigin] = set()
    for options in ((("--push",),) if push_only else ((), ("--push",))):
        result = _protection_command(host, "git", "-C", str(checkout), "remote", "get-url", "--all",
                                     *options, "origin")
        if result.returncode == 2 and (push_only or not options):
            return frozenset()
        if result.returncode or not result.stdout.strip():
            raise ProductionPathsError(f"{checkout}: cannot read origin")
        for url in result.stdout.splitlines():
            if _REMOTE_HELPER.match(url):
                raise ProductionPathsError(f"{checkout}: cannot resolve an origin URL")
            location = urlsplit(url) if url.startswith("file://") else None
            if location is not None or ("://" not in url and not _SCP_ORIGIN.fullmatch(url)):
                path = unquote(location.path) if location is not None else url
                if location is not None and location.netloc not in ("", "localhost"):
                    raise ProductionPathsError(f"{checkout}: cannot resolve the file origin")
                if host is None:
                    origins.add(LocalOrigin(remote.local_machine().casefold(),
                                            (checkout / Path(path).expanduser()).resolve(strict=True)))
                else:
                    resolved = _protection_command(host, "sh", "-c",
                        'cd -- "$1" && cd -- "$2" && pwd -P', "origin-path", str(checkout), path)
                    if resolved.returncode or not Path(resolved.stdout.strip()).is_absolute():
                        raise ProductionPathsError(f"{host.alias}: cannot resolve the file origin for {checkout}")
                    origins.add(LocalOrigin(host.alias, Path(resolved.stdout.strip())))
            else:
                origins.add(_network_origin(url, checkout, host))
    return frozenset(origins)


def _registered_origins(checkout: Path, host: remote.MachineAlias | None = None) -> frozenset[LocalOrigin | NetworkOrigin]:
    origins = _origins(checkout, host)
    if not origins:
        raise ProductionPathsError(f"{checkout}: cannot resolve the registered origin")
    return origins


def _remote_branch_origins(row: staff.StaffRow, host: remote.MachineAlias, branch: str) -> frozenset[LocalOrigin | NetworkOrigin]:
    assert isinstance(row.doc, staff.LiveDoc)
    read = _protection_command(host, "cat", str(row.doc.path))
    if read.returncode:
        raise ProductionPathsError(f"{row.doc.machine}: cannot read {row.doc.path}")
    lines = read.stdout.splitlines()
    declared = optional_field(lines, MERGE_CHECKOUT_FIELD)
    checkout_field = optional_field(lines, "Showrunner checkout")
    branch_field = optional_field(lines, "Merge branch")
    merge_branch = cell_value(branch_field.text) if isinstance(branch_field, FieldPresent) else ""
    if isinstance(declared, FieldPresent):
        main = production_field(lines, MERGE_CHECKOUT_FIELD)
        protected_branch = production_field(lines, "Merge branch")
        if not Path(main).is_absolute():
            raise ProductionPathsError(f"{row.doc.path}: {MERGE_CHECKOUT_FIELD} must be absolute")
        if branch == "main":
            return _registered_origins(Path(main), host)
        if branch == protected_branch:
            return (_registered_origins(Path(main), host)
                    | _registered_origins(Path(production_field(lines, "Showrunner checkout")), host))
        return frozenset()
    if merge_branch and branch != merge_branch:
        return frozenset()
    paths: set[Path] = set()
    if isinstance(checkout_field, FieldPresent) and (path := cell_value(checkout_field.text)):
        paths.add(Path(path))
    if isinstance(row.worktree, staff.PresentWorktree) and row.worktree.machine == row.doc.machine:
        paths.add(row.worktree.path)
    origins: set[LocalOrigin | NetworkOrigin] = set()
    for path in paths:
        if not merge_branch:
            head = _protection_command(host, "git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD")
            if head.returncode not in (0, 1):
                raise ProductionPathsError(f"{row.doc.machine}: cannot read the branch in {path}")
            if head.returncode or head.stdout.strip() != branch:
                continue
        origins.update(_registered_origins(path, host))
    return frozenset(origins)


def require_removable_checkout(path: Path, repository: Path, branch: str) -> None:
    """Keep protected checkout paths and branches that removal can delete on origin."""
    try:
        rows = _registered_production_documents()
        protected = _protected_checkouts(rows)
        candidate = path.resolve()
        for checkout in protected:
            if checkout.path.is_relative_to(candidate):
                raise ProductionPathsError(f"{candidate}: contains protected production checkout {checkout.path}")
            if candidate.is_relative_to(checkout.path):
                raise ProductionPathsError(f"{candidate}: inside protected production checkout {checkout.path}")
        matching = [checkout for checkout in protected if branch and checkout.branch == branch]
        if not branch:
            return
        checkout = git_files.locate(repository)
        if any(keep.repository == checkout.common_dir for keep in matching):
            raise ProductionPathsError(f"{repository}: protected production branch {branch}")
        origins = _origins(checkout.toplevel, push_only=True)
        if not origins:
            return
        remote_rows = [(row, host) for row in rows if isinstance(row.doc, staff.LiveDoc)
                       and (host := _protection_host(row.doc.machine)) is not None]
        if (any(origins & _registered_origins(keep.path) for keep in matching)
                or any(origins & _remote_branch_origins(row, host, branch) for row, host in remote_rows)):
            raise ProductionPathsError(f"{repository}: protected production origin branch {branch}")
    except ProductionPathsError:
        raise
    except (OSError, RuntimeError, ValueError, Refusal, subprocess.TimeoutExpired) as error:
        raise ProductionPathsError(f"{path}: cannot check production protection: {error}") from error


def _legacy_checkouts(row: staff.StaffRow, lines: list[str]) -> tuple[ProtectedCheckout, ...]:
    path_field = optional_field(lines, "Showrunner checkout")
    branch_field = optional_field(lines, "Merge branch")
    paths: set[Path] = set()
    if isinstance(path_field, FieldPresent) and (path := cell_value(path_field.text)):
        paths.add(Path(path).expanduser().resolve())
    if isinstance(row.worktree, staff.PresentWorktree) and row.worktree.machine == remote.local_machine():
        paths.add(row.worktree.path.resolve())
    result: list[ProtectedCheckout] = []
    for path in paths:
        try:
            checkout = git_files.at(path)
        except git_files.NotACheckout:
            continue
        head = git_files.head(checkout)
        branch = cell_value(branch_field.text) if isinstance(branch_field, FieldPresent) else ""
        if not branch and isinstance(head, git_files.OnBranch):
            branch = head.name
        result.append(ProtectedCheckout(path, checkout.common_dir, branch))
    return tuple(result)


@dataclass(frozen=True)
class ClaudeGuardHook:
    settings: Path
    event: str
    matcher: str
    command: str


@dataclass(frozen=True)
class CodexGuardHook:
    codex_home: Path
    event: str
    tool: str
    command: str


@dataclass(frozen=True)
class GuardReady:
    revision: str
    recorded_at: datetime
    claude: tuple[ClaudeGuardHook, ...]
    codex: tuple[CodexGuardHook, ...]


def _json_fields(path: Path) -> dict[str, object]:
    return _fields(cast(object, json.loads(path.read_text(encoding="utf-8"))))


def _fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return cast(dict[str, object], value)


def _rows(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise ValueError("expected a JSON list")
    return tuple(cast(list[object], value))


def _text(fields: dict[str, object], key: str) -> str:
    value = fields[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must contain text")
    return value


def _hook_present(path: Path, event: str, matcher: str, command: str) -> bool:
    events = _fields(_json_fields(path)["hooks"])
    for value in _rows(events[event]):
        group = _fields(value)
        if group.get("matcher") != matcher:
            continue
        for row in _rows(group["hooks"]):
            hook = _fields(row)
            if hook.get("type") == "command" and hook.get("command") == command:
                return True
    return False


def _claude_guard(value: object) -> ClaudeGuardHook:
    fields = _fields(value)
    entry = ClaudeGuardHook(Path(_text(fields, "settings")), _text(fields, "event"),
                            _text(fields, "matcher"), _text(fields, "command"))
    if not entry.settings.is_absolute() or entry.event not in ("PreToolUse", "PostToolUse"):
        raise ValueError("invalid Claude guard entry")
    return entry


def _codex_guard(value: object) -> CodexGuardHook:
    fields = _fields(value)
    entry = CodexGuardHook(Path(_text(fields, "codex_home")), _text(fields, "event"),
                           _text(fields, "tool"), _text(fields, "command"))
    if (not entry.codex_home.is_absolute() or entry.event not in ("PreToolUse", "PostToolUse")
            or entry.tool not in ("Bash", "apply_patch")):
        raise ValueError("invalid Codex guard entry")
    return entry


def read_guard_ready() -> GuardReady | None:
    """Accept the guard proof only when main and both clients still match it."""
    try:
        fields = _json_fields(showrunners.SHOWRUNNER_STATE_DIR / GUARD_READY_FILE)
        if type(fields["version"]) is not int or fields["version"] != 1:
            return None
        revision = _text(fields, "revision")
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            return None
        recorded_at = datetime.fromisoformat(_text(fields, "recorded_at"))
        if recorded_at.utcoffset() != timedelta(0):
            return None
        claude = tuple(_claude_guard(entry) for entry in _rows(fields["claude"]))
        codex = tuple(_codex_guard(entry) for entry in _rows(fields["codex"]))
        events = {"PreToolUse", "PostToolUse"}
        if not claude or not codex:
            return None
        if any({entry.event for entry in claude if entry.settings == path} != events
               for path in {entry.settings for entry in claude}):
            return None
        if any({(entry.event, entry.tool) for entry in codex if entry.codex_home == home}
               != {(event, tool) for event in events for tool in ("Bash", "apply_patch")}
               for home in {entry.codex_home for entry in codex}):
            return None
        ancestor = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(GUARD_REPOSITORY),
             "merge-base", "--is-ancestor", revision, "refs/heads/main"],
            capture_output=True, check=False,
        )
        if ancestor.returncode:
            return None
        if all(_hook_present(entry.settings, entry.event, entry.matcher, entry.command) for entry in claude) and all(
            _hook_present(entry.codex_home / "hooks.json", entry.event, entry.tool, entry.command) for entry in codex
        ):
            return GuardReady(revision, recorded_at, claude, codex)
    except (OSError, UnicodeError, KeyError, ValueError, RuntimeError):
        return None
    return None


def promotion_from_doc(lines: list[str]) -> NoPromotion | PromoteTo:
    """Read the production's promotion destinations in their declared order."""
    field = optional_field(lines, "Promote")
    if isinstance(field, FieldAbsent):
        return NoPromotion()
    promote_raw = field.text.split(" — ", 1)[0].replace("`", "")
    if promote_raw.strip().casefold() == "none":
        return NoPromotion()
    local: list[Path] = []
    mac: NoMac | MacCheckout = NoMac()
    for item in promote_raw.split(","):
        item = item.strip()
        if item.startswith("mac "):
            mac = MacCheckout(PurePosixPath(item[4:].strip()))
        elif item:
            local.append(Path(item).expanduser())
    return PromoteTo(tuple(local), mac) if local or isinstance(mac, MacCheckout) else NoPromotion()


