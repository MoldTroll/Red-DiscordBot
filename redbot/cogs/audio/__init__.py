import logging

import lavalink

from redbot.core.bot import Red

from .core import Audio


def _omit_connection_headers(record: logging.LogRecord) -> bool:
    # Red-Lavalink 0.11.1 includes the password and resume key in this INFO record.
    if record.msg == "Lavalink WS connecting to %s with headers %s":
        record.msg = "Lavalink WS connecting to %s"
        record.args = record.args[:1]
    return True


async def setup(bot: Red) -> None:
    lavalink.ws_ll_log.addFilter(_omit_connection_headers)
    try:
        cog = Audio(bot)
        await bot.add_cog(cog)
        cog.start_up_task()
    except BaseException:
        lavalink.ws_ll_log.removeFilter(_omit_connection_headers)
        raise


async def teardown(bot: Red) -> None:
    lavalink.ws_ll_log.removeFilter(_omit_connection_headers)
