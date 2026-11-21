import logging
from types import SimpleNamespace

from redbot.cogs.audio import _omit_connection_headers
from redbot.cogs.audio.core.utilities.miscellaneous import MiscellaneousUtilities
from redbot.cogs.audio.managed_node.ll_server_config import (
    DEFAULT_LAVALINK_YAML,
    generate_server_config,
)


async def test_migrate_youtube_client_preserves_custom_options(config):
    config.register_global(schema_version=4, **DEFAULT_LAVALINK_YAML)
    group = config.yaml.plugins.youtube
    await group.set(
        {
            "clients": ["WEB", "TVHTML5EMBEDDED", "MUSIC"],
            "clientOptions": {
                "TVHTML5EMBEDDED": {"playback": True, "searching": False},
                "TVHTML5_SIMPLY": {"searching": True},
            },
        }
    )
    await MiscellaneousUtilities.data_schema_migration(SimpleNamespace(config=config), 4, 5)
    assert await config.schema_version() == 5
    stored = await group.get_raw(default={})
    assert stored["clients"] == ["WEB", "TVHTML5_SIMPLY", "MUSIC"]
    assert stored["clientOptions"] == {"TVHTML5_SIMPLY": {"playback": True, "searching": True}}
    generated = generate_server_config(await config.yaml.all())
    assert generated["lavalink"]["plugins"][0]["dependency"].endswith(":1.18.2")
    assert generated["plugins"]["youtube"]["clientOptions"]["TVHTML5_SIMPLY"]["playback"]


def test_lavalink_connection_log_omits_headers():
    record = logging.LogRecord(
        "red.Audio.WS.LLNode",
        logging.INFO,
        __file__,
        1,
        "Lavalink WS connecting to %s with headers %s",
        ("ws://localhost:2333", {"Authorization": "test-password", "Resume-Key": "test-key"}),
        None,
    )
    assert _omit_connection_headers(record)
    assert record.getMessage() == "Lavalink WS connecting to ws://localhost:2333"
