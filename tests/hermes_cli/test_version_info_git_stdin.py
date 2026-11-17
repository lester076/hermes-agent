# Copyright converges to Hermes contributors; SPDX-License-Identifier: Apache-2.0
"""The git provenance lookups must not inherit the process's stdin.

When Hermes serves ACP over stdio, a read is always pending on the pipe the
client gave the process. A ``git`` subprocess that inherits that pipe blocks
at startup on Windows (Git for Windows waits on the inherited handles), so
``session/new``/``session/load`` — which build the model catalog while the
version header resolves — hang forever, and the ``timeout`` kills only the
outer ``Git\\cmd\\git.exe`` wrapper, leaving the inner ``mingw64\\bin\\git.exe``
holding the pipes (#156777). Both call sites therefore give git its own empty
stdin.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import version_info


class _RecordingRun:
    """Record the kwargs each subprocess.run call passes."""

    def __init__(self, replies=(b"main\n", b"")):
        self.calls: list[dict] = []
        self._replies = list(replies)

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        reply = self._replies.pop(0) if self._replies else b""

        class _Result:
            returncode = 0
            stdout = reply
            stderr = b""

        return _Result()


def test_run_git_passes_devnull_stdin(tmp_path: Path) -> None:
    recorder = _RecordingRun()
    with patch.object(version_info.subprocess, "run", recorder):
        version_info._run_git(tmp_path, "rev-parse", "HEAD")
    assert recorder.calls, "_run_git must call subprocess.run"
    for call in recorder.calls:
        assert call.get("stdin") is subprocess.DEVNULL


def _stub_run_git(repo_dir, *args):
    """Consistent provenance for a stubbed checkout: one release, one commit."""
    joined = " ".join(args)
    if joined.startswith("rev-parse HEAD"):
        return "0123456789abcdef0123456789abcdef0123456"
    if joined.startswith("branch"):
        return "main"
    if joined.startswith("log"):
        return "1760000000"
    if joined.startswith("tag"):
        return "v2026.10.1"
    if joined.startswith("rev-list"):
        return "3"
    if joined.startswith("rev-parse --short"):
        return "0123456"
    return None


def test_git_version_info_status_check_passes_devnull_stdin(tmp_path: Path) -> None:
    recorder = _RecordingRun()
    with patch.object(version_info, "_run_git", side_effect=_stub_run_git), \
         patch.object(version_info.subprocess, "run", recorder):
        info = version_info._git_version_info(tmp_path)
    assert info is not None
    status_calls = [c for c in recorder.calls if "status" in c["cmd"]]
    assert status_calls, "the dirty check must run git status"
    for call in status_calls:
        assert call.get("stdin") is subprocess.DEVNULL


@pytest.mark.parametrize("include_untracked", [False, True])
def test_status_command_shape_is_preserved(tmp_path: Path, include_untracked: bool) -> None:
    recorder = _RecordingRun()
    with patch.object(version_info, "_run_git", side_effect=_stub_run_git), \
         patch.object(version_info.subprocess, "run", recorder):
        version_info._git_version_info(tmp_path, include_untracked=include_untracked)
    status_calls = [c for c in recorder.calls if "status" in c["cmd"]]
    assert status_calls
    assert "--no-optional-locks" in status_calls[0]["cmd"]
    if not include_untracked:
        assert "-uno" in status_calls[0]["cmd"]
    else:
        assert "-uno" not in status_calls[0]["cmd"]
