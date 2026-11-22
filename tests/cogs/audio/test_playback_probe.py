import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import lavalink
import pytest
from lavalink.node import Node

from redbot.cogs.audio.core.commands.llset import LavalinkSetupCommands
from redbot.cogs.audio.playback_probe import PlaybackProbe, TEST_VIDEO_ID


@pytest.fixture
def probe_setup(monkeypatch):
    monkeypatch.delenv("RED_AUDIO_PROBE_VIDEO_ID", raising=False)
    player = Mock(spec=lavalink.Player)
    track = SimpleNamespace(_info={"identifier": TEST_VIDEO_ID}, length=19_000, is_stream=False)
    player.load_tracks = AsyncMock(return_value=SimpleNamespace(tracks=[track]))
    player.set_volume = AsyncMock()
    player.disconnect = AsyncMock()
    started = asyncio.Event()

    async def play():
        player.current = track
        started.set()

    player.play = play
    player.current = None
    guild = SimpleNamespace(id=12, me=SimpleNamespace(voice=None), voice_client=None)
    channel = Mock(spec=discord.VoiceChannel)
    channel.guild = guild
    channel.members = []
    channel.id = 34
    player.channel = channel
    player.node = object.__new__(Node)
    player.node._players_dict = {}
    player.update_state.side_effect = lambda state: setattr(player, "state", state)
    monkeypatch.setattr(lavalink, "Player", Mock(return_value=player))

    async def connect(*, cls, **kwargs):
        guild.voice_client = cls(None, channel)
        player.node._players_dict[guild.id] = player
        return guild.voice_client

    channel.connect = AsyncMock(side_effect=connect)
    player.cleanup.side_effect = lambda: setattr(guild, "voice_client", None)
    configured = AsyncMock(return_value=channel.id)
    bot = SimpleNamespace(
        get_channel=Mock(return_value=channel), cog_disabled_in_guild=AsyncMock(return_value=False)
    )
    probe = PlaybackProbe(
        SimpleNamespace(bot=bot, config=SimpleNamespace(playback_check_channel=configured))
    )
    updates, events = [], []
    monkeypatch.setattr(lavalink, "register_update_listener", updates.append)
    monkeypatch.setattr(lavalink, "unregister_update_listener", updates.remove)
    monkeypatch.setattr(lavalink, "register_event_listener", events.append)
    monkeypatch.setattr(lavalink, "unregister_event_listener", events.remove)
    return SimpleNamespace(
        probe=probe,
        channel=channel,
        player=player,
        track=track,
        started=started,
        updates=updates,
        events=events,
    )


async def test_playback_requires_sustained_connected_progress_and_finished(probe_setup):
    p = probe_setup
    task = asyncio.create_task(p.probe.check())
    await asyncio.wait_for(p.started.wait(), timeout=1)
    (update,) = p.updates

    async def send(connected, time, position, player=p.player):
        await update(
            player, SimpleNamespace(connected=connected, time=time, position=position), {}
        )
        await asyncio.sleep(0)

    try:
        await send(True, 0, 0, player=object())
        p.player.current = object()
        await send(True, 1000, 0)
        p.player.current = p.track
        await send(True, 2000, 1000)
        assert not task.done()
        await send(False, 3000, 2000)
        await send(True, 4000, 3000)
        await send(True, 4000, 17000)
        await send(True, 5000, 4000)
        await send(True, 18500, 17500)
        assert not task.done()
        await p.events[0](object(), lavalink.LavalinkEvents.TRACK_EXCEPTION, {})
        # Red-Lavalink's own event listener may have already cleared player.current.
        p.player.current = None
        await p.events[0](
            p.player, lavalink.LavalinkEvents.TRACK_END, lavalink.TrackEndReason.FINISHED
        )
        assert await asyncio.wait_for(task, timeout=1) == "ok"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert p.probe.task is None and p.probe.guild_id is None
    assert not p.updates and not p.events
    p.player.load_tracks.assert_awaited_once_with(
        f"https://www.youtube.com/watch?v={TEST_VIDEO_ID}"
    )
    p.player.set_volume.assert_awaited_once_with(1)
    p.player.disconnect.assert_awaited_once()


async def test_only_selected_short_youtube_video_is_used(probe_setup, monkeypatch):
    p = probe_setup
    monkeypatch.setenv("RED_AUDIO_PROBE_VIDEO_ID", "file:///etc/passwd")
    assert await p.probe.check() == "failed"
    p.channel.connect.assert_not_awaited()
    monkeypatch.setenv("RED_AUDIO_PROBE_VIDEO_ID", "abcdefghijk")
    assert await p.probe.check() == "failed"  # metadata belongs to a different video
    p.player.load_tracks.assert_awaited_once_with("https://www.youtube.com/watch?v=abcdefghijk")
    p.track._info["identifier"] = "abcdefghijk"
    p.track.length = 220_000
    assert await p.probe.check() == "failed"
    assert not p.started.is_set()


@pytest.mark.parametrize(
    "cause", ["early_finished", "exception", "stuck", "stopped", "reconnect", "listener"]
)
async def test_end_without_confirmed_continuous_voice_fails(probe_setup, cause):
    p = probe_setup
    task = asyncio.create_task(p.probe.check())
    await asyncio.wait_for(p.started.wait(), timeout=1)
    update, event = p.updates[0], p.events[0]
    for ms in (0, 1000, 2000) if cause == "early_finished" else (0, 8000, 16000):
        await update(p.player, SimpleNamespace(connected=True, time=ms, position=ms), {})
    if cause == "reconnect":
        await update(p.player, SimpleNamespace(connected=False, time=17000, position=17000), {})
        await update(p.player, SimpleNamespace(connected=True, time=18000, position=18000), {})
    if cause == "listener":
        p.channel.members = [object()]
    if cause == "exception":
        await event(p.player, lavalink.LavalinkEvents.TRACK_EXCEPTION, {})
    elif cause == "stuck":
        await event(p.player, lavalink.LavalinkEvents.TRACK_STUCK, {})
    reason = (
        lavalink.TrackEndReason.STOPPED if cause == "stopped" else lavalink.TrackEndReason.FINISHED
    )
    await event(p.player, lavalink.LavalinkEvents.TRACK_END, reason)
    assert await asyncio.wait_for(task, timeout=1) == ("busy" if cause == "listener" else "failed")
    assert not p.updates and not p.events


@pytest.mark.parametrize(
    "cause",
    [
        "unconfigured",
        "unavailable",
        "non_voice",
        "disabled",
        "permission",
        "occupied",
        "connected",
    ],
)
async def test_unusable_probe_channel_never_joins(probe_setup, cause):
    p = probe_setup
    expected = "unavailable"
    if cause == "unconfigured":
        p.probe.cog.config.playback_check_channel.return_value = None
        expected = cause
    elif cause == "unavailable":
        p.probe.cog.bot.get_channel.return_value = None
    elif cause == "non_voice":
        p.probe.cog.bot.get_channel.return_value = Mock(spec=discord.TextChannel)
    elif cause == "disabled":
        p.probe.cog.bot.cog_disabled_in_guild.return_value = True
    elif cause == "permission":
        p.channel.permissions_for.return_value.speak = False
    elif cause == "occupied":
        p.channel.members = [object()]
        expected = "busy"
    elif cause == "connected":
        p.channel.guild.voice_client = object()
        expected = "busy"
    assert await p.probe.check() == expected
    p.channel.connect.assert_not_awaited()


@pytest.mark.parametrize("failure", ["no_tracks", "lookup", "play"])
async def test_failed_probe_releases_player_and_listener(probe_setup, failure):
    p = probe_setup
    if failure == "no_tracks":
        p.player.load_tracks.return_value = SimpleNamespace(tracks=[])
    elif failure == "lookup":
        p.player.load_tracks.side_effect = RuntimeError("lookup")
    else:
        p.player.play = AsyncMock(side_effect=RuntimeError("play"))
    assert await p.probe.check() == "failed"
    assert p.probe.task is None and p.probe.guild_id is None
    assert not p.updates
    p.player.disconnect.assert_awaited_once()


async def test_probe_cancellation_and_concurrent_request(probe_setup):
    p = probe_setup
    task = asyncio.create_task(p.probe.check())
    await asyncio.wait_for(p.started.wait(), timeout=1)
    assert await p.probe.check() == "busy"
    await p.probe.close()
    assert task.cancelled()
    assert not p.updates
    p.player.disconnect.assert_awaited_once()


@pytest.mark.parametrize(
    "line,answer",
    [
        (b"available\n", b"ready\n"),
        (b"check\n", b"ok\n"),
        (b"verify\n", b"finished\n"),
        (b"unknown\n", b"invalid\n"),
    ],
)
async def test_local_probe_protocol(probe_setup, line, answer):
    p = probe_setup
    p.probe.check = AsyncMock(return_value="ok")
    reader = asyncio.StreamReader()
    reader.feed_data(line)
    reader.feed_eof()
    writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
    await p.probe._serve(reader, writer)
    writer.write.assert_called_once_with(answer)
    writer.close.assert_called_once()
    assert p.probe.check.await_count == (line in (b"check\n", b"verify\n"))


async def test_owner_command_configures_and_disables_test_channel(probe_setup):
    p = probe_setup
    config_value = p.probe.cog.config.playback_check_channel
    config_value.set = AsyncMock()
    ctx = SimpleNamespace(guild=p.channel.guild, tick=AsyncMock(), send=AsyncMock())
    callback = LavalinkSetupCommands.command_llset_playbackchannel.callback
    await callback(p.probe.cog, ctx, p.channel)
    config_value.set.assert_awaited_once_with(p.channel.id)
    await callback(p.probe.cog, ctx, None)
    config_value.set.assert_awaited_with(None)
    await callback(p.probe.cog, ctx, SimpleNamespace(guild=object()))
    assert config_value.set.await_count == 2 and ctx.tick.await_count == 2
    ctx.send.assert_awaited_once()
    p.channel.permissions_for.return_value.connect = False
    await callback(p.probe.cog, ctx, p.channel)
    assert config_value.set.await_count == 2 and ctx.send.await_count == 2


async def test_connection_failure_cleans_up_its_own_partial_player(probe_setup):
    p = probe_setup

    async def connect(*, cls, **kwargs):
        p.channel.guild.voice_client = cls(None, p.channel)
        p.player.node._players_dict[p.channel.guild.id] = p.player
        raise RuntimeError("gateway disconnected")

    p.channel.connect.side_effect = connect
    p.player.disconnect.side_effect = RuntimeError("unavailable")
    assert await p.probe.check() == "failed"
    p.player.cleanup.assert_called_once()
    assert p.channel.guild.voice_client is None
    with pytest.raises(lavalink.PlayerNotFound):
        p.player.node.get_player(p.channel.guild.id)
