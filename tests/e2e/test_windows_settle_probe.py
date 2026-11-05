"""Real-GUI check: a disabled->enabled flip is caught by the layered settle probe.

The scenario is the chat send button shape: a composer that gates a send button
(``IsEnabled`` follows the text). The button starts disabled and becomes
clickable once the composer has text. The point of the test is the *settled
state*: if the layered probe missed the button's state change, the compiled tree
would still carry the stale ``disabled`` state and the next CALL would be
refused.

The Scenario Suite's own chat screen cannot host this case: its send button is
never gated (there is no dynamic ``IsEnabled`` anywhere in the app's code - the
composer there exists to exercise the self-drawn *write* routes). The fixture
therefore lives in ``tests/fixtures/sendgate``: a minimal WPF window of the same
shape, built into a temporary directory by the test itself, so the real desktop
is still only touched by an app this repository owns.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.windows

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "sendgate"
_WINDOW_TIMEOUT = 20.0


@pytest.fixture(scope="module")
def sendgate_app(tmp_path_factory):
    """Build the send-gate fixture into a temp dir and return its exe."""
    if not sys.platform.startswith("win"):
        pytest.skip("windows only")
    if not shutil.which("dotnet"):
        pytest.skip("dotnet SDK not available to build tests/fixtures/sendgate")
    work = tmp_path_factory.mktemp("sendgate")
    project = work / "project"
    shutil.copytree(FIXTURE, project)
    built = subprocess.run(
        ["dotnet", "build", "-c", "Debug", "-o", "out"],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if built.returncode != 0:
        pytest.skip(f"send gate fixture failed to build:\n{built.stdout}{built.stderr}")
    exe = project / "out" / "SendGate.exe"
    if not exe.exists():
        pytest.skip(f"send gate exe not produced: {exe}")
    return exe


class _RecordingSettler:
    """Wrap the real settler and keep every SettleResult for assertions."""

    def __init__(self):
        from somnus.runtime.settle import Settler

        self._inner = Settler()
        self.results = []

    def wait(self, *args, **kwargs):
        tree, result = self._inner.wait(*args, **kwargs)
        self.results.append(result)
        return tree, result


def _automation_refs(session) -> dict[str, str]:
    return {
        src.native.automation_id: ref
        for ref, src in session.result.sources.items()
        if src.native.automation_id
    }


def test_the_send_button_flip_is_caught_by_the_layered_probe(sendgate_app, wait_for_window):
    from somnus.providers.base import get_provider
    from somnus.runtime.policy import Policy
    from somnus.runtime.session import Session

    proc = subprocess.Popen([str(sendgate_app)])
    provider = get_provider("uia", allow_list=["sendgate.exe"])
    settler = _RecordingSettler()
    session = Session(
        provider, policy=Policy(allow_clipboard=True), settler=settler, allow_packs=False
    )
    try:
        wait_for_window(
            session,
            process="sendgate.exe",
            title="Send Gate",
            timeout=_WINDOW_TIMEOUT,
            proc=proc,
            fail_if_exited=True,
        )

        session.observe()
        # The viewport capture must have established the watched set; without
        # it settle has no probe and this test would be checking the old path.
        assert session._watch_natives, "viewport capture did not establish a watch set"
        refs = _automation_refs(session)
        composer_ref = refs["composer"]
        send_ref = refs["send"]

        def send_states():
            node = session.result.find(send_ref)
            return set(node.states) if node is not None else set()

        assert "disabled" in send_states(), (
            f"send should start disabled, got {send_states()}"
        )

        # Warm-up call: the first settle after a fresh observe has no as-read
        # frame to compare against, so it pays full snapshots (the documented
        # one-off).
        warm = session.dispatch(f'CALL {composer_ref}.set_text("")')
        assert warm.startswith("OK"), warm

        out = session.dispatch(f'CALL {composer_ref}.set_text("hello")')
        assert out.startswith("OK"), out

        # This window is small and its capture fits whole, so settle reads full
        # frames instead of probing (a probe there costs about a full read; see
        # Session._settle). What this test is about is that the flip is *seen*.
        last = settler.results[-1]
        assert last.snapshots >= 1, f"settle must read the changed frame: {last}"
        assert last.probes == 0, f"a small window settles with full reads: {last}"
        assert last.settled is True, last

        after = session.result.find(send_ref)
        assert after is not None, "the send button must survive the recompile"
        assert "disabled" not in after.states, (
            f"settle kept a stale disabled state; states={after.states}"
        )
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - teardown
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - teardown
            proc.kill()
