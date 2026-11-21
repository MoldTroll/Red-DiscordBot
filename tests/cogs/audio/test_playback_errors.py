from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import lavalink
import pytest

from redbot.cogs.audio.core.events import lavalink as events
from redbot.cogs.audio.core.utilities.miscellaneous import MiscellaneousUtilities


@pytest.fixture
def playback(monkeypatch):
    channel = SimpleNamespace(send=AsyncMock())
    guild = SimpleNamespace(id=7, get_channel_or_thread=lambda channel_id: channel)
    source = lavalink.Track(
        {
            "track": "one",
            "info": {"title": "requested song", "uri": "https://stream.invalid/?sig=secret"},
        }
    )
    player = SimpleNamespace(
        current=source,
        channel=SimpleNamespace(guild=guild),
        guild=guild,
        node=SimpleNamespace(ready=True),
        fetch={"notify_channel": 12}.get,
        queue=[],
        skip=AsyncMock(),
    )
    settings = {"disconnect": False, "notify": True, "repeat": False, "auto_play": False}
    cog = SimpleNamespace(
        bot=SimpleNamespace(
            cog_disabled_in_guild=AsyncMock(return_value=False),
            get_embed_color=AsyncMock(return_value=discord.Color.blue()),
        ),
        config=SimpleNamespace(
            guild=lambda guild: SimpleNamespace(all=AsyncMock(return_value=settings)),
            status=AsyncMock(return_value=False),
        ),
        _error_counter={},
        rgetattr=lambda *args: MiscellaneousUtilities.rgetattr(None, *args),
        _has_notify_perms=lambda _: True,
        maybe_reset_error_counter=AsyncMock(),
        increase_error_counter=AsyncMock(return_value=False),
        api_interface=SimpleNamespace(persistent_queue_api=Mock()),
    )
    cog.send_embed_msg = lambda *args, **kwargs: MiscellaneousUtilities.send_embed_msg(
        cog, *args, **kwargs
    )
    monkeypatch.setattr(events, "set_contextual_locales_from_guild", AsyncMock())
    return cog, player, channel


async def test_exception_with_java_stack_produces_bounded_discord_notification(playback):
    cog, player, channel = playback
    player.current.title = "requested song " + "\N{MUSICAL NOTE}" * 3000
    detail = "AllClientsFailedException\n" + (
        " at youtube.client(https://host/?sig=secret)\n" * 400
    )
    await events.LavalinkEvents.lavalink_event_handler(
        cog, player, lavalink.LavalinkEvents.TRACK_EXCEPTION, {"message": detail, "cause": None}
    )
    description = channel.send.await_args.kwargs["embed"].description
    assert "YouTube" in description and "requested song" in description
    assert "secret" not in description and "AllClientsFailedException" not in description
    assert len(description.encode("utf-16-le")) // 2 <= 4096
    player.skip.assert_awaited_once()


@pytest.mark.parametrize("advance_during_send", [False, True])
async def test_notification_http_error_preserves_queue_progress(playback, advance_during_send):
    cog, player, channel = playback
    replacement = object()

    async def reject(**kwargs):
        if advance_during_send:
            player.current = replacement
        response = SimpleNamespace(status=400, reason="Bad Request")
        raise discord.HTTPException(response, {"message": "Invalid Form Body"})

    channel.send.side_effect = reject
    await events.LavalinkEvents.lavalink_event_handler(
        cog, player, lavalink.LavalinkEvents.TRACK_EXCEPTION, {"message": None, "cause": None}
    )
    assert player.skip.await_count == (0 if advance_during_send else 1)
    if advance_during_send:
        assert player.current is replacement


def test_signed_url_in_fallback_title_is_never_sent():
    track = SimpleNamespace(title="https://stream.invalid/file?token=secret")
    message = events._playback_error_description(
        track, lavalink.LavalinkEvents.TRACK_EXCEPTION, {"message": None, "cause": track.title}
    )
    assert message == "Lavalink could not play this track."
