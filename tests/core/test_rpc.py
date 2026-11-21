import subprocess
import sys
from textwrap import dedent

import aiohttp
import pytest

from redbot.pytest.rpc import *
from redbot.core._rpc import get_name


def test_rpc_disabled_startup(tmp_path):
    # A fresh interpreter avoids imports performed by aiohttp-json-rpc's pytest plugin.
    script = dedent(
        """\
        import asyncio
        import sys

        class BlockRpc:
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] == "aiohttp_json_rpc":
                    raise AssertionError(f"Unexpected import: {fullname}")

        sys.meta_path.insert(0, BlockRpc())

        import discord
        from redbot.core import _downloader, data_manager
        from redbot.core._cli import parse_cli_flags
        from redbot.core.bot import Red

        async def main():
            data_manager.basic_config = dict(
                data_manager.basic_config_default,
                DATA_PATH=sys.argv[1], STORAGE_TYPE="JSON", STORAGE_DETAILS={},
            )
            red = Red(cli_flags=parse_cli_flags(["test"]), intents=discord.Intents.default())
            await red._async_setup_hook()
            await _downloader._init(red)
            await red._pre_login()
            await red._pre_connect()
            assert not red.rpc_enabled
            assert red.rpc._site is None
            core = red.get_cog("Core")
            assert core._prefixes in red.rpc_handlers["CORE"]
            await red.remove_cog("Core")
            assert "CORE" not in red.rpc_handlers

        asyncio.run(main())
        """
    )
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, timeout=30)


@pytest.mark.parametrize("red", [("--rpc", "--rpc-port", "0")], indirect=True)
async def test_rpc_enabled_websocket(red):
    from redbot.core import _downloader

    await red._config.schema_version.set(3)
    await red._async_setup_hook()
    await _downloader._init(red)
    await red._pre_login()
    await red._pre_connect()
    try:
        host, port = red.rpc._runner.addresses[0]
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"http://{host}:{port}/") as ws:
                request = {"jsonrpc": "2.0", "id": 1, "method": "CORE__PREFIXES", "params": []}
                await ws.send_json(request)
                result = await ws.receive_json(timeout=5)
                assert result["id"] == 1
                assert result["result"] == await red._prefix_cache.get_prefixes(guild=None)

                await red.remove_cog("Core")
                await ws.send_json(dict(request, id=2))
                result = await ws.receive_json(timeout=5)
                assert result["id"] == 2
                assert result["error"]["code"] == -32601
    finally:
        await red.rpc.close()


def test_get_name(cog):
    assert get_name(cog.cofunc) == "COG__COFUNC"
    assert get_name(cog.cofunc2) == "COG__COFUNC2"
    assert get_name(cog.func) == "COG__FUNC"


def test_internal_methods_exist(rpc):
    assert "GET_METHODS" in rpc._rpc.methods


def test_add_method(rpc, cog):
    rpc.add_method(cog.cofunc)

    assert get_name(cog.cofunc) in rpc._rpc.methods


def test_double_add(rpc, cog):
    rpc.add_method(cog.cofunc)
    count = len(rpc._rpc.methods)

    rpc.add_method(cog.cofunc)

    assert count == len(rpc._rpc.methods)


def test_add_notcoro_method(rpc, cog):
    with pytest.raises(TypeError):
        rpc.add_method(cog.func)


def test_add_multi(rpc, cog):
    funcs = [cog.cofunc, cog.cofunc2, cog.cofunc3]
    rpc.add_multi_method(*funcs)

    names = [get_name(f) for f in funcs]

    assert all(n in rpc._rpc.methods for n in names)


def test_add_multi_bad(rpc, cog):
    funcs = [cog.cofunc, cog.cofunc2, cog.cofunc3, cog.func]

    with pytest.raises(TypeError):
        rpc.add_multi_method(*funcs)

    names = [get_name(f) for f in funcs]

    assert not any(n in rpc._rpc.methods for n in names)


def test_remove_method(rpc, existing_func):
    before_count = len(rpc._rpc.methods)
    rpc.remove_method(existing_func)

    assert get_name(existing_func) not in rpc._rpc.methods
    assert before_count - 1 == len(rpc._rpc.methods)


def test_remove_multi_method(rpc, existing_multi_func):
    before_count = len(rpc._rpc.methods)
    name = get_name(existing_multi_func[0])
    prefix = name.split("__")[0]

    rpc.remove_methods(prefix)

    assert before_count - len(existing_multi_func) == len(rpc._rpc.methods)

    names = [get_name(f) for f in existing_multi_func]

    assert not any(n in rpc._rpc.methods for n in names)


def test_rpcmixin_register(rpcmixin, cog):
    rpcmixin.register_rpc_handler(cog.cofunc)

    rpcmixin.rpc.add_method.assert_called_once_with(cog.cofunc)

    name = get_name(cog.cofunc)
    cogname = name.split("__")[0]

    assert cogname in rpcmixin.rpc_handlers


def test_rpcmixin_unregister(rpcmixin, cog):
    rpcmixin.register_rpc_handler(cog.cofunc)
    rpcmixin.unregister_rpc_handler(cog.cofunc)

    rpcmixin.rpc.remove_method.assert_called_once_with(cog.cofunc)

    name = get_name(cog.cofunc)
    cogname = name.split("__")[0]

    if cogname in rpcmixin.rpc_handlers:
        assert cog.cofunc not in rpcmixin.rpc_handlers[cogname]
