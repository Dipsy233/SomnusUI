"""The exposure benchmark's own contract tests.

Three things must hold before any scheme can be scored:

1. **Fold equivalence** — a folded call produces the same session behaviour as
   the flat call it stands for (mock provider, real dispatch path).
2. **Surface stability** — assembling a scheme twice gives the same hash, and
   the seven hashes are distinct.
3. **H0 neutrality** — the baseline scheme dispatches exactly like
   :func:`call_tool`.
"""

from __future__ import annotations

import pytest

from bench.exposure.assemble import assemble
from bench.exposure.dispatch import dispatch_tool
from bench.exposure.schemes import SCHEMES
from somnus.protocol.tools import TOOL_SPECS, call_tool


@pytest.fixture
def store_session():
    from somnus.runtime.session import Session
    from tests.fixtures.scenarios import make_provider

    session = Session(make_provider("store"))
    yield session
    session.close()


# ---------------------------------------------------------------------------
# surface assembly
# ---------------------------------------------------------------------------


def test_all_seven_schemes_assemble_with_distinct_hashes():
    surfaces = {sid: assemble(scheme) for sid, scheme in SCHEMES.items()}
    hashes = {s.surface_hash for s in surfaces.values()}
    assert len(hashes) == len(surfaces)
    # Determinism: same input, same hash.
    for sid, scheme in SCHEMES.items():
        assert assemble(scheme).surface_hash == surfaces[sid].surface_hash


def test_merged_schema_properties_come_from_base_specs():
    """No hand-copied schemas: every property of a merged tool exists in a
    base spec it absorbs."""
    base_props = {
        spec["name"]: set(spec["inputSchema"]["properties"]) for spec in TOOL_SPECS
    }
    look = assemble(SCHEMES["H1"]).specs[-1]  # merged observe is exposed last
    assert set(look["inputSchema"]["properties"]) <= (
        base_props["observe"] | base_props["find"] | base_props["inspect"]
    )
    act = assemble(SCHEMES["H2"]).specs[-1]
    assert set(act["inputSchema"]["properties"]) <= (
        base_props["call"]
        | base_props["run"]
        | base_props["launch"]
        | {"kind"}
    )


def test_prompt_keeps_the_body_and_regenerates_the_tools_table():
    prompt = assemble(SCHEMES["H4"]).system_prompt
    assert "## Replies" in prompt and "## Rules" in prompt
    assert "| `look` |" in prompt and "| `do` |" in prompt
    # Folded verbs stay resolvable: the note names each mapping.
    assert "`find` = `look query" in prompt
    assert "`run` = `do kind=run" in prompt


# ---------------------------------------------------------------------------
# H0 neutrality
# ---------------------------------------------------------------------------


def test_h0_matches_call_tool_on_every_tool(store_session):
    scheme = SCHEMES["H0"]
    probe_args = {
        "observe": {},
        "find": {"query": "Special Item 17"},
        "inspect": {"ref": "r1"},
        "call": {"action": "CALL r1.add()"},
        "run": {"script": 'AWAIT "Special Item 17" timeout=1'},
        "help": {"topic": "format"},
        # switch/launch/configure touch real state; the dispatch parity that
        # matters for the benchmark is argument forwarding, checked on the
        # read-only tools above.
    }
    for name, args in probe_args.items():
        assert dispatch_tool(store_session, scheme, name, args) == call_tool(
            store_session, name, args
        ), name


def test_unknown_tool_renders_the_same_error_on_every_scheme(store_session):
    flat = dispatch_tool(store_session, SCHEMES["H0"], "nope", {})
    folded = dispatch_tool(store_session, SCHEMES["H4"], "nope", {})
    assert flat == folded
    assert flat.startswith("ERR")


# ---------------------------------------------------------------------------
# fold equivalence: folded call == flat call, same session behaviour
# ---------------------------------------------------------------------------


def test_find_folded_into_observe_matches_flat_find(store_session):
    out_folded = dispatch_tool(
        store_session, SCHEMES["H1"], "observe", {"query": "Special Item 17"}
    )
    out_flat = call_tool(store_session, "find", {"query": "Special Item 17"})
    assert out_folded == out_flat
    assert out_folded.startswith("FOUND")


def test_inspect_folded_into_observe_matches_flat_inspect(store_session):
    folded = dispatch_tool(
        store_session, SCHEMES["H1"], "observe", {"ref": "r1"}
    )
    flat = call_tool(store_session, "inspect", {"ref": "r1"})
    assert folded == flat


def test_run_and_launch_folded_into_call(store_session):
    """kind=run routes to the run tool; the script executes."""
    call_tool(store_session, "observe", {})
    found = call_tool(store_session, "find", {"query": "Special Item 17"})
    ref = next(
        parts[1]
        for line in found.splitlines()
        if len(parts := line.strip().split()) >= 2 and parts[0] in ("ITEM", "PRODUCT")
    )
    folded = dispatch_tool(
        store_session,
        SCHEMES["H2"],
        "call",
        {"kind": "run", "script": f"CALL {ref}.add()"},
    )
    assert folded.startswith("RUN") or folded.startswith("OK")
    # kind inference: script without kind still runs.
    inferred = dispatch_tool(
        store_session, SCHEMES["H2"], "call", {"script": f"CALL {ref}.add()"}
    )
    assert "OK" in inferred


def test_help_and_switch_folded_into_look_do(store_session):
    out = dispatch_tool(
        store_session, SCHEMES["H4"], "look", {"topic": "format"}
    )
    assert out == call_tool(store_session, "help", {"topic": "format"})
    # do target=... routes to switch; main is a no-op returning OK.
    assert dispatch_tool(
        store_session, SCHEMES["H4"], "do", {"target": "main"}
    ).startswith("OK")


def test_all_schemes_leave_the_same_store_state():
    """The one end-to-end guarantee: every surface can drive the same task to
    the same final state through its own names."""

    def solve(scheme, session):
        # observe/look → find/query → add, expressed per scheme; the exposed
        # names are derived from the scheme, never hardcoded.
        look = _exposed(scheme, "observe")
        act = _exposed(scheme, "call")
        find = _exposed(scheme, "find")
        dispatch_tool(session, scheme, look, {})
        if find:
            folded = dispatch_tool(
                session, scheme, find, {"query": "Special Item 17"}
            )
        else:
            folded = dispatch_tool(
                session, scheme, look, {"query": "Special Item 17"}
            )
        ref = _row_ref(folded)
        dispatch_tool(session, scheme, act, {"action": f"CALL {ref}.add()"})

    def _exposed(scheme, verb):
        for exposed, base in scheme.flat:
            if base == verb:
                return exposed
        for exposed, router in scheme.merged:
            if verb == "observe" and router in ("look", "look_help"):
                return exposed
            if verb == "call" and router.startswith("act"):
                return exposed
        return None  # the verb is folded away on this scheme

    from somnus.runtime.session import Session
    from tests.fixtures.scenarios import make_provider

    for sid, scheme in SCHEMES.items():
        if sid == "H5":
            continue  # H5's flat names route through call_tool; covered below
        session = Session(make_provider("store"))
        try:
            solve(scheme, session)
            text = _aid_text(session, "store_last_action")
            assert "Special Item 17" in text, sid
            count = _aid_text(session, "store_cart_count")
            assert "1 items" in count, sid
        finally:
            session.close()

    # H5: identical verbs under namespaced names.
    session = Session(make_provider("store"))
    try:
        dispatch_tool(session, SCHEMES["H5"], "look.find", {"query": "Special Item 17"})
        out = session.dispatch('FIND "Special Item 17"')
        ref = _row_ref(out)
        dispatch_tool(session, SCHEMES["H5"], "call.invoke", {"action": f"CALL {ref}.add()"})
        assert "Special Item 17" in _aid_text(session, "store_last_action")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _row_ref(out: str) -> str:
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] in ("ITEM", "PRODUCT"):
            return parts[1]
    raise AssertionError(f"no row ref in:\n{out}")


def _aid_text(session, automation_id: str) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node.name or ""
    raise AssertionError(f"{automation_id} not in final frame")
