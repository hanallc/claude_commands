"""Compare the ledger readers with the binary supplied by the Rust integration test."""

from __future__ import annotations

import json
import locale
import os
import sys
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from scripts.style import style_config, style_ledger, style_reader


@dataclass(frozen=True)
class SearchAnswer:
    name: str
    pathspecs: tuple[str, ...]
    maintained: bool
    paths: tuple[str, ...]
    failure: str


@dataclass(frozen=True)
class LedgerReading:
    tip: style_ledger.RepositoryTip
    cached_tip: style_ledger.RepositoryTip
    entries: dict[str, dict[str, style_ledger.TreeEntry]]
    searches: tuple[SearchAnswer, ...]


def ledger_reading(repo: Path, root: Path, source: style_config.ReaderSource) -> LedgerReading:
    """Read the same inventory and searches through the selected implementation."""
    settings = root / "style_reader.conf"
    _ = settings.write_text(f"# one of: python, rust\nreader={source.value}\n")
    read_tip = cast(Callable[[Path, str], style_ledger.RepositoryTip], vars(style_ledger)["_tip"])
    tip = read_tip(repo, "HEAD")
    cached_tip = style_ledger.GitReadCache(repo).tip(tip.commit)
    entries = {revision: style_ledger.tree_entries(repo, revision)
               for revision in ("HEAD", "HEAD~0", tip.commit[:12], tip.tree, "HEAD:nested")}
    searches: list[SearchAnswer] = []
    for maintained in (False, True):
        with style_ledger.Ledger(repo, root / f"{source.value}-{maintained}-{os.environ['LC_ALL']}.sqlite") as ledger:
            observation = cast(Callable[[], AbstractContextManager[None]], getattr(ledger, "_finding_observation"))
            with observation() if maintained else nullcontext():
                for pathspecs in (("*.rs",), ("*.rs", "*.md")):
                    for name in ("shared", "working", "café", "引数", "absent_word", "r#type", "T", "T²", "²shared", "x", "xⅧ"):
                        try:
                            paths = ledger.files_naming(tip.commit, name, pathspecs)
                            answer = SearchAnswer(name, pathspecs, maintained, paths, "")
                        except style_ledger.GitReadFailed as error:
                            answer = SearchAnswer(name, pathspecs, maintained, (), type(error).__name__)
                        searches.append(answer)
    return LedgerReading(tip, cached_tip, entries, tuple(searches))


def main(repo: Path, binary: Path) -> None:
    """Print both readings without building or changing the target worktree."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        previous = {key: os.environ.get(key) for key in ("CONF_DIR", "LC_ALL")}
        os.environ["CONF_DIR"] = str(root)
        try:
            unicode_locale = ""
            for candidate in ("C.UTF-8", "en_US.UTF-8", "UTF-8"):
                try:
                    unicode_locale = locale.setlocale(locale.LC_CTYPE, candidate)
                    break
                except locale.Error:
                    continue
            if not unicode_locale:
                raise RuntimeError("the parity test requires a UTF-8 locale")
            readings: dict[str, dict[str, object]] = {}
            with style_reader.use_client(style_reader.ReaderClient(binary)):
                for source in style_config.ReaderSource:
                    readings[source.value] = {}
                    for selected_locale in ("C", unicode_locale):
                        os.environ["LC_ALL"] = selected_locale
                        readings[source.value][selected_locale] = asdict(ledger_reading(repo, root, source))
            print(json.dumps(readings, ensure_ascii=False))
        finally:
            for key, value in previous.items():
                if value is None:
                    _ = os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
