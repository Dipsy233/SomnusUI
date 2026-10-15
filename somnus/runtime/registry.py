"""Session Registry: stable refs across revisions (dev doc section 8).

The v0.1 mistake was binding refs to native (RuntimeId) objects, so every repaint
invalidated everything. Here refs are bound to *fingerprints*:

1. After each compilation, new nodes are matched to existing refs by fingerprint.
2. A unique fingerprint match keeps the ref (RuntimeId is a fast path only).
3. Zero matches -> STALE_TARGET; multiple *live* matches -> TARGET_AMBIGUOUS.
4. Refs are never recycled within a session.

A subtlety this class must survive: a fingerprint can legitimately be
allocated several refs (the element appeared twice in one tree - e.g. a
tray flyout holding a hidden copy of a taskbar button - and later closed).
When that happens the duplicated fingerprint must collapse back to one
ref as soon as only one live node carries it, otherwise the stale
duplicates poison ``by_fingerprint`` forever and every subsequent call on
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
        self.by_fingerprint: dict[str, list[str]] = {}
        self._reconcile_counter = 0
        #: Refs that resolved to a live node in the last reconcile pass.
        self._live_refs: set[str] = set()

    # -- reconciliation -----------------------------------------------------

    def reconcile(self, result: CompileResult) -> ReconcileReport:
        """Re-map freshly compiled nodes onto existing refs.

        Any node whose fingerprint matches a known record *uniquely* reuses that
        record's ref. Nodes with no match are new refs. The compiler has already
        allocated refs; we rewrite them here and update the result's lookup maps
        so downstream code sees the reconciled refs.
        """
        self._reconcile_counter += 1
        report = ReconcileReport()
        # Drop refs that died in an earlier pass: they must not participate
        # in uniqueness decisions, or a fingerprint whose element vanished
        # and returned (tray flyout, minimized window) stays ambiguous forever.
        if self._live_refs:
            for fp in list(self.by_fingerprint):
                refs = [r for r in self.by_fingerprint[fp] if r in self._live_refs]
                if refs:
                    self.by_fingerprint[fp] = refs
                else:
                    del self.by_fingerprint[fp]
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
            known = self.by_fingerprint.get(fp, [])
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
                        self.by_fingerprint[fp] = [ref]
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
                last_seen=self._reconcile_counter,
            )
            self.by_fingerprint.setdefault(fp, []).append(new_ref)
            report.new.append(new_ref)

        # Remember which refs are live so the next pass can prune dead ones.
        self._live_refs = set(claimed)

        # Stale = records not seen this round (their refs stay reserved forever).
        seen_refs = {
            r for refs in (self.by_fingerprint.get(n.fingerprint, []) for n in result.root.walk()) for r in refs
        }
        report.stale = [r for r in self.records if r not in seen_refs]
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

    def candidates_for(self, fingerprint: str) -> list[str]:
        return list(self.by_fingerprint.get(fingerprint, ()))


def _prefix_of(ref: str) -> str:
    i = 0
    while i < len(ref) and ref[i].isalpha():
        i += 1
    return ref[:i] or "n"
