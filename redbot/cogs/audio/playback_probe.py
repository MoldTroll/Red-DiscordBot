"""Check playback through the bot's own Discord and Lavalink connections."""

import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path

import discord
import lavalink


log = logging.getLogger("red.Audio.playback")
TEST_TRACK = "https://www.youtube.com/watch?v=YE7VzlLtp-4"  # Blender's Big Buck Bunny.


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
            if request == b"check\n":
                result = await self.check()
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
        # VoiceChannel.connect refuses an existing connection instead of moving the player.
        player = None

        def create_player(client, voice_channel):
            nonlocal player
            player = lavalink.Player(client, voice_channel)
            player.store("red_playback_probe", True)
            return player

        try:
            await channel.connect(cls=create_player, self_deaf=True, timeout=10)
            result = await player.load_tracks(TEST_TRACK)
            if not result.tracks:
                return "failed"
            if any(member != channel.guild.me for member in channel.members):
                return "busy"
            track = result.tracks[0]
            state_updates = asyncio.Queue()

            async def on_update(updated_player, state, raw_data):
                if updated_player is player and player.current is track:
                    state_updates.put_nowait(state)

            lavalink.register_update_listener(on_update)
            try:
                await player.set_volume(1)
                player.add(channel.guild.me, track)
                await player.play()
                first = None
                while True:
                    state = await state_updates.get()
                    if any(member != channel.guild.me for member in channel.members):
                        return "busy"
                    if not state.connected:
                        first = None
                    elif first is None:
                        first = state
                    elif state.time > first.time and state.position >= first.position + 1000:
                        return "ok"
            finally:
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
