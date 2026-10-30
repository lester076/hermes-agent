"""Regression guard for Discord text-batch flush during gateway shutdown."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


def _ensure_discord_mock():
    """Install a mock discord module when discord.py isn't available."""
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.mark.asyncio
async def test_cancel_background_tasks_awaits_pending_text_batch_before_clearing():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    flushed = asyncio.Event()

    async def pending_flush():
        await asyncio.sleep(0)
        flushed.set()

    task = asyncio.create_task(pending_flush())
    adapter._pending_text_batch_tasks["chat"] = task
    adapter._pending_text_batches["chat"] = MessageEvent(
        text="pending",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.DISCORD, chat_id="chat", chat_type="group"),
    )

    await adapter.cancel_background_tasks()

    assert flushed.is_set()
    assert task.done()
    assert adapter._pending_text_batch_tasks == {}
    assert adapter._pending_text_batches == {}


@pytest.mark.asyncio
async def test_base_cancel_background_tasks_awaits_and_spools_pending_text_batches(tmp_path, monkeypatch):
    """#146858: the base cancel path (WhatsApp/Matrix/WeCom use it; Discord overrides) left
    _pending_text_batch_tasks unowned — a batch inside its merge window was neither awaited,
    cancelled, nor spooled, and on Matrix the cursor had already advanced past it."""
    from gateway import shutdown_flush
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(shutdown_flush, "_get_flush_dir", lambda: str(tmp_path / "flushed"))

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    finished_normally = asyncio.Event()
    spooled: list = []

    async def tracked_flush():
        # #146858's failure mode on unmodified base: this task is never awaited or
        # cancelled, so it only finishes when the loop it lives in drains. The fixed
        # base awaits it, so one yield is enough for it to complete.
        await asyncio.sleep(0)
        finished_normally.set()

    def record_flush(pending, *, reason="shutdown"):
        # Snapshot: cancel_background_tasks clears the same dict right after the call.
        spooled.append((dict(pending), reason))
        return len(pending)

    task = asyncio.create_task(tracked_flush())
    adapter._pending_text_batch_tasks["chat"] = task
    adapter._pending_text_batches["chat"] = MessageEvent(
        text="pending",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.DISCORD, chat_id="chat", chat_type="group"),
    )
    # The batch event stays parked (the flush never pops it in this stand-in), so the
    # spool call must see it — the pre-fix base never spooled these at all.
    # base.py does `from gateway.shutdown_flush import flush_pending_to_file` at call
    # time, so patch the source module attribute.
    import gateway.shutdown_flush as _shutdown_flush_mod
    monkeypatch.setattr(_shutdown_flush_mod, "flush_pending_to_file", record_flush)

    # Go through the BASE implementation explicitly (Discord's override adds its own flush).
    from gateway.platforms.base import BasePlatformAdapter
    await BasePlatformAdapter.cancel_background_tasks(adapter)

    # The flush task was awaited to completion, not left to unwind on a dead loop.
    assert finished_normally.is_set()
    assert task.done()
    # Batches the flush window did not drain are spooled for recovery, then both dicts clear.
    assert any(entry.get("chat") is not None for entry, _ in spooled if isinstance(entry, dict))
    assert adapter._pending_text_batch_tasks == {}
    assert adapter._pending_text_batches == {}
