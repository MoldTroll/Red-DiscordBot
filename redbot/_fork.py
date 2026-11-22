"""Identity shared by fork builds and update discovery."""

import re


REPOSITORY = "MoldTroll/Red-DiscordBot"
BRANCH = "V3/develop"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
# Bump when an older runtime can no longer read data written by this release.
# Covers persisted Red configuration and bundled cogs, including the Audio queue.
ROLLBACK_DATA_FORMAT = 2  # Format 2 distinguishes held queue entries from active requests.


def version_from_commit(base_version: str, timestamp: int, revision: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Invalid Git commit SHA")
    if base_version.endswith(".dev1"):
        return f"{base_version[:-1]}{timestamp}+g{revision[:12]}"
    return base_version
