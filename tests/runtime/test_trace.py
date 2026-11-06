"""Stage 1: decision trace links, storage and the text timeline."""

from __future__ import annotations

import json
from pathlib import Path

from somnus.observability.jsonl import (
    JsonlTraceSink,
    resolve_trace_root,
    trace_path,
)
from somnus.observability.trace import MemoryTraceSink
from somnus.observability.view import render_timeline
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.fixtures.builders import demo_scripts, restaurant_demo_tree


def _new_session() -> Session:
    provider = MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    return Session(provider)


def test_default_session_has_tracing_off():
    session = _new_session()
    assert session.trace_sink.enabled is False
    # The no-op sink must accept the same calls the JSONL sink does.
    session.trace_sink.emit("call.start", ref="p1")
    assert session.trace_sink.snapshot("text") is None
    session.close()


def test_one_call_records_the_envelope_and_snapshots():
    session = _new_session()
    sink = MemoryTraceSink()
    session.trace_sink = sink
    session.observe()
    session.dispatch('FIND product "Big Mac"')
    session.dispatch("CALL p1.add()")

    names = [record["event"] for record in sink.events]
    assert names.count("observe") == 1
    assert "call.start" in names
    # Scope the ordering to the call itself: OBSERVE also compiles.
    call_names = names[names.index("call.start") :]
    for expected in (
        "call.start",
        "route.chain",
        "settle",
        "compile",
        "delta",
        "verify",
        "call.end",
    ):
        assert expected in call_names, call_names
    assert call_names.index("route.chain") < call_names.index("settle")
    assert call_names.index("settle") < call_names.index("compile")
    assert call_names.index("compile") < call_names.index("delta")
    assert call_names.index("verify") < call_names.index("call.end")

    start = next(r for r in sink.events if r["event"] == "call.start")
    assert start["tool"] == "call"
    assert start["ref"] == "p1"
    assert start["cap"] == "add"
    assert start["target_states"] == []
    assert start["call_id"].split(":")[1] == "1"
    assert start["before"] in sink.snapshots

    end = next(r for r in sink.events if r["event"] == "call.end")
    assert end["status"] == "OK"
    assert end["after"] in sink.snapshots
    assert "call_id" not in end or end["call_id"] == start["call_id"]

    # The before-snapshot is exactly what the model saw; the after-snapshot is
    # the settled tree.
    assert sink.snapshots[start["before"]] == session._last_observation_text
    assert 'CART c1 "Cart" count=1' in sink.snapshots[end["after"]]


def test_a_failed_call_still_ends_the_envelope():
    session = _new_session()
    sink = MemoryTraceSink()
    session.trace_sink = sink
    session.observe()
    session.dispatch("CALL p9.activate()")

    end = [r for r in sink.events if r["event"] == "call.end"][-1]
    assert end["status"] == "ERR"
    assert end["error_code"] == "STALE_TARGET"


def test_observe_without_a_call_is_recorded_once():
    session = _new_session()
    sink = MemoryTraceSink()
    session.trace_sink = sink
    observation = session.observe()
    session.close()

    observes = sink.events_named("observe")
    assert len(observes) == 1
    assert observes[0]["tokens"] == observation.tokens
    assert observes[0]["rev"] == observation.rev
    assert observes[0]["truncated"] is False


def test_jsonl_trace_round_trips_and_deduplicates_snapshots(tmp_path: Path):
    root = tmp_path / "run"
    sink = JsonlTraceSink(root, settings={"settle_quiet_ms": 40}, session="main")
    session = _new_session()
    session.trace_sink = sink
    session.observe()
    session.dispatch("CALL p1.add()")
    sink.close()

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "somnus-trace"
    assert manifest["version"] == 1
    assert manifest["trace"] == "trace.jsonl"
    assert manifest["settings"] == {"settle_quiet_ms": 40}
    assert manifest["events"]["call.start"] == 1
    assert manifest["events"]["call.end"] == 1
    assert manifest["closed_at"]

    events = [
        json.loads(line)
        for line in trace_path(root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "ts" in events[0] and "mono_ms" in events[0]
    refs = {
        value
        for record in events
        for key in ("before", "after")
        if isinstance(value := record.get(key), str)
    }
    assert refs
    for ref in refs:
        assert (root / ref).is_file(), ref
        assert (root / ref).read_text(encoding="utf-8").startswith("@rev")

    # Content addressing: the same text writes one file, not one per event.
    first = sink.snapshot("same body")
    second = sink.snapshot("same body")
    assert first == second
    assert len(list((root / "snapshots").rglob("*.sui.txt"))) == len(refs) + 1

    timeline = render_timeline(root, snapshots=True)
    assert "call.start" in timeline
    assert "route.chain" in timeline
    assert "--- snapshot" in timeline
    assert "CART" in timeline


def test_trace_root_resolution():
    assert resolve_trace_root(None) is None
    assert resolve_trace_root("") is None
    assert resolve_trace_root("0") is None
    # A bare marker chooses a fresh directory outside the repository.
    root = resolve_trace_root("1")
    assert root is not None
    assert root.parent.name == "traces"
    assert "somnusui" in root.parts
    # An explicit path is used verbatim.
    assert resolve_trace_root("./my-trace").name == "my-trace"
