"""Unit tests for the #146856 tree-kill orphan fix in ``_kill_process_windows``.

``terminate_pid`` is fail-closed on a missing/changed wrapper start-time fingerprint;
the pre-fix fallback only killed the (already dead) wrapper, so a timed-out or
cancelled command's actual subtree survived silently. These tests pin the fallback's
contract against a mocked ``psutil`` boundary: still-living descendants are killed by
identity, enumeration failures never break the kill path, and the wrapper kill always
runs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tools.environments import local as local_mod


def _proc_mock(pid: int):
    proc = MagicMock()
    proc.pid = pid
    proc.wait = MagicMock()
    return proc


@pytest.fixture()
def refuse_terminate(monkeypatch):
    """Make the terminate_pid from-import inside _kill_process_windows raise."""
    import gateway.status as status_mod
    monkeypatch.setattr(status_mod, "terminate_pid",
                        MagicMock(side_effect=OSError("refusing to force-kill")))


@pytest.fixture()
def fake_psutil(monkeypatch):
    """A controllable ``psutil`` stand-in for the function-local import."""
    import sys
    psutil_mod = MagicMock()
    monkeypatch.setitem(sys.modules, "psutil", psutil_mod)
    return psutil_mod


def test_fallback_kills_living_descendants_when_terminate_refuses(fake_psutil, refuse_terminate):
    """terminate_pid raises on the fingerprint guard; the wrapper kill is best-effort
    and every still-living descendant must be killed via its own live handle (#146856)."""
    wrapper = _proc_mock(100)
    sleeper = MagicMock()
    sleeper.is_running.return_value = True
    fake_psutil.Process.side_effect = lambda pid: (
        MagicMock(children=MagicMock(return_value=[sleeper])) if pid == 100
        else MagicMock()
    )
    killed = []
    sleeper.kill.side_effect = lambda: killed.append(sleeper)

    local_mod._kill_process_windows(wrapper)

    assert killed, "the living descendant was never killed by the fallback"
    wrapper.kill.assert_called_once()


def test_descendant_kill_failure_never_breaks_the_path(fake_psutil, refuse_terminate):
    """A descendant dying between enumeration and kill must not escape the kill path."""
    wrapper = _proc_mock(200)
    dying = MagicMock()
    dying.is_running.return_value = True
    dying.kill.side_effect = RuntimeError("gone")
    fake_psutil.Process.side_effect = lambda pid: (
        MagicMock(children=MagicMock(return_value=[dying])) if pid == 200
        else MagicMock()
    )

    local_mod._kill_process_windows(wrapper)  # must not raise
    wrapper.kill.assert_called_once()
    wrapper.wait.assert_called_once()


def test_enumeration_failure_never_breaks_the_kill(fake_psutil, refuse_terminate):
    """psutil enumeration can fail (access denied, wrapper vanished mid-call); the
    wrapper kill and proc.wait still run — the pre-fix behavior for that shape."""
    wrapper = _proc_mock(300)
    fake_psutil.Process.side_effect = RuntimeError("access denied")

    local_mod._kill_process_windows(wrapper)  # must not raise
    wrapper.kill.assert_called_once()
    wrapper.wait.assert_called_once()
