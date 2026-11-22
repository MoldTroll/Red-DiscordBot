from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from packaging.requirements import Requirement
from packaging.version import Version

from redbot import _fork
from redbot._update.common import get_red_dependency_specifier
from redbot.core import _events
from redbot.core.utils._internal_utils import AvailableVersion
from redbot.core.utils import _internal_utils


async def test_startup_and_reconnect_do_not_poll_for_updates_or_notify_owners(red, monkeypatch):
    monkeypatch.setattr(red._connection, "user", SimpleNamespace(display_name="Red"))
    version = AvailableVersion(Version("99.0"), {}, commit="a" * 40, requires_python=">=3.11")
    available_versions = AsyncMock(return_value=[version])
    monkeypatch.setattr(_internal_utils, "fetch_available_red_versions", available_versions)
    destinations = AsyncMock(return_value=[])
    monkeypatch.setattr(red, "get_owner_notification_destinations", destinations)

    _events.init_events(red, red._cli_flags)
    await red.on_ready()
    await red.on_ready()

    assert red._red_ready.is_set()
    available_versions.assert_not_awaited()
    destinations.assert_not_awaited()


async def test_fork_discovery_reads_metadata_from_resolved_commit(monkeypatch):
    sha = "b" * 40
    source = f"https://raw.githubusercontent.com/{_fork.REPOSITORY}/{sha}"
    branch = f"https://api.github.com/repos/{_fork.REPOSITORY}/commits/{_fork.BRANCH}"
    commit = {"sha": sha, "commit": {"committer": {"date": "2026-01-01T00:00:00Z"}}}
    responses = {
        branch: SimpleNamespace(json=AsyncMock(return_value=commit)),
        f"{source}/redbot/__init__.py": SimpleNamespace(
            text=AsyncMock(return_value='_VERSION = "3.5.25.dev1"')
        ),
        f"{source}/pyproject.toml": SimpleNamespace(
            text=AsyncMock(return_value='[project]\nrequires-python = ">=3.11,<3.16"')
        ),
    }

    @asynccontextmanager
    async def get(url):
        yield responses.pop(url)

    @asynccontextmanager
    async def session(**kwargs):
        yield SimpleNamespace(get=get)

    monkeypatch.setattr(_internal_utils, "_SIMPLE_API_URL", None)
    monkeypatch.setattr(_internal_utils.aiohttp, "ClientSession", session)
    latest = await _internal_utils.fetch_latest_red_version()
    assert not responses
    assert latest.commit == sha
    assert latest.version == Version("3.5.25.dev1767225600+gbbbbbbbbbbbb")
    assert Version("3.15") in latest.requires_python


def test_fork_commit_is_preserved_across_update_stages(monkeypatch):
    monkeypatch.delenv("_RED_UPDATE_PRETEND_SPECIFIER_TEMPLATE", raising=False)
    sha = "a" * 40
    version = Version(_fork.version_from_commit("3.5.25.dev1", 1234567890, sha))
    original = AvailableVersion(version, {}, commit=sha, requires_python=">=3.11,<3.16")
    restored = AvailableVersion.from_json_dict(original.to_json_dict())
    requirement = Requirement(
        get_red_dependency_specifier(restored.version, ["postgres"], restored.commit)
    )

    assert requirement.extras == {"postgres"}
    assert requirement.url == f"git+{_fork.REPOSITORY_URL}.git@{sha}"
    assert restored.requires_python == original.requires_python
    assert not restored.is_newer_than(version)
    assert restored.is_newer_than(Version("3.5.25.dev1234567890+gffffffffffff"))


def test_package_index_update_retains_version_pin(monkeypatch):
    monkeypatch.delenv("_RED_UPDATE_PRETEND_SPECIFIER_TEMPLATE", raising=False)
    requirement = Requirement(get_red_dependency_specifier(Version("3.5.24"), []))
    assert str(requirement.specifier) == "==3.5.24"
    assert requirement.url is None
