"""Check playback through the bot's own Discord and Lavalink connections."""

import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
import re

import discord
import lavalink


log = logging.getLogger("red.Audio.playback")
# A short YouTube track that can reach its natural end within a deployment check.
TEST_VIDEO_ID = "jNQXAC9IVRw"  # Me at the zoo, approximately 19 seconds.


class PlaybackProbe:
    def __init__(self, cog):
        self.cog = cog
        self.guild_id = None
        self.server = None
        self.task = None

    async def start(self):
        address = os.environ.get("RED_AUDIO_PROBE_SOCKET")
        if not address:
            return
        path = Path(address)
        directory = path.parent.stat()
        if directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
            raise ValueError("Audio probe requires a private directory owned by the bot")
        self.server = await asyncio.start_unix_server(self._serve, path=path)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _serve(self, reader, writer):
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5)
            if request in (b"check\n", b"verify\n"):
                result = await self.check()
                if request == b"verify\n" and result == "ok":
                    result = "finished"
            elif request == b"available\n":
                result, _ = await self.channel_status()
            else:
                result = "invalid"
            writer.write((result + "\n").encode("ascii"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def channel_status(self):
        channel_id = await self.cog.config.playback_check_channel()
        if not channel_id:
            return "unconfigured", None
        channel = self.cog.bot.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel) or channel.guild.me is None:
            return "unavailable", None
        permissions = channel.permissions_for(channel.guild.me)
        if not (permissions.view_channel and permissions.connect and permissions.speak):
            return "unavailable", None
        if await self.cog.bot.cog_disabled_in_guild(self.cog, channel.guild):
            return "unavailable", None
        if self.task or channel.members or channel.guild.voice_client or channel.guild.me.voice:
            return "busy", None
        return "ready", channel

    async def check(self):
        status, channel = await self.channel_status()
        if status != "ready":
            return status
        self.task = asyncio.current_task()
        self.guild_id = channel.guild.id
        try:
            async with asyncio.timeout(60):
                return await self._play(channel)
        except Exception as exc:
            log.warning("Playback check failed (%s)", type(exc).__name__)
            return "failed"
        finally:
            self.task = None
            self.guild_id = None

    async def _play(self, channel):
        video_id = os.getenv("RED_AUDIO_PROBE_VIDEO_ID", TEST_VIDEO_ID)
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            log.warning("RED_AUDIO_PROBE_VIDEO_ID must be an 11-character YouTube video ID")
            return "failed"
        # VoiceChannel.connect refuses an existing connection instead of moving the player.
        player = None

        def create_player(client, voice_channel):
            nonlocal player
            player = lavalink.Player(client, voice_channel)
            player.store("red_playback_probe", True)
            return player

        try:
            await channel.connect(cls=create_player, self_deaf=True, timeout=10)
            result = await player.load_tracks(f"https://www.youtube.com/watch?v={video_id}")
            if not result.tracks:
                return "failed"
            if any(member != channel.guild.me for member in channel.members):
                return "busy"
            track = result.tracks[0]
            if (
                track._info.get("identifier") != video_id
                or track.is_stream
                or not 15_000 <= track.length <= 35_000
            ):
                log.warning("Playback check requires the selected video to last 15 to 35 seconds")
                return "failed"
            outcome = asyncio.get_running_loop().create_future()
            first = last = None

            def finish(status):
                if not outcome.done():
                    outcome.set_result(status)

            async def on_update(updated_player, state, raw_data):
                nonlocal first, last
                if updated_player is not player or player.current is not track:
                    return
                if any(member != channel.guild.me for member in channel.members):
                    finish("busy")
                elif not state.connected:
                    first = last = None
                elif first is None:
                    first = last = state
                elif state.time > last.time and state.position > last.position:
                    last = state

            async def on_event(updated_player, event, extra):
                if updated_player is not player:
                    return
                if any(member != channel.guild.me for member in channel.members):
                    finish("busy")
                elif event in (
                    lavalink.LavalinkEvents.TRACK_EXCEPTION,
                    lavalink.LavalinkEvents.TRACK_STUCK,
                    lavalink.LavalinkEvents.WEBSOCKET_CLOSED,
                    lavalink.LavalinkEvents.FORCED_DISCONNECT,
                ):
                    finish("failed")
                elif event == lavalink.LavalinkEvents.TRACK_END:
                    # FINISHED can follow a partial stream. Require both connected
                    # playback over time and a position close to the reported end.
                    played = (
                        extra == lavalink.TrackEndReason.FINISHED
                        and first is not None
                        and last.time - first.time >= 10_000
                        and last.position - first.position >= 10_000
                        and last.position >= track.length - 5_000
                    )
                    finish("ok" if played else "failed")

            lavalink.register_update_listener(on_update)
            lavalink.register_event_listener(on_event)
            try:
                await player.set_volume(1)
                player.add(channel.guild.me, track)
                await player.play()
                return await outcome
            finally:
                lavalink.unregister_event_listener(on_event)
                lavalink.unregister_update_listener(on_update)
        finally:
            if player is not None and channel.guild.voice_client is player:
                try:
                    await asyncio.wait_for(player.disconnect(), timeout=10)
                finally:
                    # A node or network failure can interrupt disconnect before cache cleanup.
                    if channel.guild.voice_client is player:
                        with suppress(lavalink.PlayerNotFound):
                            if player.node.get_player(channel.guild.id) is player:
                                await player.update_state(lavalink.enums.PlayerState.DISCONNECTING)
                                player.node.remove_player(player)
                        player.cleanup()
