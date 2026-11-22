"""Check playback through the bot's own Discord and Lavalink connections."""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import re

import discord
import lavalink

from redbot import _fork
from redbot.core import data_manager


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
            if request in (b"check\n", b"verify\n", b"rollout-check\n", b"rollout-verify\n"):
                result = await self.check(rollout=request.startswith(b"rollout-"))
                if request in (b"verify\n", b"rollout-verify\n") and result == "ok":
                    result = "finished"
            elif request in (b"available\n", b"idle\n"):
                result, _ = await self.channel_status(rollout=request == b"idle\n")
                if result == "occupied":
                    result = "ready"
            elif request.startswith(b"updated "):
                result = await self._notify_update(request)
            else:
                result = "invalid"
            writer.write((result + "\n").encode("ascii"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _notify_update(self, request):
        match = re.fullmatch(
            rb"updated ([0-9a-f]{40}) ([a-z][a-z0-9_]{0,31}(?: [a-z][a-z0-9_]{0,31})*)\n",
            request,
        )
        if not match:
            return "invalid"
        revision = match[1].decode("ascii")
        names = match[2].decode("ascii").split()
        if len(set(names)) != len(names) or data_manager.instance_name() not in names:
            return "invalid"
        dist = metadata.distribution("Red-DiscordBot")
        origin = json.loads(dist.read_text("direct_url.json") or "{}")
        if origin.get("vcs_info", {}).get("commit_id") != revision:
            return "invalid"
        bots = ", ".join(f"`{name}`" for name in names)
        content = f"{bots} 업데이트를 완료했습니다.\n<{_fork.REPOSITORY_URL}/commit/{revision}>"
        bot = self.cog.bot
        await bot.wait_until_red_ready()
        receipt = self.cog.config.rollout_dm_receipt
        async with receipt.get_lock():
            state = await receipt()
            if state.get("revision") != revision:
                state = {"revision": revision, "reports": {}}
            group = " ".join(sorted(names))
            reports = state.setdefault("reports", {})
            if "recipients" in state and len(names) > 1:
                # 구버전은 통합 알림만 보냈다. 재시도 시 기존 전송 기록과 nonce를 유지한다.
                reports[group] = state.pop("recipients")
                state["legacy_group"] = group
                await receipt.set(state)
            recipients = reports.setdefault(group, {})
            nonce_prefix = (
                revision if state.get("legacy_group") == group else f"{revision}:{group}"
            )
            opt_outs = await bot._config.owner_opt_out_list()
            retry = False
            for owner_id in sorted(set(bot.owner_ids).difference(opt_outs)):
                nonce = hashlib.sha256(f"{nonce_prefix}:{owner_id}".encode()).hexdigest()[:24]
                previous = recipients.get(nonce)
                if previous == "done":
                    continue
                try:
                    user = bot.get_user(owner_id) or await bot.fetch_user(owner_id)
                    if not user.bot:
                        dm = await user.create_dm()
                        sent = False
                        if previous:
                            # 응답을 받기 전에 종료됐다면 실제 DM 기록에서 전송 결과를 찾는다.
                            async for message in dm.history(
                                limit=None, after=datetime.fromtimestamp(previous, timezone.utc)
                            ):
                                if message.author.id == bot.user.id and message.content == content:
                                    sent = True
                                    break
                        else:
                            recipients[nonce] = discord.utils.utcnow().timestamp() - 60
                            await receipt.set(state)
                        if not sent:
                            await dm.send(content, nonce=nonce)
                        log.info("Confirmed owner update DM for revision %s.", revision)
                except (discord.Forbidden, discord.NotFound):
                    log.warning("Cannot deliver the rollout DM to an owner for %s.", revision[:12])
                except (discord.HTTPException, OSError, asyncio.TimeoutError):
                    log.warning("Will retry an owner rollout DM for %s.", revision[:12])
                    retry = True
                    continue
                recipients[nonce] = "done"
                await receipt.set(state)
        return "retry" if retry else "notified"

    def _voice_in_use(self, *, probe_channel=None, probe_player=None):
        for guild in self.cog.bot.guilds:
            if guild.voice_client is not None and guild.voice_client is not probe_player:
                return True
            voice = guild.me.voice if guild.me is not None else None
            if voice is not None and (probe_channel is None or voice.channel != probe_channel):
                return True
        return False

    async def _configured_channel(self):
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
        return "ready", channel

    async def channel_status(self, *, rollout=False):
        status, channel = await self._configured_channel()
        if rollout and (self.task or self._voice_in_use()):
            return "busy", None
        if channel is None:
            return status, None
        if self.task or channel.guild.voice_client or channel.guild.me.voice:
            return "busy", None
        if channel.voice_states:
            return ("occupied" if rollout else "busy"), None
        return "ready", channel

    async def check(self, *, rollout=False):
        status, channel = await self.channel_status(rollout=rollout)
        if status != "ready":
            return status
        self.task = asyncio.current_task()
        self.guild_id = channel.guild.id
        try:
            async with asyncio.timeout(60):
                return await self._play(channel, rollout=rollout)
        except Exception as exc:
            log.warning("Playback check failed (%s)", type(exc).__name__)
            return "failed"
        finally:
            self.task = None
            self.guild_id = None

    async def _play(self, channel, *, rollout):
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

        def interrupted():
            if rollout and self._voice_in_use(probe_channel=channel, probe_player=player):
                return "busy"
            if any(member_id != channel.guild.me.id for member_id in channel.voice_states):
                return "occupied" if rollout else "busy"

        try:
            await channel.connect(cls=create_player, self_deaf=True, timeout=10)
            result = await player.load_tracks(f"https://www.youtube.com/watch?v={video_id}")
            if status := interrupted():
                return status
            if not result.tracks:
                return "failed"
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

            async def on_voice_state_update(member, before, after):
                if member.id != channel.guild.me.id and after.channel != channel:
                    return
                if status := interrupted():
                    finish(status)

            async def on_update(updated_player, state, raw_data):
                nonlocal first, last
                if updated_player is not player or player.current is not track:
                    return
                if status := interrupted():
                    finish(status)
                elif not state.connected:
                    first = last = None
                elif first is None:
                    first = last = state
                elif state.time > last.time and state.position > last.position:
                    last = state

            async def on_event(updated_player, event, extra):
                if updated_player is not player:
                    return
                if status := interrupted():
                    finish(status)
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
            self.cog.bot.add_listener(on_voice_state_update)
            try:
                await player.set_volume(1)
                if outcome.done():
                    return outcome.result()
                if status := interrupted():
                    return status
                player.add(channel.guild.me, track)
                await player.play()
                return await outcome
            finally:
                self.cog.bot.remove_listener(on_voice_state_update)
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
