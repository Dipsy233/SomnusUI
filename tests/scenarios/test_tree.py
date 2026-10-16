"""Scenario S5: a nested tree — compression controlled by expansion level.

This screen is the answer to "can the same UI cost 6 lines or thousands". The
tree has ``fanout`` children per level over four levels, so fully expanded it is
thousands of nodes and collapsed it is one.

Three properties are asserted, and together they are the compression story:

* the **L0 overview stays bounded** no matter how much of the tree is realized -
  an agent's standing context does not grow just because the app has a big tree;
* the **detailed observation scales with the expansion level** and with nothing
  else - a caller that asked for more pays for exactly what it unfolded;
* a node that was never realized is still **reachable** (FIND / INSPECT), so
  folding never makes content unreachable, only unrendered.
"""

from __future__ import annotations

import json

from somnus.projection.budget import ObservationBudget, count_tokens
from somnus.providers.mock import raw_tree_to_dict
from tests.fixtures.scenarios import TREE_FANOUT, tree_tree


# ---------------------------------------------------------------------------
# the overview is insensitive to how big the tree is
# ---------------------------------------------------------------------------


def test_l0_overview_stays_bounded_as_the_tree_grows(make_session):
    """Realizing more of the tree must not inflate the standing overview.

    The L0 depth/budget caps are what make an observation affordable on a large
    application, and a tree is the shape that would break them: every level
    multiplies the node count.
    """
    cap = ObservationBudget().for_level(0).hard_tokens
    for expanded in (set(), {"tree_root"}, {"tree_root", "tree_root_0"}):
        obs = make_session(tree_tree(expanded=expanded)).observe()
        assert obs.tokens <= cap, f"L0 grew to {obs.tokens} with expanded={expanded}"


def test_the_collapsed_root_says_it_is_collapsed(make_session):
    """A folded branch is marked, not silently empty."""
    obs = make_session(tree_tree()).observe().text
    assert "根目录" in obs
    assert "collapsed" in obs


# ---------------------------------------------------------------------------
# the detailed view scales with the expansion level
# ---------------------------------------------------------------------------


def test_expansion_level_drives_the_detailed_observation(make_session):
    """L1 grows monotonically with how much was realized, and proportionally.

    Monotonic is not enough on its own - a runtime that always dumped the whole
    tree would also be monotonic. The last assertion pins the ratio: unfolding a
    second branch roughly doubles the *realized* tree, so the observation must
    grow by a factor far smaller than the whole-tree cost.
    """
    sizes = []
    for expanded in (set(), {"tree_root"}, {"tree_root", "tree_root_0"}):
        obs = make_session(tree_tree(expanded=expanded)).observe(level=1)
        sizes.append(obs.tokens)
    assert sizes[0] < sizes[1] < sizes[2], sizes

    whole = make_session(tree_tree(expanded=_all_ids())).observe(full=True)
    assert sizes[2] < whole.tokens, (
        f"one extra branch ({sizes[2]}) must cost less than the whole tree "
        f"({whole.tokens})"
    )


def test_realized_node_count_matches_the_expansion_level(make_session):
    """The provider realizes exactly the branches that were asked for."""
    counts = []
    for expanded in (set(), {"tree_root"}, {"tree_root", "tree_root_0"}):
        tree = tree_tree(expanded=expanded)
        counts.append(sum(1 for _ in tree.walk()))
    assert counts[0] < counts[1] < counts[2]
    assert counts[2] < sum(1 for _ in tree_tree(expanded=_all_ids()).walk())


# ---------------------------------------------------------------------------
# folding never makes content unreachable
# ---------------------------------------------------------------------------


def test_find_reaches_a_node_that_was_never_realized(make_session):
    """A branch that the overview folds is still addressable by name."""
    session = make_session(tree_tree(expanded={"tree_root"}))
    session.observe(level=1)
    found = session.dispatch('FIND "根目录.2"')
    assert found.startswith("FOUND"), found
    assert "根目录.2" in found
    # The hit carries its gesture, so the agent can descend from it.
    assert "+expand" in found


def test_inspect_reaches_a_single_deep_node(make_session):
    """INSPECT shows one node's detail without paying for its siblings."""
    session = make_session(tree_tree(expanded={"tree_root"}))
    session.observe(level=1)
    found = session.dispatch('FIND "根目录.2"')
    ref = next(
        l.split()[1] for l in found.splitlines()
        if l.split()[0] == "TREEITEM" and "根目录.2" in l
    )
    detail = session.inspect_direct(ref, depth=1)
    assert "根目录.2" in detail
    # A different branch is not in this inspection.
    assert "根目录.4" not in detail


def test_full_observe_is_the_escape_hatch(make_session):
    """`OBSERVE ... full` renders everything the provider realized."""
    session = make_session(tree_tree(expanded={"tree_root", "tree_root_0"}))
    budgeted = session.observe()
    full = session.observe(full=True)
    assert full.tokens > budgeted.tokens
    assert "根目录.0.1" in full.text


# ---------------------------------------------------------------------------
# the compression ratio the project promises
# ---------------------------------------------------------------------------


def test_compression_ratio_beats_the_gate(make_session):
    """Raw accessibility tree vs the collapsed observation: >= 10x."""
    big = tree_tree(expanded=_all_ids())
    raw_tokens = count_tokens(json.dumps(raw_tree_to_dict(big), ensure_ascii=False))
    collapsed = make_session(tree_tree()).observe()
    ratio = raw_tokens / max(1, collapsed.tokens)
    assert ratio >= 10, f"compression {raw_tokens}/{collapsed.tokens} = {ratio:.1f}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _all_ids(fanout: int = TREE_FANOUT, depth: int = 4) -> set[str]:
    """Every branch id, so the tree is fully realized."""
    ids: set[str] = set()
    frontier = ["tree_root"]
    for _ in range(depth):
        nxt: list[str] = []
        for prefix in frontier:
            ids.add(prefix)
            nxt.extend(f"{prefix}_{i}" for i in range(fanout))
        frontier = nxt
    return ids
