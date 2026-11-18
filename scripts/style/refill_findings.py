"""Store finding proofs and their immutable source dependencies."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import cast

from scripts.style.refill_facts import RefillFacts
from scripts.style.refill_inputs import UnstoredFact, input_identity


FINDING_READER_VERSION = 1
FINDING_RESULT_VERSION = 4
OBJECT_GROUP_VERSION = 1
OBJECT_GROUP_PREFIX = "group:"


def proof_types(*namespaces: Mapping[str, object]) -> dict[str, Callable[..., object]]:
    """Register the constructors that a caller's semantic readers use."""
    result: dict[str, Callable[..., object]] = {}
    for namespace in namespaces:
        for value in namespace.values():
            if isinstance(value, type) and (is_dataclass(value) or issubclass(value, Enum)):
                result[value.__module__ + "." + value.__qualname__] = value
    return result


@dataclass(frozen=True)
class FindingFootprint:
    """The ordered source paths and uncertain module paths for one finding view."""

    files: tuple[str, ...]
    uncertain: tuple[str, ...]


def encode_proof(value: object) -> str:
    """Encode only values from the existing proof types."""
    def encode(item: object) -> object:
        if isinstance(item, Enum):
            return {"enum": type(item).__module__ + "." + type(item).__qualname__, "value": cast(object, item.value)}
        if isinstance(item, Path):
            return {"path": str(item)}
        if is_dataclass(item) and not isinstance(item, type):
            return {"type": type(item).__module__ + "." + type(item).__qualname__,
                    "fields": {field.name: encode(cast(object, getattr(item, field.name))) for field in fields(item)}}
        if isinstance(item, (tuple, list, frozenset, set)):
            kind = "tuple" if isinstance(item, tuple) else "set" if isinstance(item, (frozenset, set)) else "list"
            return {kind: [encode(part) for part in cast(tuple[object, ...], item)]}
        if isinstance(item, dict):
            return {"map": [[encode(key), encode(part)] for key, part in cast(dict[object, object], item).items()]}
        return item
    return json.dumps(encode(value), separators=(",", ":"), ensure_ascii=False)


def decode_proof(source: str, types: Mapping[str, Callable[..., object]]) -> object:
    """Restore registered proof types without importing stored names."""
    def decode(item: object) -> object:
        if not isinstance(item, dict):
            return item
        data = cast(dict[str, object], item)
        if "path" in data:
            return Path(cast(str, data["path"]))
        if "enum" in data:
            return types[cast(str, data["enum"])](data["value"])
        if "type" in data:
            arguments = cast(dict[str, object], data["fields"])
            return types[cast(str, data["type"])](**{name: decode(part) for name, part in arguments.items()})
        for key in ("tuple", "set", "list"):
            if key in data:
                parts = [decode(part) for part in cast(list[object], data[key])]
                return tuple(parts) if key == "tuple" else frozenset(parts) if key == "set" else parts
        return {decode(key): decode(part) for key, part in cast(list[tuple[object, object]], data["map"])}
    return decode(cast(object, json.loads(source)))


@dataclass(frozen=True)
class StoredFindingProof:
    """A result whose source versions and required objects still match."""

    result: str
    objects: tuple[str, ...]
    approvals: tuple[tuple[str, bool], ...]


class SearchIndex:
    """The search work of one ledger, shared by all of its write batches."""

    def __init__(self) -> None:
        self.word_blobs: dict[tuple[tuple[str, ...], str], set[str]] = {}
        self.indexed_words: set[tuple[str, tuple[str, ...]]] = set()
        self.indexed_domains: set[tuple[str, tuple[str, ...]]] = set()
        # Object groups that a committed batch or a ledger read holds.
        self.stored_groups: set[str] = set()


class FindingResults:
    """Own each finding's result at a named revision view."""

    def __init__(self, facts: RefillFacts, search: SearchIndex) -> None:
        self.facts: RefillFacts = facts
        self.search: SearchIndex = search
        self.published_groups: set[str] = set()

    def word_index(self, blob: str, locale: tuple[str, ...], calculate: Callable[[], frozenset[str]]) -> None:
        """Queue each new word index once for each ledger."""
        key = (blob, locale)
        if key in self.search.indexed_words:
            return
        if isinstance(self.facts.input_fact("finding_words", key, 2), UnstoredFact):
            words = calculate()
            for word in words:
                self.search.word_blobs.setdefault((locale, word), set()).add(blob)
            locale_key = input_identity(locale)
            def publish() -> None:
                _ = self.facts.connection.executemany(
                    "INSERT OR IGNORE INTO refill_objects "
                    + "(object_store,object_id,reader_version,fact_kind,fact_key,line_count,declaration_name) "
                    + "VALUES (?,?,2,'finding_word',?,0,?)",
                    ((self.facts.object_store, blob, locale_key + ":" + word, word) for word in words))
                self.facts.store_input("finding_words", key, 2, "complete")
            self.facts.publish(publish)
        self.search.indexed_words.add(key)

    def blobs_with_word(self, word: str, locale: tuple[str, ...], blobs: frozenset[str]) -> frozenset[str]:
        """Return the blobs of one search domain whose word index holds word."""
        rows = cast(list[tuple[str]], self.facts.connection.execute(
            "SELECT object_id FROM refill_objects WHERE object_store=? AND reader_version=2 "
            + "AND fact_kind='finding_word' AND declaration_name=? AND fact_key=?",
            (self.facts.object_store, word, input_identity(locale) + ":" + word)).fetchall())
        found = {blob for (blob,) in rows}
        found.update(self.search.word_blobs.get((locale, word), ()))
        return frozenset(found & blobs)

    def publish_group(self, name: str, members: tuple[str, ...]) -> None:
        """Store the members of one immutable object group once for each write batch."""
        if name in self.published_groups or name in self.search.stored_groups:
            return
        self.published_groups.add(name)
        self.facts.publish_input("object_group", name, OBJECT_GROUP_VERSION, json.dumps(members))

    def group_members(self, name: str) -> tuple[str, ...] | UnstoredFact:
        """Read the members of a stored object group."""
        stored = self.facts.input_fact("object_group", name, OBJECT_GROUP_VERSION)
        if isinstance(stored, UnstoredFact):
            return stored
        self.search.stored_groups.add(name)
        return tuple(cast(list[str], json.loads(stored)))

    def key(self, finding: str, view: str) -> str:
        return json.dumps((self.facts.catalog, finding, view), separators=(",", ":"), ensure_ascii=False)

    def prepare(self, findings: tuple[str, ...], view: str, available: Callable[[tuple[str, ...]], bool]) -> None:
        """Verify the observation's required objects in one Git batch."""
        rows = cast(list[tuple[str]], self.facts.connection.execute(
            "SELECT DISTINCT objects.value FROM refill_findings f,json_each(f.objects) objects "
            + "WHERE f.catalog=? AND f.view=? AND f.finding_id IN (SELECT value FROM json_each(?))",
            (self.facts.catalog, view, json.dumps(findings))).fetchall())
        _ = available(tuple(name for (name,) in rows))

    def prepare_footprints(self, available: Callable[[tuple[str, ...]], bool]) -> None:
        """Verify the source objects for the footprint observation together."""
        rows = cast(list[tuple[str]], self.facts.connection.execute(
            "SELECT DISTINCT objects.value FROM refill_findings f,json_each(f.objects) objects "
            + "WHERE f.catalog=? AND f.view LIKE 'footprint:%'", (self.facts.catalog,)).fetchall())
        _ = available(tuple(name for (name,) in rows))

    def load(self, finding: str, view: str, identity: object,
             available: Callable[[tuple[str, ...]], bool],
             approved: Callable[[Path], bool]) -> StoredFindingProof | UnstoredFact:
        row = cast(tuple[str, int, str, str, str] | None, self.facts.connection.execute(
            "SELECT input_identity,dirty_version,result,objects,approvals FROM refill_findings "
            + "WHERE consumer_key=?", (self.key(finding, view),)).fetchone())
        if row is None or row[0] != input_identity((FINDING_RESULT_VERSION, identity)) or row[1]:
            return UnstoredFact()
        objects = tuple(cast(list[str], json.loads(row[3])))
        approvals = tuple((path, answer) for path, answer in cast(list[tuple[str, bool]], json.loads(row[4])))
        if not available(objects) or any(approved(Path(path)) != answer for path, answer in approvals):
            return UnstoredFact()
        return StoredFindingProof(row[2], objects, approvals)

    def store(self, finding: str, view: str, identity: object, result: object,
              versions: tuple[tuple[str, int], ...], edges: tuple[tuple[str, str, str], ...],
              objects: tuple[str, ...], approvals: tuple[tuple[str, bool], ...], *,
              crate: str = "", old_name: str = "", new_name: str = "", owner: str = "",
              footprint: tuple[str, ...] = ()) -> None:
        key = self.key(finding, view)
        encoded = encode_proof(result)
        def publish() -> None:
            if versions != self.facts.source_versions():
                return
            with self.facts.publication():
                _ = self.facts.connection.execute(
                    "INSERT OR REPLACE INTO refill_findings "
                    + "(consumer_key,catalog,finding_id,view,input_identity,dirty_version,result,objects,approvals,"
                    + "crate,old_name,new_name,owner,footprint) VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?,?)",
                    (key, self.facts.catalog, finding, view, input_identity((FINDING_RESULT_VERSION, identity)),
                     encoded, json.dumps(objects), json.dumps(approvals, ensure_ascii=False),
                     crate, old_name, new_name, owner, json.dumps(footprint, ensure_ascii=False)))
                self.facts.dependencies("refill_findings", key, edges)
        self.facts.publish(publish)

    def source_fact[T](self, kind: str, identity: object, calculate: Callable[[], T],
                       decode: Callable[[str], T]) -> T:
        """Share immutable comparisons and compact source facts."""
        stored = self.facts.input_fact(kind, identity, FINDING_READER_VERSION)
        if not isinstance(stored, UnstoredFact):
            return decode(stored)
        result = calculate()
        self.facts.publish_input(kind, identity, FINDING_READER_VERSION, encode_proof(result))
        return result
