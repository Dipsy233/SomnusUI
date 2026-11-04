"""Session Registry: stable refs across revisions.

The v0.1 mistake was binding refs to native (RuntimeId) objects, so every repaint
invalidated everything. Here refs are bound to *fingerprints*:

1. After each compilation, new nodes are matched to existing refs by fingerprint.
2. A unique fingerprint match keeps the ref (RuntimeId is a fast path only).
3. Zero matches -> STALE_TARGET; multiple *live* matches -> TARGET_AMBIGUOUS.
4. Refs are never recycled within a session.

**Fingerprints are scoped per window.** A fingerprint is computed from the
element's own role/name/identity plus its ancestor path - all of which describe
*this window's* tree. Two different windows can produce the same fingerprint
(two copies of the same form, two Explorer windows on one folder), and a
session that switches target must not let one window's record answer for the
other. Binding by bare fingerprint made a call on the new window silently
re-bind to the old window's native element (the "closed the form, reopened it,
old q1 wrote into the new instance" failure). The namespace here is
``(window_id, fingerprint)``; refs stay globally unique because one allocator
serves the whole session, and records are retained so a stale ref still
reports what it used to be.

A subtlety this class must survive: a fingerprint can legitimately be
allocated several refs (the element appeared twice in one tree - e.g. a
tray flyout holding a hidden copy of a taskbar button - and later closed).
When that happens the duplicated fingerprint must collapse back to one
ref as soon as only one live node carries it, otherwise the stale
duplicates poison the mapping forever and every subsequent call on
a perfectly unambiguous node fails with TARGET_AMBIGUOUS.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from somnus.compiler.compile import CompileResult, RefAllocator
from somnus.ir.node import SemanticNode


@dataclass(slots=True)
class RefRecord:
    """Everything the registry remembers about one session ref."""

    ref: str
    fingerprint: str
    entity: str | None
    role: str
    name: str | None
    #: The window this ref's fingerprint belongs to. Matching is scoped to it,
    #: so a record can never answer for a different window's identical tree.
    window_id: str = ""
    #: Number of times this fingerprint has been observed (for ambiguity).
    seen: int = 1
    #: Last reconcile pass in which this record matched a live node.
    last_seen: int = 0


@dataclass(slots=True)
class ReconcileReport:
    kept: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)


class SessionRegistry:
    """Maintains the ref <-> fingerprint mapping for one session/window."""

    def __init__(self) -> None:
        self.allocator = RefAllocator()
        self.records: dict[str, RefRecord] = {}
        self.by_fingerprint: dict[tuple[str, str], list[str]] = {}
        self._reconcile_counter = 0
        #: Window id -> refs that resolved to a live node in its last pass.
        #: Scoped per window: a pass over w2 must not prune w1's live set, or
        #: switching back would re-mint every ref of w1.
        self._live_refs: dict[str, set[str]] = {}

    # -- reconciliation -----------------------------------------------------

    def reconcile(self, result: CompileResult) -> ReconcileReport:
        """Re-map freshly compiled nodes onto existing refs.

        Any node whose fingerprint matches a known record *uniquely* reuses that
        record's ref. Nodes with no match are new refs. The compiler has already
        allocated refs; we rewrite them here and update the result's lookup maps
        so downstream code sees the reconciled refs.

        Matching is scoped to ``result.window_id``: the same fingerprint in
        another window is a different element and must not share a ref.
        """
        self._reconcile_counter += 1
        report = ReconcileReport()
        window_id = _window_of(result)
        # Drop refs of *this window* that died in an earlier pass: they must not
        # participate in uniqueness decisions, or a fingerprint whose element
        # vanished and returned (tray flyout, minimized window) stays ambiguous
        # forever. Other windows' live sets are untouched.
        live = self._live_refs.get(window_id, set())
        for key in list(self.by_fingerprint):
            if key[0] != window_id:
                continue
            refs = [r for r in self.by_fingerprint[key] if r in live]
            if refs:
                self.by_fingerprint[key] = refs
            else:
                del self.by_fingerprint[key]
        # How many nodes in the *current* tree carry each fingerprint. Only a
        # fingerprint with >1 live node is genuinely ambiguous.
        fp_counts = Counter(node.fingerprint for node in result.root.walk())
        rewritten: dict[str, str] = {}
        #: refs already claimed during *this* reconcile pass. A fingerprint may
        #: legitimately repeat (anonymous panes), so a ref can be reused by only
        #: one node per pass.
        claimed: set[str] = set()

        for node in result.root.walk():
            old_ref = node.ref
            fp = node.fingerprint
            key = (window_id, fp)
            known = self.by_fingerprint.get(key, [])
            if len(known) > 1 and fp_counts[fp] == 1:
                # False ambiguity: earlier passes allocated several refs for
                # this fingerprint (a node present twice in one tree, e.g. a
                # tray flyout that has since closed) but the current tree
                # holds exactly one node with it. Collapse the duplicates to
                # the record seen most recently instead of failing forever.
                candidates = [self.records[r] for r in known if r in self.records]
                if candidates:
                    record = max(candidates, key=lambda rec: rec.last_seen)
                    if record.entity == node.entity and record.role == node.role:
                        ref = record.ref
                        record.seen += 1
                        record.name = node.name
                        record.last_seen = self._reconcile_counter
                        claimed.add(ref)
                        report.kept.append(ref)
                        self.by_fingerprint[key] = [ref]
                        if ref != old_ref:
                            rewritten[old_ref] = ref
                            self._rewrite_ref(node, old_ref, ref, result)
                        continue
            if len(known) == 1 and known[0] not in claimed:
                ref = known[0]
                record = self.records[ref]
                if record.entity == node.entity and record.role == node.role:
                    record.seen += 1
                    record.name = node.name
                    record.last_seen = self._reconcile_counter
                    claimed.add(ref)
                    report.kept.append(ref)
                    if ref != old_ref:
                        rewritten[old_ref] = ref
                        self._rewrite_ref(node, old_ref, ref, result)
                    continue
            if len(known) > 1 and fp_counts[fp] > 1:
                # Genuinely ambiguous: more than one live node shares this
                # fingerprint; do NOT silently rebind. Flag it for the caller.
                report.ambiguous.append(fp)
            # Adopt the compiler-allocated ref when it is unused; otherwise
            # allocate a fresh one (refs are never recycled within a session).
            if old_ref and old_ref not in self.records and old_ref not in claimed:
                new_ref = old_ref
            else:
                new_ref = self._fresh_ref(node)
            claimed.add(new_ref)
            if new_ref != old_ref:
                rewritten[old_ref] = new_ref
                self._rewrite_ref(node, old_ref, new_ref, result)
            self.records[new_ref] = RefRecord(
                ref=new_ref,
                fingerprint=fp,
                entity=node.entity,
                role=node.role,
                name=node.name,
                window_id=window_id,
                last_seen=self._reconcile_counter,
            )
            self.by_fingerprint.setdefault(key, []).append(new_ref)
            report.new.append(new_ref)

        # Remember which refs of *this window* are live so the next pass can
        # prune its dead ones. Other windows keep their own sets.
        self._live_refs[window_id] = set(claimed)

        # Stale = this window's records not seen this round (their refs stay
        # reserved forever). A record belonging to another window is not stale
        # from this window's point of view - it is simply not part of this tree.
        seen_refs = {
            r
            for refs in (
                self.by_fingerprint.get((window_id, n.fingerprint), [])
                for n in result.root.walk()
            )
            for r in refs
        }
        report.stale = [
            r
            for r, rec in self.records.items()
            if rec.window_id == window_id and r not in seen_refs
        ]
        return report

    def _fresh_ref(self, node: SemanticNode) -> str:
        prefix = _prefix_of(node.ref)
        self.allocator.observe_existing(node.ref)
        return self.allocator.next(prefix)

    def _rewrite_ref(
        self, node: SemanticNode, old: str, new: str, result: CompileResult
    ) -> None:
        node.ref = new
        if old in result.sources:
            result.sources[new] = result.sources.pop(old)
        if old in result.fingerprints:
            result.fingerprints[new] = result.fingerprints.pop(old)
        if old in result.members_by_ref:
            result.members_by_ref[new] = result.members_by_ref.pop(old)
        # member ref lists point at child refs; swap any that referenced old.
        for members in result.members_by_ref.values():
            for key, value in list(members.items()):
                if value == old:
                    members[key] = new

    # -- queries ------------------------------------------------------------

    def fingerprint_of(self, ref: str) -> str | None:
        record = self.records.get(ref)
        return record.fingerprint if record else None

    def is_stale(self, ref: str, current_fingerprints: set[str]) -> bool:
        fp = self.fingerprint_of(ref)
        if fp is None:
            return True
        return fp not in current_fingerprints

    def candidates_for(self, fingerprint: str, window_id: str | None = None) -> list[str]:
        """Refs that carry ``fingerprint``, optionally within one window.

        Without ``window_id`` the answer spans every window this session has
        seen, which is only useful for diagnostics; a caller that is about to
        act on one of them must scope it, because the same fingerprint in
        another window names a different native element.
        """
        if window_id is not None:
            return list(self.by_fingerprint.get((window_id, fingerprint), ()))
        out: list[str] = []
        for (_window, fp), refs in self.by_fingerprint.items():
            if fp == fingerprint:
                out.extend(refs)
        return out


def _window_of(result: object) -> str:
    """The window namespace of a compilation, without a hard attribute dependency.

    ``CompileResult`` carries ``window_id`` (set from the provider's
    ``RawTree.window``), which is what every production caller passes. A
    ``RawTree`` carries the same fact one level down (``window.window_id``);
    accepting it here keeps the registry usable if a caller hand-builds a tree,
    and an object that carries neither falls back to one shared namespace
    (``""``), which is exactly the pre-P2-6 single-window behaviour rather than
    an ``AttributeError``.
    """
    window_id = getattr(result, "window_id", None)
    if window_id:
        return str(window_id)
    window = getattr(result, "window", None)
    nested = getattr(window, "window_id", None)
    return str(nested) if nested else ""


def _prefix_of(ref: str) -> str:
    i = 0
    while i < len(ref) and ref[i].isalpha():
        i += 1
    return ref[:i] or "n"
