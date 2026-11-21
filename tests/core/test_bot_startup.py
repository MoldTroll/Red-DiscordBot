from unittest.mock import AsyncMock

import pytest

from redbot.core import _downloader, bot


@pytest.mark.parametrize(
    ("lib_contents", "expect_audio"),
    [("empty", True), ("scaffold", True), ("package", False), ("shared_library", False)],
)
async def test_cogs_on_python_upgrade(red, monkeypatch, tmp_path, lib_contents, expect_audio):
    lib = tmp_path / "lib"
    shared = lib / "cog_shared"
    monkeypatch.setattr(_downloader, "LIB_PATH", lib, raising=False)
    monkeypatch.setattr(_downloader, "SHAREDLIB_PATH", shared, raising=False)
    monkeypatch.setattr(_downloader, "_SHAREDLIB_INIT", shared / "__init__.py", raising=False)
    if lib_contents == "empty":
        lib.mkdir()
    else:
        _downloader._create_lib_folder()
    if lib_contents == "package":
        (lib / "old_dependency").mkdir()
    elif lib_contents == "shared_library":
        (shared / "old_shared_library.py").touch()

    await red._config.last_system_info.python_version.set([3, 10])
    await red._config.packages.set(["audio"])
    monkeypatch.setattr(bot.modlog, "_init", AsyncMock())
    monkeypatch.setattr(bot.bank, "_init", AsyncMock())
    monkeypatch.setattr(red, "add_cog", AsyncMock())
    monkeypatch.setattr(red, "load_extension", AsyncMock())
    notice = AsyncMock()
    monkeypatch.setattr(bot, "send_to_owners_with_prefix_replaced", notice)

    await red._pre_connect()

    if expect_audio:
        red.load_extension.assert_awaited_once()
        assert red.load_extension.await_args.args[0].name == "redbot.cogs.audio"
        notice.assert_not_called()
    else:
        red.load_extension.assert_not_awaited()
        notice.assert_called_once()
        assert not any(lib.iterdir())
    assert await red._config.packages() == ["audio"]


async def test_load_and_unload_audio_extension(red):
    await red._async_setup_hook()
    spec = await red._cog_mgr.find_cog("audio")
    await red.load_extension(spec)
    try:
        audio = red.get_cog("Audio")
        assert audio is not None
        assert red.get_command("play").cog is audio
    finally:
        await red.unload_extension("audio")
    assert red.get_command("play") is None
    assert audio.session.closed
