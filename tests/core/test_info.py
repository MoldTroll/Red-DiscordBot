from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from redbot import _fork
from redbot.core import core_commands


@pytest.mark.parametrize("embed", [True, False])
async def test_info_names_the_fork_and_credits_upstream(red, monkeypatch, embed):
    await red.rpc._pre_login()
    cog = core_commands.Core(red)
    ctx = AsyncMock()
    ctx.embed_requested.return_value = embed
    ctx.embed_colour.return_value = 0
    application = SimpleNamespace(team=None, owner="test owner")
    monkeypatch.setattr(red, "application_info", AsyncMock(return_value=application))
    monkeypatch.setattr(
        core_commands, "fetch_latest_red_version", AsyncMock(side_effect=TimeoutError)
    )

    await cog.info.callback(cog, ctx)

    if embed:
        message = ctx.send.await_args.kwargs["embed"]
        about = next(field.value for field in message.fields if field.name == "About Red")
        references = about
    else:
        about, references = [call.args[0] for call in ctx.send.await_args_list]
    assert _fork.REPOSITORY in about
    assert "a fork of" in about
    assert "Cog-Creators/Red-DiscordBot" in about
    assert "Twentysix" in about
    assert _fork.REPOSITORY_URL in references
    assert "https://github.com/Cog-Creators/Red-DiscordBot" in references
