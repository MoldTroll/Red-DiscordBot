import asyncio
import copy
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from redbot.cogs.audio import playback_probe
from redbot.cogs.audio.playback_probe import PlaybackProbe


REVISION = "1a" * 20
REQUEST = f"updated {REVISION} moldsub mold\n".encode()


@pytest.fixture
def notifier(monkeypatch):
    install = Mock()
    install.read_text.return_value = json.dumps({"vcs_info": {"commit_id": REVISION}})
    monkeypatch.setattr(playback_probe.metadata, "distribution", lambda _: install)
    monkeypatch.setattr(playback_probe.data_manager, "instance_name", lambda: "mold")
    state = {}
    receipt = AsyncMock(side_effect=lambda: copy.deepcopy(state))
    receipt.get_lock = Mock(return_value=asyncio.Lock())
    receipt.set = AsyncMock(side_effect=lambda value: state.update(copy.deepcopy(value)))
    bot = SimpleNamespace(
        owner_ids={11, 12, 13},
        user=SimpleNamespace(id=30),
        _config=SimpleNamespace(owner_opt_out_list=AsyncMock(return_value=[12])),
        wait_until_red_ready=AsyncMock(),
        get_user=Mock(return_value=None),
        fetch_user=AsyncMock(),
    )
    dms = {}
    for owner_id in (11, 13):
        messages = []
        dm = SimpleNamespace(messages=messages, send=AsyncMock())

        async def history(*, limit, after, messages=messages):
            for message in messages:
                yield message

        dm.history = history
        dms[owner_id] = dm
    bot.fetch_user.side_effect = lambda uid: SimpleNamespace(
        bot=False, create_dm=AsyncMock(return_value=dms[uid])
    )
    cog = SimpleNamespace(bot=bot, config=SimpleNamespace(rollout_dm_receipt=receipt))
    return SimpleNamespace(cog=cog, probe=PlaybackProbe(cog), dms=dms, bot=bot)


async def test_rollout_dm_only_to_eligible_owners_once_across_retries(notifier):
    n = notifier
    assert await asyncio.gather(*(n.probe._notify_update(REQUEST) for _ in range(2))) == [
        "notified",
        "notified",
    ]
    assert await PlaybackProbe(n.cog)._notify_update(REQUEST) == "notified"
    assert [call.args[0] for call in n.bot.fetch_user.await_args_list] == [11, 13]
    for dm in n.dms.values():
        dm.send.assert_awaited_once()
        content = dm.send.await_args.args[0]
        assert "`moldsub`, `mold` 업데이트를 완료했습니다." in content
        assert f"https://github.com/MoldTroll/Red-DiscordBot/commit/{REVISION}" in content
    assert n.dms[11].send.await_args.kwargs["nonce"] != n.dms[13].send.await_args.kwargs["nonce"]


@pytest.mark.parametrize("accepted", [True, False])
async def test_rollout_dm_recovers_ambiguous_delivery_after_restart(notifier, accepted):
    n = notifier
    dm = n.dms[11]

    async def disconnect(content, *, nonce):
        if accepted:
            dm.messages.append(SimpleNamespace(author=n.bot.user, content=content))
        raise OSError("connection closed")

    dm.send.side_effect = disconnect
    assert await n.probe._notify_update(REQUEST) == "retry"
    dm.send.side_effect = None
    assert await PlaybackProbe(n.cog)._notify_update(REQUEST) == "notified"
    assert dm.send.await_count == (1 if accepted else 2)
    assert dm.send.await_args.kwargs["nonce"] == dm.send.await_args_list[0].kwargs["nonce"]
    n.dms[13].send.assert_awaited_once()


async def test_rollout_dm_closed_dms_do_not_block_other_owners(notifier, caplog):
    caplog.set_level(logging.INFO, logger="red.Audio.playback")
    notifier.dms[11].send.side_effect = discord.Forbidden(Mock(status=403, reason="Forbidden"), "")
    assert await notifier.probe._notify_update(REQUEST) == "notified"
    assert await notifier.probe._notify_update(REQUEST) == "notified"
    for dm in notifier.dms.values():
        dm.send.assert_awaited_once()
    assert caplog.messages.count(f"Confirmed owner update DM for revision {REVISION}.") == 1


@pytest.mark.parametrize(
    "request_line",
    [
        b"updated foobar moldsub mold\n",
        REQUEST.replace(b"1a", b"2b"),
        REQUEST.replace(b"moldsub mold", b"mold moldsub"),
        REQUEST.replace(b"moldsub mold", b"mold mold"),
        REQUEST.replace(b"moldsub", b"@everyone"),
    ],
)
async def test_rollout_dm_socket_rejects_incorrect_revision_or_instances(notifier, request_line):
    reader = asyncio.StreamReader()
    reader.feed_data(request_line)
    writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
    await notifier.probe._serve(reader, writer)
    writer.write.assert_called_once_with(b"invalid\n")
    notifier.bot.fetch_user.assert_not_awaited()
