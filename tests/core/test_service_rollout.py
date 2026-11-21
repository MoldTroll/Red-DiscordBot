from io import BytesIO
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools.redbot import update


def test_offline_snapshot_restores_json_and_sqlite_files(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "sync_dir", lambda path: None)
    core = tmp_path / "core"
    audio = tmp_path / "cogs" / "Audio"
    core.mkdir()
    audio.mkdir(parents=True)
    settings = core / "settings.json"
    db = audio / "Audio.db"
    wal = audio / "Audio.db-wal"
    unrelated = audio / "song.mp3"
    settings.write_bytes(b'{"packages":["audio"]}')
    db.write_bytes(b"old database")
    wal.write_bytes(b"old transaction")
    unrelated.write_bytes(b"original song")
    archive = BytesIO()
    update.snapshot(tmp_path, archive)
    settings.write_bytes(b"{}")
    db.write_bytes(b"migrated database")
    wal.unlink()
    (audio / "Audio.db-shm").touch()
    unrelated.write_bytes(b"changed song")

    archive.seek(0)
    update.restore(tmp_path, archive)
    assert settings.read_bytes() == b'{"packages":["audio"]}'
    assert db.read_bytes() == b"old database"
    assert wal.read_bytes() == b"old transaction"
    assert not (audio / "Audio.db-shm").exists()
    assert unrelated.read_bytes() == b"changed song"


@pytest.fixture()
def rollout(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "STATE", tmp_path)
    monkeypatch.setattr(update, "JOURNAL", tmp_path / "transaction.json")
    monkeypatch.setattr(update, "sync_dir", lambda path: None)
    updater = object.__new__(update.Rollout)
    updater.names = ["canary", "primary"]
    updater.root = tmp_path
    updater.data = {name: tmp_path / "data" / name for name in updater.names}
    backup = tmp_path / "abbacaba8765-test"
    backup.mkdir()
    links = tmp_path / "instances"
    links.mkdir()
    sha = "a" * 40
    candidate = tmp_path / "releases" / f"{sha}-{'b' * 32}"
    candidate.mkdir(parents=True)
    tx = {
        "instances": {},
        "data": {name: str(path) for name, path in updater.data.items()},
        "candidate": str(candidate),
        "backup": str(backup),
        "sha": sha,
    }
    running = {}
    for name in updater.names:
        (links / name).mkdir()
        running[name] = True
        tx["instances"][name] = {"old": str(links / name), "phase": "pending"}
    update.save(tx)
    operations = []

    def execute(*args, **kwargs):
        if args[0] == "systemctl":
            _, action, unit = args
            name = unit.split("@")[1].split(".")[0]
            running[name] = action == "start"
        elif args[0] in ("snapshot", "restore", "switch"):
            action, path = args[:2]
            name = Path(path).name
            assert not running[name]
            if action == "snapshot":
                kwargs["stdout"].write(b"backup")
            if action == "restore":
                assert kwargs["stdin"].read() == b"backup"
        else:
            raise AssertionError(args)
        operations.append((action, name))

    def health(name, sha=None):
        assert running[name]
        operations.append(("new_health" if sha else "old_health", name))

    monkeypatch.setattr(update, "run", execute)
    monkeypatch.setattr(update, "active", running.__getitem__)
    monkeypatch.setattr(updater, "helper", lambda *args: args)
    monkeypatch.setattr(updater, "idle", lambda **kwargs: None)
    monkeypatch.setattr(updater, "health", health)
    monkeypatch.setattr(updater, "discard", Mock())
    return updater, tx, operations


def test_update_checks_canary_before_primary(rollout):
    updater, tx, operations = rollout
    updater.apply(tx)
    assert operations.index(("new_health", "canary")) < operations.index(("stop", "primary"))
    assert operations[-1] == ("new_health", "primary")
    assert not update.JOURNAL.exists()


def test_second_service_failure_restores_both_offline(rollout, monkeypatch):
    updater, tx, operations = rollout
    health = updater.health

    def reject_primary(name, sha=None):
        if sha and name == "primary":
            raise RuntimeError("failed primary probe")
        health(name, sha)

    monkeypatch.setattr(updater, "health", reject_primary)
    with pytest.raises(RuntimeError, match="failed primary probe"):
        updater.apply(tx)
    assert [name for action, name in operations if action == "restore"] == ["primary", "canary"]
    assert operations[-1] == ("old_health", "canary")
    assert not update.JOURNAL.exists()


def test_interrupted_rollout_recovers_without_network_or_new_release(rollout):
    updater, tx, operations = rollout
    tx["instances"]["canary"]["phase"] = "backed_up"
    archive = Path(tx["backup"]) / "canary.tar.gz"
    archive.write_bytes(b"backup")
    tx["instances"]["canary"]["checksum"] = update.checksum(archive)
    update.save(tx)
    updater.update()
    assert ("restore", "canary") in operations
    assert all(name == "canary" for _, name in operations)
    assert not update.JOURNAL.exists()


def test_listener_between_switches_defers_then_resumes(rollout, monkeypatch):
    updater, tx, operations = rollout
    monkeypatch.setattr(updater, "idle", Mock(side_effect=[None, update.Busy()]))
    with pytest.raises(update.Busy):
        updater.apply(tx)
    assert ("stop", "primary") not in operations
    assert json.loads(update.JOURNAL.read_text())["deferred"]
    monkeypatch.setattr(updater, "idle", lambda **kwargs: None)
    updater.update()
    assert operations.count(("stop", "canary")) == 1
    assert operations[-1] == ("new_health", "primary")


def test_instance_names_must_be_unique_and_safe():
    assert update.instance_names("test01 main") == ["test01", "main"]
    for names in ("main", "main main", "main ../other", "main --other"):
        with pytest.raises(ValueError):
            update.instance_names(names)


def test_failed_candidate_build_discards_only_its_new_directory(rollout, monkeypatch):
    updater, tx, operations = rollout
    updater.python = Path("python")
    updater.user = "service"

    def fail_build(*args, **kwargs):
        if "venv" in args:
            raise RuntimeError("venv creation failed")

    monkeypatch.setattr(update, "run", fail_build)
    with pytest.raises(RuntimeError, match="venv creation failed"):
        updater.prepare(tx["sha"])
    updater.discard.assert_called_once()
    path = updater.discard.call_args.args[0]
    assert path.parent == updater.root / "releases"
    assert path.name.startswith(tx["sha"] + "-")
    assert not (update.STATE / "staging.json").exists()
    assert not operations


def test_cleanup_retains_active_previous_and_bootstrap(rollout, monkeypatch):
    updater, tx, _ = rollout
    previous = str(updater.root / "releases" / "previous")
    retired = str(updater.root / "releases" / "retired")
    bootstrap = updater.root / "initial-environment"
    bootstrap.mkdir()
    tx["instances"]["canary"]["old"] = previous
    tx["instances"]["primary"]["old"] = str(bootstrap)
    paths = [update.STATE / ("a" * 12 + f"-old{i}") for i in range(2)]
    for path in paths:
        path.mkdir()
    ledger = update.STATE / "managed.json"
    update.save({"releases": [previous, retired], "backups": list(map(str, paths))}, ledger)
    monkeypatch.setattr(updater, "link", lambda name: Path(tx["candidate"]))
    updater.complete(tx, committed=True)
    updater.discard.assert_called_once_with(Path(retired))
    result = json.loads(ledger.read_text())
    assert set(result["releases"]) == {previous, tx["candidate"]}
    assert result["backups"] == [str(paths[1]), tx["backup"]]
    assert bootstrap.exists()
    assert not paths[0].exists()


def test_bad_snapshot_keeps_recovery_available(rollout):
    updater, tx, operations = rollout
    tx["instances"]["canary"].update(phase="backed_up", checksum="invalid")
    (Path(tx["backup"]) / "canary.tar.gz").write_bytes(b"damaged")
    update.save(tx)
    with pytest.raises(RuntimeError, match="integrity check"):
        updater.update()
    assert not operations
    assert update.JOURNAL.exists()
