import asyncio
import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import lavalink
import pytest

from redbot.cogs.audio.apis.persist_queue_wrapper import QueueInterface
from redbot.cogs.audio.core.commands.audioset import AudioSetCommands
from redbot.cogs.audio.core.commands.controller import PlayerControllerCommands
from redbot.cogs.audio.core.commands.queue import QueueCommands
from redbot.cogs.audio.core.events.cog import AudioEvents
from redbot.cogs.audio.core.utilities.miscellaneous import MiscellaneousUtilities
from redbot.cogs.audio.core.utilities.player import PlayerUtilities
from redbot.core.utils.dbtools import APSWConnectionWrapper


@pytest.fixture
def music(tmp_path, monkeypatch):
    path = tmp_path / "Audio.db"
    conn = APSWConnectionWrapper(path)
    guild = SimpleNamespace(id=42)
    channel = SimpleNamespace(id=74, members=[])
    author = SimpleNamespace(id=9, voice=SimpleNamespace(channel=channel))
    ctx = SimpleNamespace(guild=guild, author=author, channel=channel)
    player = SimpleNamespace(
        guild=guild, channel=channel, current=None, repeat=False, queue=[], store=Mock()
    )

    async def play():
        if player.repeat and player.current:
            player.queue.append(player.current)
        player.current = player.queue.pop(0)

    player.play = play
    monkeypatch.setattr(lavalink, "get_player", lambda _: player)
    guild_config = SimpleNamespace(
        dj_enabled=AsyncMock(return_value=False), auto_play=AsyncMock(return_value=False)
    )
    cog = SimpleNamespace(
        config=SimpleNamespace(guild=lambda _: guild_config),
        _dj_status_cache={},
        _persist_queue_cache={42: True},
        _player_check=lambda _: True,
        _can_instaskip=AsyncMock(return_value=True),
        get_track_description=AsyncMock(return_value="song"),
        local_folder_current_path=None,
        send_embed_msg=AsyncMock(),
        bot=Mock(),
        track_to_json=lambda track: MiscellaneousUtilities.track_to_json(None, track),
    )
    api = QueueInterface(cog.config, conn, cog)
    cog.api_interface = SimpleNamespace(persistent_queue_api=api)
    yield SimpleNamespace(
        path=path, conn=conn, guild=guild, ctx=ctx, player=player, cog=cog, api=api
    )
    conn.close()


def track(title):
    return lavalink.Track(
        {"track": title, "info": {"title": title, "uri": f"https://example.org/{title}"}}
    )


async def add(m, source, author=None):
    entry = await m.api.prepare_track(m.player, source, author or m.ctx.author)
    entry.requester = author or m.ctx.author
    m.player.queue.append(entry)
    return entry


async def reopen(m):
    with closing(APSWConnectionWrapper(m.path)) as database:
        api = QueueInterface(m.cog.config, database, m.cog)
        await api.init()
        return [row.track_object for row in await api.fetch_all()]


async def test_duplicate_play_remove_and_restart(music):
    m = music
    await m.api.init()
    source = track("same")
    source.start_timestamp = 9000
    source.extras["third_party_handle"] = object()
    first = await add(m, source)
    removable = await add(m, source)
    survivor = await add(m, source, SimpleNamespace(id=28))
    assert (
        len(
            {
                first._red_queue_entry_id,
                removable._red_queue_entry_id,
                survivor._red_queue_entry_id,
            }
        )
        == 3
    )
    assert first is not removable and removable.extras is not survivor.extras

    await m.player.play()
    m.api.started(m.player, first, None, True)
    await PlayerControllerCommands.command_remove.callback(m.cog, m.ctx, 1)
    await m.api.enqueued(m.guild.id, m.player.channel.id, removable)
    (restored,) = await reopen(m)
    assert restored._red_queue_entry_id == survivor._red_queue_entry_id
    assert restored.extras["requester"] == 28
    assert restored.extras["vc"] == m.player.channel.id
    assert restored.start_timestamp == 9000
    assert "third_party_handle" not in restored.extras
    await QueueCommands.command_queue_clear.callback(m.cog, m.ctx)
    assert await reopen(m) == []


async def test_skip_to_preserves_only_pending_tracks(music):
    m = music
    await m.api.init()
    m.player.current = track("earlier")
    for title in ["A", "B", "C", "D"]:
        await add(m, track(title))
    await PlayerUtilities._skip_action(m.cog, m.ctx, 3)
    m.api.started(m.player, m.player.current, None, True)
    assert [t.title for t in await reopen(m)] == ["D"]


async def test_repeat_skip_preserves_new_order_and_repeated_entry(music):
    m = music
    await m.api.init()
    previous = await add(m, track("old"))
    await m.player.play()
    m.api.started(m.player, previous, None, True)
    m.player.repeat = True
    for title in ["A", "B", "C", "D"]:
        await add(m, track(title))
    await PlayerUtilities._skip_action(m.cog, m.ctx, 3)
    m.api.started(m.player, m.player.current, previous, True)
    assert [t.title for t in m.player.queue] == ["D", "old", "A", "B"]
    assert [t.title for t in await reopen(m)] == ["D", "old", "A", "B"]


async def test_end_events_leave_new_requests_intact(music, monkeypatch):
    m = music
    await m.api.init()
    entry = await add(m, track("new"))
    m.cog.api_interface.local_cache_api = SimpleNamespace(youtube=AsyncMock())
    m.cog.playlist_api = AsyncMock()
    monkeypatch.setattr("redbot.cogs.audio.core.events.cog.asyncio.sleep", AsyncMock())
    await AudioEvents.on_red_audio_track_end(m.cog, m.guild, track("old"), m.ctx.author)
    await AudioEvents.on_red_audio_queue_end(m.cog, m.guild, track("old"), m.ctx.author)
    assert [t._red_queue_entry_id for t in await reopen(m)] == [entry._red_queue_entry_id]


async def test_failure_hold_survives_restart_without_auto_play(music):
    m = music
    await m.api.init()
    first = await add(m, track("A"))
    duplicate = await add(m, track("A"), SimpleNamespace(id=28))
    other = await add(m, track("B"))
    m.api.hold(m.guild.id, [other, duplicate, first])
    assert m.api.rooms_to_restore() == {}
    assert await reopen(m) == []
    await m.api.drop(m.guild.id)  # automatic empty-channel cleanup
    with closing(APSWConnectionWrapper(m.path)) as db:
        recovered = QueueInterface(m.cog.config, db, m.cog)
        await recovered.init()
        assert [t.title for t in recovered.held(m.guild.id)] == ["B", "A", "A"]
        m.guild.get_member = lambda uid: SimpleNamespace(id=uid)
        m.guild.me = m.ctx.author
        m.player.add = lambda requester, t: m.player.queue.append(t)
        m.player.queue = []
        recovered.release_held(m.player)
        assert [t.extras["requester"] for t in m.player.queue] == [9, 28, 9]
        assert recovered.held(m.guild.id) == []
        assert [t.title for t in await reopen(m)] == ["B", "A", "A"]
        assert recovered.release_held(m.player) == [] and len(m.player.queue) == 3


async def test_nonpersistent_queue_hold_is_memory_only(music):
    m = music
    await m.api.init()
    m.cog._persist_queue_cache[m.guild.id] = False
    entry = await add(m, track("ephemeral"))
    m.api.hold(m.guild.id, [entry])
    assert m.api.held(m.guild.id) == [entry]
    with closing(APSWConnectionWrapper(m.path)) as db:
        assert QueueInterface(m.cog.config, db, m.cog).held(m.guild.id) == []
    m.api.clear_held(m.guild.id)
    assert m.api.held(m.guild.id) == []


async def test_persist_toggle_keeps_live_queue_and_honors_restart(music):
    m = music
    await m.api.init()
    pending = await add(m, track("active"))
    saved = await add(m, track("held"))
    m.api.hold(m.guild.id, [saved])
    m.player.queue = [pending]
    setting = AsyncMock(return_value=True)
    setting.get_lock = Mock(return_value=asyncio.Lock())

    async def change(value):
        setting.return_value = value

    setting.set = AsyncMock(side_effect=change)
    m.cog.config.guild = lambda _: SimpleNamespace(persist_queue=setting)
    toggle = AudioSetCommands.command_audioset_persist_queue.callback
    await toggle(m.cog, m.ctx)
    assert m.player.queue == [pending] and m.api.held(m.guild.id) == [saved]
    assert saved._red_queue_entry_id is None and pending._red_queue_entry_id is None
    with closing(APSWConnectionWrapper(m.path)) as disk:
        fresh = QueueInterface(m.cog.config, disk, m.cog)
        await fresh.init()
        assert list(disk.cursor().execute("SELECT * FROM persist_queue")) == []
        assert fresh.held(m.guild.id) == [] and fresh.rooms_to_restore() == {}

    await toggle(m.cog, m.ctx)
    assert m.cog._persist_queue_cache[m.guild.id] is True
    assert pending._red_queue_entry_id and saved._red_queue_entry_id
    with closing(APSWConnectionWrapper(m.path)) as disk:
        fresh = QueueInterface(m.cog.config, disk, m.cog)
        await fresh.init()
        assert [r.track_object.title for r in await fresh.fetch_all()] == ["active"]
        assert [t.title for t in fresh.held(m.guild.id)] == ["held"]


@pytest.mark.parametrize("connected", [True, False])
async def test_pause_directs_to_resume_without_invoking_it(music, connected):
    m = music
    await m.api.init()
    saved = await add(m, track("held"))
    m.api.hold(m.guild.id, [saved])
    m.player.queue = []
    m.cog._player_check = lambda _: connected
    m.ctx.clean_prefix = "^"
    await PlayerControllerCommands.command_pause.callback(m.cog, m.ctx)
    assert "^queue resume" in m.cog.send_embed_msg.await_args.kwargs["description"]
    assert m.api.held(m.guild.id) == [saved] and m.player.current is None


@pytest.mark.parametrize("busy", [False, True])
async def test_queue_resume_refuses_busy_player_and_is_repeatable(music, busy):
    m = music
    await m.api.init()
    first = await add(m, track("A"))
    last = await add(m, track("B"))
    m.api.hold(m.guild.id, [first, last])
    m.player.queue = []
    m.player.current = track("someone else's song") if busy else None
    m.guild.voice_client = m.player
    m.guild.get_member = lambda member_id: m.ctx.author
    m.guild.me = m.ctx.author
    m.player.add = lambda requester, t: m.player.queue.append(t)
    m.cog._eq_check = AsyncMock()
    m.cog.set_player_settings = AsyncMock()
    m.cog.rgetattr = MiscellaneousUtilities.rgetattr.__get__(m.cog)
    m.cog._error_counter = {m.guild.id: 5}
    await QueueCommands.command_queue_resume.callback(m.cog, m.ctx)
    if busy:
        assert m.player.current.title == "someone else's song"
        assert m.api.held(m.guild.id) == [first, last]
        assert m.player.queue == []
    else:
        assert m.player.current is first
        assert m.player.queue == [last] and m.api.held(m.guild.id) == []
        assert m.cog._error_counter[m.guild.id] == 0
        await QueueCommands.command_queue_resume.callback(m.cog, m.ctx)
        assert m.player.queue == [last]


async def test_resume_restashes_only_its_own_entries_on_transport_error(music):
    m = music
    await m.api.init()
    held = await add(m, track("held"))
    later = await add(m, track("new"))
    m.api.hold(m.guild.id, [held])
    m.player.queue = []
    m.guild.voice_client = m.player
    m.guild.get_member = lambda member_id: m.ctx.author
    m.guild.me = m.ctx.author
    m.player.add = lambda requester, t: m.player.queue.append(t)
    m.cog._eq_check = AsyncMock()
    m.cog.set_player_settings = AsyncMock()
    m.cog.rgetattr = MiscellaneousUtilities.rgetattr.__get__(m.cog)
    m.cog._error_counter = {}

    async def fail():
        m.player.current = m.player.queue.pop(0)
        m.player.queue.append(later)
        raise ConnectionError

    m.player.play = fail
    await QueueCommands.command_queue_resume.callback(m.cog, m.ctx)
    assert "still saved" in m.cog.send_embed_msg.await_args.kwargs["title"]
    assert m.api.held(m.guild.id) == [held]
    assert m.player.current is None and m.player.queue == [later]
    assert [t.title for t in await reopen(m)] == ["new"]


async def test_migrate_previous_entry_id_schema(music):
    m = music
    cur = m.conn.cursor()
    cur.execute(
        "CREATE TABLE persist_queue(entry_id INTEGER PRIMARY KEY, guild_id INTEGER, room_id INTEGER, track TEXT, position INTEGER)"
    )
    cur.execute(
        "INSERT INTO persist_queue VALUES(17,42,74,?,NULL)",
        (json.dumps({"track": "before", "info": {"title": "before"}}),),
    )
    await m.api.init()
    await m.api.init()
    assert m.api.rooms_to_restore() == {42: 74}
    assert [t.title for t in await reopen(m)] == ["before"]


async def test_migrate_legacy_queue_is_ordered_and_idempotent(music):
    m = music
    cur = m.conn.cursor()
    cur.execute(
        """CREATE TABLE persist_queue (
            guild_id INTEGER, room_id INTEGER, track JSON, played BOOLEAN,
            track_id TEXT, time INTEGER, PRIMARY KEY (guild_id, room_id, track_id)
        )"""
    )
    for guild, name, played, timestamp in [
        (2, "b", 0, 6),
        (1, "a", 0, 4),
        (2, "c", 0, 5),
        (1, "gone", 1, 2),
    ]:
        data = json.dumps({"track": name, "info": {"title": name}})
        cur.execute(
            "INSERT INTO persist_queue VALUES (?, 74, ?, ?, ?, ?)",
            (guild, data, played, name, timestamp),
        )
    await m.api.init()
    await m.api.init()
    rows = await m.api.fetch_all()
    assert [(row.guild_id, row.track_object.title) for row in rows] == [
        (1, "a"),
        (2, "c"),
        (2, "b"),
    ]
    assert all(row.track_object.extras["vc"] == 74 for row in rows)
    m.api.removed(2, [rows[1].track_object])
    assert [t.title for t in await reopen(m)] == ["a", "b"]


async def test_queue_shuffle_never_moves_existing_player(music, monkeypatch):
    m = music
    await m.api.init()
    await add(m, track("first"))
    await add(m, track("second"))
    m.ctx.author.voice.channel = SimpleNamespace(id=99)
    m.ctx.command = Mock()
    m.cog._can_instaskip.return_value = False
    m.player.force_shuffle = Mock(side_effect=lambda _: m.player.queue.reverse())
    connect = AsyncMock()
    monkeypatch.setattr(lavalink, "connect", connect)
    await QueueCommands.command_queue_shuffle.callback(m.cog, m.ctx)
    connect.assert_not_awaited()
    m.player.force_shuffle.assert_not_called()
    m.ctx.command.reset_cooldown.assert_called_once_with(m.ctx)
    m.ctx.author.voice.channel = m.player.channel
    await QueueCommands.command_queue_shuffle.callback(m.cog, m.ctx)
    m.player.force_shuffle.assert_called_once_with(0)
    connect.assert_not_awaited()
    assert [t.title for t in await reopen(m)] == ["second", "first"]


@pytest.mark.parametrize("remove_during_setup", [False, True])
async def test_restore_uses_pending_entries_after_voice_setup(
    music, monkeypatch, remove_during_setup
):
    from redbot.cogs.audio.core.tasks.startup import StartUpTasks

    m = music
    await m.api.init()
    first = await add(m, track("first"))
    second = await add(m, track("second"))
    m.player.queue = [first]
    m.player.is_playing = True
    m.player.volume = 0

    async def set_volume(volume):
        if remove_during_setup:
            m.api.removed(m.guild.id, [second])
        m.player.volume = volume

    m.player.set_volume = set_volume
    m.player.add = lambda author, entry: m.player.queue.append(entry)
    m.guild.me = SimpleNamespace(id=10)
    m.guild.get_member = lambda member_id: m.ctx.author if member_id == m.ctx.author.id else None
    m.cog.bot.get_guild.return_value = m.guild
    settings = {
        "repeat": False,
        "shuffle": False,
        "volume": 42,
        "shuffle_bumped": False,
        "auto_deafen": True,
    }
    group = SimpleNamespace(
        persist_queue=AsyncMock(return_value=True), all=AsyncMock(return_value=settings)
    )
    m.cog.config = SimpleNamespace(
        all_guilds=AsyncMock(return_value={}), guild=lambda g: group, guild_from_id=lambda _: group
    )
    m.cog.lavalink_connection_aborted = False
    monkeypatch.setattr(lavalink, "get_all_nodes", lambda: [SimpleNamespace(ready=True)])
    await StartUpTasks.restore_players(m.cog)
    await StartUpTasks.restore_players(m.cog)
    expected = ["first"] if remove_during_setup else ["first", "second"]
    assert [t.title for t in m.player.queue] == expected
    assert m.player.queue[0] is first
