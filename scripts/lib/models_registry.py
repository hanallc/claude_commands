"""Read, atomically edit and publish the shared model registry."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from enum import StrEnum
from pathlib import Path
from typing import Literal, NamedTuple, cast, final

from scripts.lib import build_folders
from scripts.lib import conf_reads, land_main, main_publish, main_push_lock, registry_rows, waiting_rows
from scripts.lib.registry_rows import parse_registry as parse_registry
from scripts.lib.operation_environment import OPERATION_ENVIRONMENT as OPERATION_ENVIRONMENT
from scripts.lib.operation_environment import agent_environment as agent_environment
from scripts.lib.process_failure import failure_detail
from scripts.lib.atomic_file import write_text_atomic

DEFAULT_PATH = Path("~/.claude/config/models.conf").expanduser()
REGISTRY_PATH = "config/models.conf"


class LaunchModelConfiguration(NamedTuple):
    """One configuration for a launch and its child processes."""
    path: Path
    text: str
    rows: tuple[registry_rows.RegistryRow, ...]

    def environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        return _environment(environment, OPERATION_ENVIRONMENT[0], self)


class FreshLaunchConfiguration(NamedTuple):
    """The next operation must read a new registry configuration."""


class RegistryEditBase(NamedTuple):
    """The exact text and parsed rows that one editor validates and changes."""
    path: Path
    text: str
    rows: tuple[registry_rows.RegistryRow, ...]

    def environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        return _environment(environment, OPERATION_ENVIRONMENT[1], self)


def _encoded(configuration: LaunchModelConfiguration | RegistryEditBase) -> str:
    return json.dumps([str(configuration.path), configuration.text])


def _environment(environment: Mapping[str, str], variable: str,
                 configuration: LaunchModelConfiguration | RegistryEditBase) -> dict[str, str]:
    return {**environment, variable: _encoded(configuration)}


def _snapshot(environment: Mapping[str, str], variable: str, path: Path, *,
              synchronize: bool = False) -> tuple[Path, str, tuple[registry_rows.RegistryRow, ...]]:
    if encoded := environment.get(variable, ""):
        path, text = _decoded(encoded)
    else:
        if synchronize:
            _sync_catalog(path, environment)
        text = registry_text(path)
    return path, text, tuple(registry_rows.records(text))


def launch_configuration(environment: Mapping[str, str], *, registry: Path | None = None) -> LaunchModelConfiguration:
    """Use the parent's operation configuration, or read a new configuration."""
    return LaunchModelConfiguration(*_snapshot(
        environment, OPERATION_ENVIRONMENT[0], registry or registry_path(), synchronize=True))


def edit_base(environment: Mapping[str, str]) -> RegistryEditBase:
    """Start an editor operation, or continue its parent's editor operation."""
    return RegistryEditBase(*_snapshot(environment, OPERATION_ENVIRONMENT[1], registry_path(), synchronize=True))


def _sync_catalog(path: Path, environment: Mapping[str, str]) -> None:
    if environment.get("MODELS_CONFIG_TIER_ONLY") == "1":
        return
    home = Path(environment.get("HOME", str(Path.home())))
    script = Path(__file__).resolve().parents[1] / "agents/sync_codex_catalog.sh"
    state = Path(environment.get("CODEX_CATALOG_SYNC_STATE_FILE") or home / ".local/state/codex-agent-catalog-sync/last_success")
    sources = (Path(environment.get("CODEX_CONFIG_FILE") or home / ".codex/config.toml"),
               Path(environment.get("CODEX_MODELS_CACHE_FILE") or home / ".codex/models_cache.json"))
    if not path.is_file() or not os.access(script, os.X_OK):
        return
    if state.is_file() and not any(source.exists() and source.stat().st_mtime > state.stat().st_mtime for source in sources):
        return
    try:
        result = subprocess.run([str(script)], env={**environment, "MODELS_CONFIG_FILE": str(path)},
                                stdout=subprocess.DEVNULL, check=False)
        if result.returncode == 0:
            return
    except OSError:
        pass
    print("WARNING: Codex catalog sync failed; using the current model registry.", file=sys.stderr)


def _decoded(encoded: str) -> tuple[Path, str]:
    match cast(object, json.loads(encoded)):
        case [str() as path, str() as text]:
            return Path(path), text
        case _:
            raise ValueError("invalid model configuration; start a new operation")


class LaunchSettings(NamedTuple):
    kind: Literal["claude", "codex"]
    model: str
    effort: str
    service_tier: str


def claude_model(model: str, environment: Mapping[str, str], *, timeout: float | None = None) -> str:
    """Resolve a model with the selected launch account's catalog."""
    try:
        configuration = LaunchModelConfiguration(*_snapshot(
            environment, OPERATION_ENVIRONMENT[0], registry_path(environment)))
        result = subprocess.run(
            ["bash", "-c", 'source "$1" && { IFS= read -r -d "" table; _models_table_load "$table"; } && '
             + '_agents_model_slug claude "$2"', "claude-model",
             str(Path(__file__).resolve().parents[1] / "agents/models_config.sh"), model],
            input=configuration_shell(configuration),
            env={**environment, "MODELS_CONFIG_TIER_ONLY": "1"}, capture_output=True, text=True, check=True, timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return model
    return result.stdout.strip()


def claude_arguments(arguments: Sequence[str], environment: Mapping[str, str]) -> list[str]:
    """Resolve model options after the caller selects the launch account."""
    resolved = list(arguments)
    for index, argument in enumerate(arguments):
        if argument == "--":
            break
        if argument == "--model":
            resolved[index + 1] = claude_model(arguments[index + 1], environment)
        elif argument.startswith("--model="):
            resolved[index] = "--model=" + claude_model(argument.partition("=")[2], environment)
    return resolved


class ProducerProfile(NamedTuple):
    family: Literal["claude", "codex"]
    model: str
    effort: str

    def value(self) -> str:
        return f"{self.family}:{self.model}:{self.effort}"


def parse_producer_profile(value: str) -> ProducerProfile:
    match value.split(":"):
        case ["claude" | "codex" as family, model, effort] if model and effort:
            return ProducerProfile(family, model, effort)
        case _:
            raise ValueError("expected <family>:<model>:<effort>")


def producer_defaults() -> ProducerProfile:
    """Read and validate the default profile for a new producer."""
    path = registry_path()
    key = "producer_defaults.model"
    try:
        text = registry_text(path)
        profile = parse_producer_profile(parse_registry(text).get("producer_defaults", {}).get("model", ""))
        configuration = LaunchModelConfiguration(path, text, tuple(registry_rows.records(text)))
        _ = launch_settings("producer_defaults", "model", configuration=configuration)
    except (OSError, ValueError) as error:
        raise ValueError(f"{path}: {key}: {' '.join(str(error).splitlines())}") from error
    return profile


def launch_settings(section: str, key: str, family: Literal["claude", "codex", "assigned"] = "assigned", *,
                    configuration: LaunchModelConfiguration | None = None) -> LaunchSettings:
    """Read launch settings through the existing shell resolver.

    A named family reads that family's row; a writer passes its candidate text.
    """
    if configuration is None:
        configuration = LaunchModelConfiguration(*_snapshot(os.environ, OPERATION_ENVIRONMENT[0], registry_path()))
    resolver = 'agents_resolve "$2.$3"' if family == "assigned" else '_agents_resolve_in_family "$2.$3" "$4"'
    resolver_arguments: list[str] = [] if family == "assigned" else [family]
    if section in ("producers", "producer_defaults"):
        value = next((row.value for row in reversed(configuration.rows)
                      if row.section == section and row.key == key), "")
        try:
            profile = parse_producer_profile(value)
        except ValueError as error:
            raise ValueError(f"invalid launch settings for {section}.{key}: {error}") from error
        resolver_arguments = [profile.family, f"{profile.model}:{profile.effort}"]
        resolver = ('AGENT_FAMILY="$4"; _agents_validate_pair "$2.$3" "$4" "$5" && '
                    '_agents_resolve_service_tier "$2.$3" "$4"')
    return _resolve_settings(configuration, resolver, [section, key, *resolver_arguments],
                             f"{section}.{key}")


def requested_model(word: str, effort: str, configuration: RegistryEditBase) -> LaunchSettings:
    """Validate a model and effort through the launch resolver."""
    resolver = ('AGENT_FAMILY="$(_agents_agent_families_inline "$2")"; '
                'case "$AGENT_FAMILY" in claude|codex) ;; *) '
                'printf "Unknown or ambiguous model %s. Valid models: %s %s %s %s\\n" "$2" '
                '"$(_agents_section_keys_inline claude.agents)" '
                '"$(_agents_section_keys_inline claude.families)" '
                '"$(_agents_section_keys_inline codex.families)" '
                '"$(_agents_section_keys_inline codex.agents)" >&2; exit 1 ;; esac; '
                '_agents_validate_pair model "$AGENT_FAMILY" "$2:$3" && '
                'AGENT_SERVICE_TIER=""')
    return _resolve_settings(configuration, resolver, [word, effort], word)


def _resolve_settings(configuration: LaunchModelConfiguration | RegistryEditBase, resolver: str,
                      arguments: list[str], context: str) -> LaunchSettings:
    result = subprocess.run(
        ["bash", "-c", 'source "$1" && { IFS= read -r -d "" table; _models_table_load "$table"; } && '
         + '{ ' + resolver + '; } && '
         + 'printf "%s\\n%s\\n%s\\n%s\\n" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT" "$AGENT_SERVICE_TIER"',
         "launch-settings", str(Path(__file__).resolve().parents[1] / "agents/models_config.sh"), *arguments],
        input=configuration_shell(configuration), text=True, capture_output=True, check=False,
        env={**os.environ, "MODELS_CONFIG_TIER_ONLY": "1"})
    values = result.stdout.splitlines()
    kind = values[0] if values else ""
    if result.returncode or len(values) != 4 or kind not in ("claude", "codex") or not values[1]:
        raise ValueError(result.stderr.strip() or f"invalid launch settings for {context}")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return LaunchSettings(kind, values[1], values[2], values[3])


class SharedRegistry(NamedTuple):
    repository: Path
    git_directory: Path


class UnpublishedRegistry(NamedTuple):
    pass


class FastForwardCheckFailed(OSError):
    """The check refused the move before any change to the checkout."""


def registry_path(environment: Mapping[str, str] | None = None) -> Path:
    configured = (os.environ if environment is None else environment).get("MODELS_CONFIG_FILE")
    if configured:
        return Path(configured)
    if environment is None:
        return DEFAULT_PATH
    home = Path(environment.get("HOME", str(Path.home())))
    return home / ".claude/config/models.conf"


def registry_text(registry: Path | None = None) -> str:
    return conf_reads.read_text(registry or registry_path())


def _shell_rows(rows: tuple[registry_rows.RegistryRow, ...]) -> str:
    return "".join(f"_models_table_{name}=(" + " ".join(shlex.quote(value) for value in values) + ")\n"
                   for name, values in (("sections", [row.section for row in rows]),
                                        ("keys", [row.key for row in rows]), ("values", [row.value for row in rows])))


def configuration_shell(configuration: LaunchModelConfiguration | RegistryEditBase) -> str:
    return (f"MODELS_CONFIG_FILE={shlex.quote(str(configuration.path))}\n"
            + f"MODELS_REGISTRY_BASE={shlex.quote(configuration.text)}\n"
            + f"{OPERATION_ENVIRONMENT[0]}={shlex.quote(_encoded(configuration))}\n"
            + _shell_rows(configuration.rows))


def _git(repo: Path, *arguments: str, input_text: str | None = None,
         environment: dict[str, str] | None = None) -> str:
    try:
        done = subprocess.run(["git", "-C", str(repo), *arguments], input=input_text, text=True,
                              capture_output=True, env=environment, timeout=60, check=False,
                              pass_fds=((build_folders.build_folder_lock_fd(Path(environment["GIT_INDEX_FILE"]).parent),)
                                        if environment is not None and "GIT_INDEX_FILE" in environment else ()))
    except subprocess.TimeoutExpired as error:
        raise OSError(f"git {' '.join(arguments)} timed out") from error
    if done.returncode:
        raise OSError(f"git {' '.join(arguments)}: {failure_detail(done)}")
    return done.stdout.strip()


def _publish_repository(path: Path) -> SharedRegistry | UnpublishedRegistry:
    if path.match(REGISTRY_PATH):
        found = main_publish.git(path.parent, "rev-parse", "--show-toplevel", "--absolute-git-dir")
        if not found.returncode:
            top_level, git_directory = found.stdout.splitlines()
            repo = Path(top_level)
            if path == repo.resolve() / REGISTRY_PATH:
                return SharedRegistry(repo, Path(git_directory))
    return UnpublishedRegistry()


def _fetch(repo: Path, branch: str) -> str:
    """Read the branch tip from its own remote ref; every fetch in the checkout writes FETCH_HEAD."""
    tracking = f"refs/remotes/origin/{branch}"
    _ = _git(repo, "fetch", "origin", f"+refs/heads/{branch}:{tracking}")
    return _git(repo, "rev-parse", "--verify", f"{tracking}^{{commit}}")


def _fast_forward(repo: Path, path: Path, target: str, *, registry_already_shared: bool = False,
                  expected_head: str = "", replay_registry: waiting_rows.RegistryReplay = registry_rows.replay_pending) -> None:
    with waiting_rows.locked_journal(waiting_rows.journal_path()) as pending:
        try:
            move = waiting_rows.move_target(repo, target, pending, fast_forward=True, allow_contained=True,
                                            expected_head=expected_head)
            if move.head == move.tip:
                return
            rows = waiting_rows.check_move(repo, move, pending, replace_registry=registry_already_shared,
                                           registry_move=True, replay_registry=replay_registry)
        except OSError as error:
            raise FastForwardCheckFailed(str(error)) from error
        try:
            waiting_rows.apply_move(repo, rows, pending)
        except OSError as error:
            raise OSError(f"registry fast-forward failed for {path}: {error}") from error


def _reconcile_shared(repo: Path, path: Path, target: str, expected_head: str) -> bool:
    try:
        _fast_forward(repo, path, target, registry_already_shared=True, expected_head=expected_head)
    except OSError as error:
        raise OSError(f"{path} is shared, but local reconciliation failed: {error}") from error
    return True


def _registry_commit(repo: Path, parent: str, contents: str, description: str) -> str:
    """Commit just the registry through an isolated index; never touch the user's index."""
    with build_folders.tool_folder("models-index") as directory:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        _ = _git(repo, "read-tree", parent, environment=environment)
        blob = _git(repo, "hash-object", "-w", "--stdin", input_text=contents)
        _ = _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{REGISTRY_PATH}",
                 environment=environment)
        tree = _git(repo, "write-tree", environment=environment)
        return _git(repo, "commit-tree", tree, "-p", parent, "-m", main_publish.REGISTRY_SUBJECT + ' '.join(description.splitlines()))


def _catalog_replay(before: str, written: str, catalog: list[registry_rows.RowChange]) -> waiting_rows.RegistryReplay:
    values = registry_rows.values(written)
    undo = registry_rows.changes(written, before)

    def replay(head: str, local: str, target: str) -> str:
        committed, current = registry_rows.values(head), registry_rows.values(local)
        catalog_text = registry_rows.replay(head, catalog, True)
        catalog_values = registry_rows.values(catalog_text)
        prior = [row for row in registry_rows.changes(catalog_text, head)
                 if current.get((row.section, row.key)) == catalog_values.get((row.section, row.key))]
        prior += [row for row in undo if current.get(key := (row.section, row.key)) == values.get(key) != committed.get(key)]
        ordinary = registry_rows.replay(local, prior, False)
        return registry_rows.replay(registry_rows.replay_pending(head, ordinary, target), catalog, True)

    return replay


def _publish(repo: Path, path: Path, description: str, replay_registry: waiting_rows.RegistryReplay, *,
             edit_after_fetch: Callable[[str], str] | None = None) -> bool:
    head_and_branch = _git(repo, "rev-parse", "HEAD", "--symbolic-full-name", "HEAD").splitlines()
    if len(head_and_branch) != 2 or not head_and_branch[1].startswith("refs/heads/"):
        raise OSError("registry publication refused: HEAD is not on a branch")
    base, reference = head_and_branch
    branch = reference.removeprefix("refs/heads/")
    try:
        remote = _fetch(repo, branch)
    except OSError as error:
        raise OSError(f"registry publication refused: fetch failed: {error}") from error
    try:
        _fast_forward(repo, path, remote, expected_head=base, replay_registry=replay_registry)
    except FastForwardCheckFailed as error:
        raise OSError(f"registry publication refused: fast-forward check failed: {error}") from error
    if foreign := _git(repo, "rev-list", "--abbrev-commit", f"{remote}..HEAD").split():
        raise OSError(f"registry publication refused: HEAD has {len(foreign)} commits that are not on "
                      + f"origin/{branch}: {' '.join(foreign[:5])}")
    last_error = ""
    for attempt in range(3):
        with waiting_rows.locked_journal(waiting_rows.journal_path()) as journal:
            journal.restore(repo, "HEAD")
            base = _git(repo, "rev-parse", "HEAD")
            contents = registry_text(path)
            if edit_after_fetch is not None:
                edited = edit_after_fetch(contents)
                if edited != contents:
                    write_text_atomic(path, edited)
                    contents = edited
        previous = _git(repo, "show", f"{base}:{REGISTRY_PATH}") + "\n"
        if not registry_rows.changes(previous, contents):
            return False
        commit = _registry_commit(repo, base, contents, description)
        try:
            landed = land_main.land(repo, commit, branch)
        except (OSError, land_main.LandError) as error:
            last_error = str(error)
            try:
                remote = _fetch(repo, branch)
                ancestor = _git(repo, "merge-base", commit, remote)
                remote_text = _git(repo, "show", f"{remote}:{REGISTRY_PATH}") + "\n"
                shared = ancestor == commit or (ancestor == base and not registry_rows.changes(
                    remote_text, replay_registry(previous, contents, remote_text)))
            except OSError as retry_error:
                last_error = str(retry_error)
                break
            if shared:
                return _reconcile_shared(repo, path, remote, base)
            try:
                if attempt < 2:
                    _fast_forward(repo, path, remote, expected_head=base, replay_registry=replay_registry)
            except OSError as retry_error:
                last_error = f"retry fast-forward check failed: {retry_error}"
                break
            continue
        if isinstance(landed.hook, land_main.HookFailed):
            print(land_main.hook_report(landed.hook), file=sys.stderr)
        return _reconcile_shared(repo, path, commit, base)
    raise OSError(f"{path} is edited but not shared; publish failed after at most 3 tries: {last_error}")


def _publish_lock_path(target: SharedRegistry | UnpublishedRegistry, path: Path) -> Path:
    if isinstance(target, SharedRegistry):
        return target.git_directory / "models-config.publish.lock"
    return path.with_name(path.name + ".lock")


def _on_main(target: SharedRegistry) -> bool:
    return _git(target.repository, "symbolic-ref", "--short", "HEAD") == "main"


def publish_pending(registry: Path) -> bool:
    """Publish rows that main does not have yet; return False at once when main is busy."""
    path = registry.resolve()
    target = _publish_repository(path)
    if (not isinstance(target, SharedRegistry) or path != DEFAULT_PATH.resolve()
            or os.environ.get("MODELS_CONFIG_PUBLISH") == "0"):
        return False
    head = _git(target.repository, "show", f"HEAD:{REGISTRY_PATH}") + "\n"
    if not waiting_rows.move_marker(target.repository).exists() and not registry_rows.changes(head, registry_text(path)):
        return False
    with ExitStack() as locks:
        if _on_main(target):
            try:
                locks.enter_context(main_push_lock.main_push_lock(target.repository, wait=False))
            except BlockingIOError:
                return False
        lock = locks.enter_context(_publish_lock_path(target, path).open("a"))
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _publish(target.repository, path, "rows written while main was busy", registry_rows.replay_pending)


class RegistryPublication(StrEnum):
    SHARED = "confirmed"
    DEFERRED = "deferred"
    LOCAL_ONLY = "disabled"
    UNCONFIRMED = "unconfirmed"


class SavedRegistry(NamedTuple):
    text: str
    changed: bool
    publication: RegistryPublication
    detail: str = ""


def store_registry(registry: Path, before: str, after: str, description: str, *,
                   additive_catalog: bool = False, preserve_producer_rows: bool = False) -> str:
    """Save the registry and report a publication failure to existing callers."""
    saved = save_registry(registry, before, after, description, additive_catalog=additive_catalog,
                          preserve_producer_rows=preserve_producer_rows)
    if saved.publication == RegistryPublication.UNCONFIRMED:
        raise OSError(f"the rows are live; main does not have them yet: {saved.detail}; the notes timer tries again")
    return saved.text


def save_registry(registry: Path, before: str, after: str, description: str, *,
                  additive_catalog: bool = False, preserve_producer_rows: bool = False) -> SavedRegistry:
    """Return the local save and the outcome of publication."""
    path = registry.resolve()
    changes = [row for row in registry_rows.changes(before, after)
               if not additive_catalog or row.section in ("codex.agents", "codex.families")]
    replay = registry_rows.replay_pending
    if preserve_producer_rows:
        keys = {(row.section, row.key) for row in changes}
        if any(section != "producers" for section, _ in keys) or keys & registry_rows.values(before).keys():
            raise ValueError("expected new producer rows")

        def replay_preserving_producers(head: str, local: str, target: str) -> str:
            existing = registry_rows.values(target)
            pending = [row for row in registry_rows.changes(head, local)
                       if (row.section, row.key) not in keys or (row.section, row.key) not in existing]
            return registry_rows.replay(target, pending, False)

        replay = replay_preserving_producers
    target = _publish_repository(path)
    lock_path = _publish_lock_path(target, path)
    with ExitStack() as locks:
        lock = locks.enter_context(lock_path.open("a"))
        fcntl.flock(lock, fcntl.LOCK_EX)
        if isinstance(target, SharedRegistry):
            journal = locks.enter_context(waiting_rows.locked_journal(waiting_rows.journal_path()))
            journal.restore(target.repository, "HEAD")
        latest = registry_text(path)
        if preserve_producer_rows:
            existing = registry_rows.values(latest)
            changes = [row for row in changes if (row.section, row.key) not in existing]
        result = registry_rows.replay(latest, changes, additive_catalog) if changes else latest
        if changes and additive_catalog:
            replay = _catalog_replay(latest, result, changes)
        changed = registry_rows.values(latest) != registry_rows.values(result)
        if changed:
            result = after if latest == before and not additive_catalog else result
            write_text_atomic(path, result)
    if not isinstance(target, SharedRegistry) or path != DEFAULT_PATH.resolve():
        return SavedRegistry(result, changed, RegistryPublication.LOCAL_ONLY)
    publish = os.environ.get("MODELS_CONFIG_PUBLISH")
    if publish == "0":
        return SavedRegistry(result, changed, RegistryPublication.LOCAL_ONLY)
    if publish == "later":
        return SavedRegistry(result, changed, RegistryPublication.DEFERRED)
    try:
        with ExitStack() as locks:
            if _on_main(target):
                locks.enter_context(main_push_lock.main_push_lock(target.repository))
            lock = locks.enter_context(lock_path.open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX)
            _ = _publish(target.repository, path, description, replay)
            result = registry_text(path)
    except (OSError, subprocess.SubprocessError) as error:
        return SavedRegistry(result, changed, RegistryPublication.UNCONFIRMED, " ".join(str(error).splitlines()))
    return SavedRegistry(result, changed, RegistryPublication.SHARED)


def _producer_candidate(name: str, family: str, model: str, effort: str) -> tuple[Path, str, str, LaunchSettings]:
    path = registry_path()
    before = registry_text(path)
    after = registry_rows.replay(before, [registry_rows.RowChange("producers", name, f"{name}={family}:{model}:{effort}")], False)
    settings = launch_settings("producers", name,
                               configuration=LaunchModelConfiguration(path, after, tuple(registry_rows.records(after))))
    return path, before, after, settings


def producer_settings(name: str, family: str, model: str, effort: str) -> LaunchSettings:
    """Validate one producer's candidate settings without a write."""
    return _producer_candidate(name, family, model, effort)[3]


def store_producer(name: str, family: str, model: str, effort: str) -> LaunchSettings:
    """Validate one producer's settings, then publish its row; an equal row changes nothing."""
    path, before, after, settings = _producer_candidate(name, family, model, effort)
    _ = store_registry(path, before, after, f"{name} producer uses {family}:{model}:{effort}")
    return settings


def rename_producer(old: str, new: str, profile: ProducerProfile, *, keep_old: bool = False) -> None:
    """Publish the new producer key and remove the old key when permitted."""
    path = registry_path()
    before = registry_text(path)
    rows = parse_registry(before).get("producers", {})
    if (not keep_old and old in rows and rows[old] != profile.value()) or (new in rows and rows[new] != profile.value()):
        raise ValueError(f"{path}: producer settings changed during rename")
    edits = [] if keep_old else [registry_rows.RowChange("producers", old, "")]
    edits.append(registry_rows.RowChange("producers", new, f"{new}={profile.value()}"))
    _ = store_registry(path, before, registry_rows.replay(before, edits, False), f"rename producer {old} to {new}")


def remove_producer(name: str, profile: ProducerProfile) -> None:
    """Remove the key after the lifecycle operation saves its value."""
    path = registry_path()
    before = registry_text(path)
    rows = parse_registry(before).get("producers", {})
    if name in rows and rows[name] != profile.value():
        raise ValueError(f"{path}: producer settings changed during retirement")
    edits = [registry_rows.RowChange("producers", name, "")]
    _ = store_registry(path, before, registry_rows.replay(before, edits, False), f"retire producer {name}")


def remove_failed_clone_profile(name: str, profile: ProducerProfile) -> None:
    """Remove a failed clone's profile from the registry that owns it."""
    path = registry_path().resolve()
    target = _publish_repository(path)
    if not isinstance(target, SharedRegistry) or path != DEFAULT_PATH.resolve():
        remove_producer(name, profile)
        return
    if os.environ.get("MODELS_CONFIG_PUBLISH") in ("0", "later"):
        raise OSError(f"{name}: cannot confirm removal while registry publication is disabled or deferred")

    def remove(text: str) -> str:
        rows = parse_registry(text).get("producers", {})
        if name not in rows:
            return text
        if rows[name] != profile.value():
            raise ValueError(f"{path}: {name}'s producer profile changed")
        return registry_rows.replay(text, [registry_rows.RowChange("producers", name, "")], False)

    def replay(head: str, local: str, shared: str) -> str:
        return registry_rows.replay_pending(head, remove(local), remove(shared))

    with ExitStack() as locks:
        if _on_main(target):
            locks.enter_context(main_push_lock.main_push_lock(target.repository))
        lock = locks.enter_context(_publish_lock_path(target, path).open("a"))
        fcntl.flock(lock, fcntl.LOCK_EX)
        _ = _publish(target.repository, path, f"remove failed producer clone {name}", replay, edit_after_fetch=remove)
        if name in parse_registry(registry_text(path)).get("producers", {}):
            raise OSError(f"{name}: the producer profile remains after publication")


@final
class Arguments(argparse.Namespace):
    command: str = ""
    registry: Path
    before: Path = Path("/dev/stdin")
    after: Path = Path()
    description: str = "update registry"
    additive_catalog: bool = False
    launch: bool = False
    edit: bool = False

    def __init__(self) -> None:
        super().__init__()
        self.registry = registry_path()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("store", "catalog", "shell"))
    _ = parser.add_argument("--launch", action="store_true")
    _ = parser.add_argument("--edit", action="store_true")
    _ = parser.add_argument("--registry", type=Path)
    _ = parser.add_argument("--before", type=Path, default=Path("/dev/stdin"))
    _ = parser.add_argument("--after", type=Path, default=Path())
    _ = parser.add_argument("--description", default="update registry")
    _ = parser.add_argument("--additive-catalog", action="store_true")
    arguments = parser.parse_args(namespace=Arguments())
    try:
        if arguments.command == "shell":
            if arguments.launch:
                configuration = LaunchModelConfiguration(*_snapshot(
                    os.environ, OPERATION_ENVIRONMENT[0], arguments.registry, synchronize=True))
            elif arguments.edit:
                configuration = RegistryEditBase(*_snapshot(
                    os.environ, OPERATION_ENVIRONMENT[1], arguments.registry, synchronize=True))
            else:
                configuration = RegistryEditBase(*_snapshot({}, "", arguments.registry))
            print(configuration_shell(configuration), end="")
            return 0
        conf_reads.record("models")
        before = arguments.before.read_text(encoding="utf-8")
        after = arguments.after.read_text(encoding="utf-8")
        if arguments.command == "catalog":
            print(registry_rows.catalog_text(before, after), end="")
        else:
            text = store_registry(arguments.registry, before, after, arguments.description,
                                  additive_catalog=arguments.additive_catalog)
            print(configuration_shell(RegistryEditBase(arguments.registry, text, tuple(registry_rows.records(text)))), end="")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
