import json
import time
from copy import copy
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Union

import lavalink

from redbot.core import Config
from redbot.core.commands import Cog
from redbot.core.utils.dbtools import APSWConnectionWrapper

from .api_utils import QueueFetchResult

if TYPE_CHECKING:
    from .. import Audio


class QueueInterface:
    def __init__(self, config: Config, conn: APSWConnectionWrapper, cog: Union["Audio", Cog]):
        self.database = conn
        self.config = config
        self.cog = cog

    async def init(self) -> None:
        with self.database.transaction() as cursor:
            columns = {row[1] for row in cursor.execute("PRAGMA table_info(persist_queue)")}
            legacy = "track_id" in columns
            if legacy:
                cursor.execute("ALTER TABLE persist_queue RENAME TO persist_queue_legacy")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS persist_queue (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    room_id INTEGER NOT NULL,
                    track TEXT NOT NULL,
                    position INTEGER
                )
                """
            )
            if legacy:
                # Old rows identified media, so repeated requests may already have been merged.
                cursor.execute(
                    """
                    INSERT INTO persist_queue (guild_id, room_id, track)
                    SELECT guild_id, room_id, track FROM persist_queue_legacy
                    WHERE played = 0 ORDER BY time, rowid
                    """
                )
                cursor.execute("DROP TABLE persist_queue_legacy")
            cursor.execute("CREATE INDEX IF NOT EXISTS queue_guild ON persist_queue (guild_id)")

    def rooms_to_restore(self) -> Dict[int, int]:
        # Use the most recently enqueued outstanding request's voice channel for each guild.
        rows = self.database.cursor().execute(
            """
            SELECT guild_id, room_id FROM persist_queue
            WHERE entry_id IN (SELECT max(entry_id) FROM persist_queue GROUP BY guild_id)
            """
        )
        return dict(rows)

    async def fetch_all(self, guild_id: Optional[int] = None) -> List[QueueFetchResult]:
        sql = "SELECT guild_id, room_id, track, entry_id FROM persist_queue"
        if guild_id is not None:
            sql += " WHERE guild_id = ?"
        sql += " ORDER BY guild_id, coalesce(position, entry_id), entry_id"
        rows = self.database.cursor().execute(sql, () if guild_id is None else (guild_id,))
        return [QueueFetchResult(*row) for row in rows]

    async def prepare_track(
        self, player, track: lavalink.Track, requester, **extras
    ) -> lavalink.Track:
        # A replay, playlist or search result can reuse a Track still held by the player.
        entry = copy(track)
        entry.extras = {
            **track.extras,
            "enqueue_time": int(time.time()),
            "vc": player.channel.id,
            "requester": requester.id,
            **extras,
        }
        entry._red_queue_entry_id = None
        if await self._enabled(player.guild.id):
            self._save(player.guild.id, player.channel.id, entry)
        return entry

    async def _enabled(self, guild_id: int) -> bool:
        cache = self.cog._persist_queue_cache
        if guild_id not in cache:
            cache[guild_id] = await self.config.guild_from_id(guild_id).persist_queue()
        return cache[guild_id]

    def _save(self, guild_id: int, room_id: int, track: lavalink.Track) -> None:
        data = self.cog.track_to_json(track)
        data["extras"] = {
            key: track.extras[key]
            for key in ("requester", "vc", "enqueue_time", "bumped", "autoplay")
            if key in track.extras
        }
        data["info"]["timestamp"] = track.start_timestamp
        cursor = self.database.cursor()
        row = cursor.execute(
            "INSERT INTO persist_queue (guild_id, room_id, track) VALUES (?, ?, ?) RETURNING entry_id",
            (guild_id, room_id, json.dumps(data)),
        ).fetchone()
        track._red_queue_entry_id = row[0]

    async def enqueued(self, guild_id: int, room_id: int, track: lavalink.Track) -> None:
        # Third-party cogs can enqueue a track and dispatch the public event themselves.
        if hasattr(track, "_red_queue_entry_id"):
            return
        enabled = await self._enabled(guild_id)
        if enabled and not hasattr(track, "_red_queue_entry_id"):
            self._save(guild_id, room_id, track)

    def started(self, player, current, previous, persist: bool) -> None:
        self.removed(player.guild.id, [current])
        # Lavalink appends the outgoing track when repeat is on, without an enqueue event.
        if (
            persist
            and player.repeat
            and previous
            and any(track is previous for track in player.queue)
        ):
            if getattr(previous, "_red_queue_entry_id", None) is None:
                self._save(player.guild.id, player.channel.id, previous)
            self.reordered(player)

    def maybe_shuffle(self, player, sticky_songs: int = 1) -> None:
        if player.shuffle:
            player.maybe_shuffle(sticky_songs)
            self.reordered(player)

    def removed(self, guild_id: int, tracks: Iterable[lavalink.Track]) -> None:
        tracks = list(tracks)
        entries = [
            (guild_id, track._red_queue_entry_id)
            for track in tracks
            if getattr(track, "_red_queue_entry_id", None) is not None
        ]
        if entries:
            with self.database.transaction() as cursor:
                cursor.executemany(
                    "DELETE FROM persist_queue WHERE guild_id=? AND entry_id=?", entries
                )
        for track in tracks:
            # A delayed public enqueue event must not reinsert a consumed track.
            track._red_queue_entry_id = None

    def reordered(self, player) -> None:
        queued_ids = (
            track._red_queue_entry_id
            for track in player.queue
            if getattr(track, "_red_queue_entry_id", None) is not None
        )
        entries = [
            (position, player.guild.id, entry_id) for position, entry_id in enumerate(queued_ids)
        ]
        if entries:
            with self.database.transaction() as cursor:
                cursor.executemany(
                    "UPDATE persist_queue SET position=? WHERE guild_id=? AND entry_id=?", entries
                )

    async def drop(self, guild_id: int) -> None:
        self.database.cursor().execute("DELETE FROM persist_queue WHERE guild_id=?", (guild_id,))
