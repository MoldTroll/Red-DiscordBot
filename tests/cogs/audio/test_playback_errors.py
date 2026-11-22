import asyncio
import time
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import lavalink
import pytest

from redbot.cogs.audio.core.events import lavalink as events
from redbot.cogs.audio.core.utilities.miscellaneous import MiscellaneousUtilities
from redbot.cogs.audio.core.utilities.player import PlayerUtilities


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
    state = {"notify_channel": 12}
    player = SimpleNamespace(
        current=source,
        channel=SimpleNamespace(guild=guild),
        guild=guild,
        node=SimpleNamespace(ready=True),
        fetch=state.get,
        store=state.__setitem__,
        paused=False,
        queue=[],
        skip=AsyncMock(),
        repeat=False,
    )
    settings = {"disconnect": False, "notify": True, "repeat": False, "auto_play": False}
    cog = SimpleNamespace(
        bot=SimpleNamespace(
            cog_disabled_in_guild=AsyncMock(return_value=False),
            get_embed_color=AsyncMock(return_value=discord.Color.blue()),
            get_valid_prefixes=AsyncMock(return_value=["^"]),
        ),
        config=SimpleNamespace(
            guild=lambda guild: SimpleNamespace(all=AsyncMock(return_value=settings)),
            status=AsyncMock(return_value=False),
        ),
        _error_counter={},
        _ll_guild_updates=set(),
        rgetattr=lambda *args: MiscellaneousUtilities.rgetattr(None, *args),
        _has_notify_perms=lambda _: True,
        record_playback_failure=Mock(return_value=False),
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


@pytest.mark.parametrize(
    "detail,expected",
    [
        ("Client [IOS] failed: This video requires age verification.", "age verification"),
        ("Client [WEB] failed: This video requires login.", "sign-in"),
        ("Client [MWEB] failed: Not success status code: 403", "HTTP 403"),
    ],
)
def test_reports_only_known_youtube_reasons(detail, expected):
    result = events._playback_error_description(
        None,
        lavalink.LavalinkEvents.TRACK_EXCEPTION,
        {"cause": f"AllClientsFailedException\n{detail}\nhttps://x.invalid/?token=secret"},
    )
    assert expected in result and "secret" not in result


def test_reports_mixed_client_outcomes_without_claiming_a_single_cause():
    msg = events._playback_error_description(
        None,
        lavalink.LavalinkEvents.TRACK_EXCEPTION,
        {
            "cause": "AllClientsFailedException\nClient [MWEB] failed: Not success status code: 403\nClient [TV] failed: This video requires login.\nClient [IOS] failed: This video requires age verification."
        },
    )
    assert "client responses" in msg
    assert "HTTP 403" in msg and "sign-in requested" in msg and "age verification requested" in msg


async def test_independent_duplicate_request_survives_failure(playback):
    cog, player, channel = playback
    failed = player.current
    duplicate = lavalink.Track({"track": "one", "info": {"title": "second request"}})
    player.queue = [duplicate, failed]
    await events.LavalinkEvents.lavalink_event_handler(
        cog, player, lavalink.LavalinkEvents.TRACK_EXCEPTION, {}
    )
    assert len(player.queue) == 1 and player.queue[0] is duplicate
    cog.api_interface.persistent_queue_api.removed.assert_called_once_with(7, [failed])
    # Lavalink may emit a second exception or queue-end for the same failed request.
    await events.LavalinkEvents.lavalink_event_handler(
        cog, player, lavalink.LavalinkEvents.TRACK_STUCK, None
    )
    player.current = None
    player.queue.clear()
    player.is_auto_playing = False
    cog.bot.dispatch = Mock()
    await events.LavalinkEvents.lavalink_event_handler(
        cog, player, lavalink.LavalinkEvents.QUEUE_END, None
    )
    assert channel.send.await_count == 1


async def test_now_playing_requires_connected_progress(playback):
    cog, player, channel = playback
    t = player.current
    cog.config.guild = lambda _: SimpleNamespace(thumbnail=AsyncMock(return_value=False))
    cog.format_time = str
    cog.local_folder_current_path = None
    cog.get_track_description = AsyncMock(return_value="song")
    player.store("pending_now_playing", (t, None))

    async def update(when, position, connected):
        await events.LavalinkEvents.lavalink_update_handler(
            cog,
            player,
            SimpleNamespace(time=when, position=position, connected=connected),
            {"guildId": 7},
        )

    await update(1, 4000, False)
    await update(2, 4000, True)
    await update(3, 4000, True)
    channel.send.assert_not_awaited()
    await update(4, 4500, True)
    await update(5, 5000, True)
    assert channel.send.await_count == 1
    assert channel.send.await_args.kwargs["embed"].title == "Now Playing"


async def test_no_now_playing_after_track_changed_during_embed(playback):
    cog, player, channel = playback
    cog.config.guild = lambda _: SimpleNamespace(thumbnail=AsyncMock(return_value=False))
    cog.format_time = str
    cog.local_folder_current_path = None

    async def changed(*args):
        player.current = None
        return "stale"

    cog.get_track_description = changed
    player.store("pending_now_playing", (player.current, (1, 0)))
    await events.LavalinkEvents.lavalink_update_handler(
        cog, player, SimpleNamespace(time=2, position=1500, connected=True), {"guildId": 7}
    )
    channel.send.assert_not_awaited()


async def test_stale_start_for_replaced_track_keeps_failure_state(playback):
    cog, player, channel = playback
    current = player.current
    player.store("last_playback_failure", current)
    player.store("queue_suspended", True)
    await events.LavalinkEvents.lavalink_event_handler(
        cog, player, lavalink.LavalinkEvents.TRACK_START, "different-encoded-track"
    )
    assert player.fetch("last_playback_failure") is current
    assert player.fetch("queue_suspended")
    cog.api_interface.persistent_queue_api.started.assert_not_called()


async def test_error_burst_holds_only_unattempted_requests(playback):
    cog, player, channel = playback
    cog.config.guild = lambda _: SimpleNamespace(
        all=AsyncMock(
            return_value={"disconnect": False, "notify": True, "repeat": False, "auto_play": False}
        ),
        currently_auto_playing_in=SimpleNamespace(set=AsyncMock()),
    )
    cog.record_playback_failure.return_value = True
    player.queue = [object(), object()]
    pending = list(player.queue)
    failed = player.current
    player.pause = AsyncMock()
    player.node.no_event_stop = AsyncMock()
    await events.LavalinkEvents.lavalink_event_handler(
        cog, player, lavalink.LavalinkEvents.TRACK_EXCEPTION, {}
    )
    cog.api_interface.persistent_queue_api.removed.assert_called_once_with(7, [failed])
    cog.api_interface.persistent_queue_api.hold.assert_called_once_with(7, pending)
    assert player.queue == [] and player.current is None
    player.skip.assert_not_awaited()
    assert "queue resume" in channel.send.await_args.kwargs["embed"].description


async def test_fifth_failure_precedes_client_finished_and_new_requests(playback):
    cog, player, channel = playback
    cog._error_counter = Counter({7: 4})
    cog._error_timer = {7: time.monotonic()}
    cog.record_playback_failure = PlayerUtilities.record_playback_failure.__get__(cog)
    pending = [lavalink.Track({"track": key}) for key in ("later1", "later2")]
    player.queue = list(pending)
    requested = lavalink.Track({"track": "new"})
    player.stop = AsyncMock()
    player.pause = AsyncMock()
    player.node.play = AsyncMock()
    player.node.no_event_stop = AsyncMock()
    player.play = lavalink.Player.play.__get__(player)

    async def concurrently(*args):
        # The library schedules this handler alongside the Audio cog's listener.
        await lavalink.Player.handle_event(
            player, lavalink.LavalinkEvents.TRACK_END, lavalink.TrackEndReason.FINISHED
        )
        assert player.current is None
        player.queue.append(requested)
        await player.play()
        return False

    cog.bot.cog_disabled_in_guild = concurrently
    await events.LavalinkEvents.lavalink_event_handler(
        cog, player, lavalink.LavalinkEvents.TRACK_EXCEPTION, {}
    )
    cog.api_interface.persistent_queue_api.hold.assert_called_once_with(7, pending)
    assert player.current is requested
    player.pause.assert_not_awaited()
    player.node.no_event_stop.assert_not_awaited()


async def test_stale_now_playing_cannot_delete_new_notification(playback):
    cog, player, channel = playback
    a = player.current
    b = lavalink.Track({"track": "two", "info": {"title": "next"}})
    describing = asyncio.Event()
    finish = asyncio.Event()

    async def describe(track, _):
        if track is a:
            describing.set()
            await finish.wait()
        return track.title

    cog.get_track_description = describe
    cog.config.guild = lambda _: SimpleNamespace(thumbnail=AsyncMock(return_value=False))
    cog.format_time = str
    cog.local_folder_current_path = None
    notify = SimpleNamespace(delete=AsyncMock())
    cog.send_embed_msg = AsyncMock(return_value=notify)

    async def progress(track):
        player.store("pending_now_playing", (track, (1, 0)))
        await events.LavalinkEvents.lavalink_update_handler(
            cog, player, SimpleNamespace(time=2, position=1000, connected=True), {"guildId": 7}
        )

    stale = asyncio.create_task(progress(a))
    await describing.wait()
    player.current = b
    await progress(b)
    finish.set()
    await stale
    cog.send_embed_msg.assert_awaited_once()
    assert player.fetch("notify_message") is notify
    notify.delete.assert_not_awaited()


async def test_reconnect_needs_new_connected_progress_even_during_notification(playback):
    cog, player, channel = playback
    scope = SimpleNamespace(thumbnail=AsyncMock(return_value=False))
    cog.config.guild = lambda _: scope
    cog.format_time = str
    cog.local_folder_current_path = None

    async def update(when, position, connected):
        await events.LavalinkEvents.lavalink_update_handler(
            cog,
            player,
            SimpleNamespace(time=when, position=position, connected=connected),
            {"guildId": 7},
        )

    async def interrupted(*args):
        await update(3, 900, False)
        return "old connection"

    cog.get_track_description = interrupted
    player.store("pending_now_playing", (player.current, (1, 0)))
    await update(2, 500, True)
    await update(4, 6000, True)
    channel.send.assert_not_awaited()
    cog.get_track_description = AsyncMock(return_value="song")
    await update(5, 6800, True)
    channel.send.assert_awaited_once()


async def test_delayed_auto_disconnect_spares_new_track(playback, monkeypatch):
    cog, player, channel = playback
    settings = {"disconnect": True, "notify": True, "repeat": False, "auto_play": False}
    cog.config.guild = lambda _: SimpleNamespace(all=AsyncMock(return_value=settings))
    cog.bot.dispatch = Mock()
    player.guild.voice_client = player
    player.current = None
    player.is_auto_playing = False
    player.disconnect = AsyncMock()

    async def new_request(_):
        player.current = object()

    monkeypatch.setattr(events.asyncio, "sleep", new_request)
    await events.LavalinkEvents.lavalink_event_handler(
        cog, player, lavalink.LavalinkEvents.QUEUE_END, None
    )
    player.disconnect.assert_not_awaited()
