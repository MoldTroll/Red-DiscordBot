from io import BytesIO
from importlib import metadata
import json
from pathlib import Path, PurePosixPath
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.redbot import update


def test_runtime_music_fingerprint_covers_audio_and_dependencies(tmp_path, monkeypatch):
    audio = PurePosixPath("redbot/cogs/audio/queue.py")
    other = PurePosixPath("redbot/cogs/trivia/trivia.py")
    for part in (audio, other):
        path = tmp_path / part
        path.parent.mkdir(parents=True)
        path.write_text("pass")
    red = SimpleNamespace(
        files=[audio, other],
        locate_file=lambda p: tmp_path / p,
        metadata={"Name": "Red-DiscordBot"},
        version="1",
        read_text=lambda _: None,
    )
    dep = SimpleNamespace(metadata={"Name": "red-lavalink"}, version="1")
    monkeypatch.setattr(metadata, "distribution", lambda _: red)
    monkeypatch.setattr(metadata, "distributions", lambda: [red, dep])
    old = update.runtime_info()["music"]
    assert old
    red.version = "2"
    (tmp_path / other).write_text("pass # unrelated")
    assert update.runtime_info()["music"] == old
    (tmp_path / audio).write_text("pass # new audio")
    changed = update.runtime_info()["music"]
    assert changed != old
    dep.version = "2"
    assert update.runtime_info()["music"] != changed
    red.files = None
    assert update.runtime_info()["music"] is None


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
        elif args[0] in ("snapshot", "recovery-snapshot", "restore", "switch"):
            action, path = args[:2]
            name = Path(path).name
            assert not running[name]
            if action == "snapshot":
                kwargs["stdout"].write(b"backup")
            if action == "recovery-snapshot":
                kwargs["stdout"].write(b"current user data")
            if action == "restore":
                assert kwargs["stdin"].read() == b"backup"
        else:
            raise AssertionError(args)
        operations.append((action, name))

    def health(name, sha=None, **kwargs):
        assert running[name]
        operations.append(("new_health" if sha else "old_health", name))

    monkeypatch.setattr(update, "run", execute)
    monkeypatch.setattr(update, "active", running.__getitem__)
    monkeypatch.setattr(updater, "helper", lambda *args: args)
    monkeypatch.setattr(updater, "idle", lambda **kwargs: None)
    monkeypatch.setattr(updater, "health", health)
    monkeypatch.setattr(updater, "invocation", lambda name: name + "-running")
    monkeypatch.setattr(updater, "connected", lambda name, runtime=None: running[name])
    monkeypatch.setattr(updater, "revision", lambda link: sha)
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

    def reject_primary(name, sha=None, **kwargs):
        if sha and name == "primary":
            raise RuntimeError("failed primary probe")
        health(name, sha)

    monkeypatch.setattr(updater, "health", reject_primary)
    with pytest.raises(RuntimeError, match="failed primary probe"):
        updater.apply(tx)
    assert [name for action, name in operations if action == "restore"] == ["primary", "canary"]
    assert operations[-1] == ("old_health", "canary")
    assert not update.JOURNAL.exists()


@pytest.mark.parametrize(
    "old_schema, current_schema, keep",
    [("schema-1", "schema-1", True), ("schema-1", "schema-2", False), (None, "", False)],
)
def test_rollback_of_serving_canary_keeps_or_archives_user_changes(
    rollout, monkeypatch, old_schema, current_schema, keep
):
    updater, tx, operations = rollout
    archive = Path(tx["backup"]) / "canary.tar.gz"
    archive.write_bytes(b"backup")
    row = tx["instances"]["canary"]
    row.update(phase="ready", checksum=update.checksum(archive), audio_schema=old_schema)
    monkeypatch.setattr(update, "output", lambda *args: current_schema)
    updater.rollback_instance(tx, "canary")
    recovery = archive.with_name("canary-before-rollback.tar.gz")
    assert recovery.read_bytes() == b"current user data"
    assert update.checksum(recovery) == row["recovery_checksum"]
    assert (("restore", "canary") in operations) is not keep
    assert operations.index(("recovery-snapshot", "canary")) < operations.index(
        ("switch", "canary")
    )


def test_interrupted_incompatible_restore_does_not_overwrite_recovery_archive(
    rollout, monkeypatch
):
    updater, tx, operations = rollout
    archive = Path(tx["backup"]) / "canary.tar.gz"
    archive.write_bytes(b"backup")
    tx["instances"]["canary"].update(phase="ready", checksum=update.checksum(archive))
    tx["instances"]["canary"]["old"] = str(updater.root / "older-release")
    execute = update.run

    def fail_restore(*args, **kwargs):
        if args[0] == "restore":
            raise OSError("interrupt restore")
        return execute(*args, **kwargs)

    monkeypatch.setattr(update, "run", fail_restore)
    with pytest.raises(OSError, match="interrupt restore"):
        updater.rollback_instance(tx, "canary")
    pending = json.loads(update.JOURNAL.read_text())
    assert pending["instances"]["canary"]["phase"] == "rolling_back"
    monkeypatch.setattr(update, "run", execute)
    updater.rollback_instance(pending, "canary")
    assert operations.count(("recovery-snapshot", "canary")) == 1
    assert operations.count(("restore", "canary")) == 1
    assert operations[-1] == ("old_health", "canary")


def test_reboot_after_switch_keeps_writes_received_by_old_runtime(rollout):
    updater, tx, operations = rollout
    backup = Path(tx["backup"])
    archive = backup / "canary.tar.gz"
    archive.write_bytes(b"backup")
    current = backup / "canary-before-rollback.tar.gz"
    current.write_bytes(b"initial candidate data")
    tx["instances"]["canary"].update(
        phase="rolling_back",
        checksum=update.checksum(archive),
        keep_data=False,
        recovery_checksum=update.checksum(current),
    )
    updater.rollback_instance(tx, "canary")
    assert ("restore", "canary") not in operations
    assert current.read_bytes() == b"initial candidate data"
    assert (backup / "canary-after-restart.tar.gz").read_bytes() == b"current user data"
    assert operations[-1] == ("old_health", "canary")


def test_recovery_disk_full_preserves_live_canary_without_destructive_restore(
    rollout, monkeypatch
):
    updater, tx, operations = rollout
    archive = Path(tx["backup"]) / "canary.tar.gz"
    archive.write_bytes(b"backup")
    tx["instances"]["canary"].update(phase="ready", checksum=update.checksum(archive))
    monkeypatch.setattr(updater, "backup_data", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        updater.rollback_instance(tx, "canary")
    assert operations == [("stop", "canary"), ("start", "canary")]


def test_audio_schema_tracks_durable_layout_and_ignores_song_changes(tmp_path):
    audio = tmp_path / "cogs/Audio"
    audio.mkdir(parents=True)
    assert update.audio_schema(tmp_path) == ""
    db = sqlite3.connect(audio / "Audio.db")
    try:
        db.execute("CREATE TABLE queue (title TEXT)")
        old = update.audio_schema(tmp_path)
        assert old
        db.execute("INSERT INTO queue VALUES ('new song')")
        db.commit()
        assert update.audio_schema(tmp_path) == old
        db.execute("ALTER TABLE queue ADD COLUMN position INTEGER")
        changed = update.audio_schema(tmp_path)
        assert changed and changed != old
        db.execute("PRAGMA user_version=2")
        assert update.audio_schema(tmp_path) != changed
    finally:
        db.close()


def test_format_two_held_queue_archived_before_old_runtime_reads_format_one(rollout, monkeypatch):
    updater, tx, _ = rollout
    root = updater.data["canary"]
    audio = root / "cogs/Audio/Audio.db"
    audio.parent.mkdir(parents=True)
    (root / "core").mkdir()
    (root / "core/settings.json").write_text("{}")
    with sqlite3.connect(audio) as db:
        db.execute("CREATE TABLE persist_queue (title TEXT)")
        db.execute("INSERT INTO persist_queue VALUES ('before')")
    db.close()
    backup = Path(tx["backup"])
    archive = backup / "canary.tar.gz"
    with archive.open("wb") as dst:
        update.snapshot(root, dst)
    with sqlite3.connect(audio) as db:
        db.execute("ALTER TABLE persist_queue ADD COLUMN held INTEGER NOT NULL DEFAULT 0")
        db.execute("INSERT INTO persist_queue VALUES ('added after cutover', 1)")
    db.close()
    row = tx["instances"]["canary"]
    row.update(phase="ready", compatible_data=False, checksum=update.checksum(archive))
    execute = update.run

    def run(*args, **kwargs):
        if args[0] == "recovery-snapshot":
            update.snapshot(root, kwargs["stdout"], recovery=True)
        elif args[0] == "restore":
            update.restore(root, kwargs["stdin"])
        else:
            if args[0] == "switch":
                with sqlite3.connect(audio) as db:
                    assert db.execute("SELECT * FROM persist_queue").fetchall() == [("before",)]
                    assert [c[1] for c in db.execute("PRAGMA table_info(persist_queue)")] == [
                        "title"
                    ]
                db.close()
            return execute(*args, **kwargs)

    monkeypatch.setattr(update, "run", run)
    updater.rollback_instance(tx, "canary")
    evidence = updater.root / "evidence"
    evidence.mkdir()
    with (backup / "canary-before-rollback.tar.gz").open("rb") as src:
        update.restore(evidence, src)
    with sqlite3.connect(evidence / "cogs/Audio/Audio.db") as db:
        assert db.execute("SELECT title FROM persist_queue WHERE held=1").fetchall() == [
            ("added after cutover",)
        ]
    db.close()


def test_missing_test_channel_blocks_music_cutover(rollout, monkeypatch):
    updater, tx, operations = rollout
    monkeypatch.setattr(updater, "idle", update.Rollout.idle.__get__(updater))
    monkeypatch.setattr(updater, "probe", lambda name, *flags: ["probe", name, *flags])
    monkeypatch.setattr(updater, "lava", lambda action: ["lava", action])
    monkeypatch.setattr(Path, "is_socket", lambda self: True)

    def run(args):
        return update.subprocess.CompletedProcess(args, 78 if args[0] == "probe" else 0)

    monkeypatch.setattr(update.subprocess, "run", run)
    tx["require_playback"] = True
    with pytest.raises(update.Busy, match="test voice channel"):
        updater.apply(tx)
    assert json.loads(update.JOURNAL.read_text())["deferred"]
    assert not operations
    tx["require_playback"] = False
    updater.apply(tx)
    assert ("stop", "primary") in operations


@pytest.mark.parametrize(
    "strict, result, expected", [(True, 0, None), (True, 78, update.Busy), (False, 78, None)]
)
def test_music_candidate_requires_completed_voice_check(
    rollout, monkeypatch, strict, result, expected
):
    updater, tx, _ = rollout
    updater.timeout = 30
    monkeypatch.setattr(updater, "connected", lambda *args: True)
    monkeypatch.setattr(updater, "revision", lambda path: tx["sha"])
    monkeypatch.setattr(updater, "lava", lambda action: ["lava", action])
    monkeypatch.setattr(update, "run", Mock())
    monkeypatch.setattr(Path, "is_socket", lambda self: True)
    probe = Mock(return_value=["probe"])
    monkeypatch.setattr(updater, "probe", probe)
    monkeypatch.setattr(
        update.subprocess, "run", lambda args: update.subprocess.CompletedProcess(args, result)
    )
    health = lambda: update.Rollout.health(updater, "canary", tx["sha"], require_playback=strict)
    if expected:
        with pytest.raises(expected):
            health()
    else:
        health()
    probe.assert_called_once_with("canary", *(["--require-playback"] if strict else []))


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
    assert operations.count(("new_health", "canary")) == 1
    assert operations[-1] == ("new_health", "primary")


def test_restarted_deferred_canary_repeats_voice_validation(rollout, monkeypatch):
    updater, tx, operations = rollout
    monkeypatch.setattr(updater, "idle", Mock(side_effect=[None, update.Busy()]))
    with pytest.raises(update.Busy):
        updater.apply(tx)
    monkeypatch.setattr(updater, "idle", lambda **kwargs: None)
    monkeypatch.setattr(updater, "invocation", lambda name: name + "-restarted")
    updater.update()
    assert operations.count(("new_health", "canary")) == 2
    assert operations.count(("stop", "canary")) == 1


def test_missing_probe_classified_using_service_configuration(rollout, monkeypatch, capsys):
    updater, tx, _ = rollout
    updater.timeout = 4
    monkeypatch.setattr(Path, "is_socket", lambda self: False)
    monkeypatch.setattr(updater, "lava", lambda action: ["lava", action])
    lava_check = Mock()
    monkeypatch.setattr(update, "run", lava_check)
    monkeypatch.setattr(updater, "socket_disabled", lambda name: True)
    update.Rollout.health(updater, "canary", tx["sha"])
    lava_check.assert_called_once_with("lava", "check")
    assert "probe disabled" in capsys.readouterr().out
    with pytest.raises(update.Busy, match="disabled"):
        update.Rollout.health(updater, "canary", tx["sha"], require_playback=True)
    monkeypatch.setattr(updater, "socket_disabled", lambda name: False)
    monkeypatch.setattr(update.time, "monotonic", Mock(side_effect=[0, 1, 3, 5]))
    monkeypatch.setattr(update.time, "sleep", lambda t: None)
    with pytest.raises(RuntimeError, match="Timed out"):
        update.Rollout.health(updater, "canary", tx["sha"])


def test_nonmusic_preflight_requires_connected_audio_and_expected_probe(rollout, monkeypatch):
    updater, tx, _ = rollout
    monkeypatch.setattr(updater, "lava", lambda action: ["lava", action])
    monkeypatch.setattr(
        update.subprocess, "run", lambda args: update.subprocess.CompletedProcess(args, 0)
    )
    monkeypatch.setattr(Path, "is_socket", lambda self: False)
    monkeypatch.setattr(updater, "socket_disabled", lambda name: False)
    with pytest.raises(update.Busy, match="enabled"):
        update.Rollout.idle(updater)
    monkeypatch.setattr(updater, "socket_disabled", lambda name: True)
    update.Rollout.idle(updater)
    monkeypatch.setattr(updater, "connected", lambda name: False)
    with pytest.raises(RuntimeError, match="not connected"):
        update.Rollout.idle(updater)


def test_split_runtime_accounts_route_data_helpers_without_changing_build_account(
    tmp_path, monkeypatch
):
    # The production wrapper historically supplied its own snapshot and restore.
    # New data operations fall through to the base implementation.
    class SeparateAccounts(update.Rollout):
        def helper(self, operation, *args):
            if operation in ("snapshot", "restore"):
                return ["legacy-helper"]
            return super().helper(operation, *args)

    updater = object.__new__(SeparateAccounts)
    updater.user = "build"
    updater.data = {"canary": tmp_path / "canary", "primary": tmp_path / "primary"}
    uid = {"build": 1000, "bot-canary": 1001, "root": 0}
    monkeypatch.setitem(
        sys.modules,
        "pwd",
        SimpleNamespace(getpwnam=lambda user: SimpleNamespace(pw_uid=uid[user])),
    )
    monkeypatch.setenv("RED_USER_CANARY", "bot-canary")
    monkeypatch.delenv("RED_USER_PRIMARY", raising=False)
    monkeypatch.setattr(
        update,
        "output",
        lambda *args: "bot-canary" if args[-1] == "red@canary.service" else "build",
    )
    for operation in ("audio-schema", "recovery-snapshot"):
        assert updater.helper(operation, updater.data["canary"])[2] == "bot-canary"
        assert updater.helper(operation, updater.data["primary"])[2] == "build"
        with pytest.raises(ValueError, match="data path"):
            updater.helper(operation, tmp_path / "outside")
    assert updater.helper("snapshot", updater.data["canary"]) == ["legacy-helper"]
    assert update.Rollout.helper(updater, "restore", updater.data["canary"])[2] == "bot-canary"
    assert updater.helper("switch", tmp_path / "link", tmp_path / "release")[2] == "build"
    monkeypatch.setenv("RED_USER_CANARY", "build")
    with pytest.raises(ValueError, match="service account"):
        updater.helper("audio-schema", updater.data["canary"])
    monkeypatch.setenv("RED_USER_CANARY", "root")
    monkeypatch.setattr(update, "output", lambda *args: "root")
    with pytest.raises(ValueError, match="service account"):
        updater.helper("recovery-snapshot", updater.data["canary"])


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
    last_incident = update.STATE / ("b" * 12 + "-failure")
    last_incident.mkdir()
    ledger = update.STATE / "managed.json"
    update.save(
        {
            "releases": [previous, retired],
            "backups": list(map(str, paths)),
            "failed_backup": str(last_incident),
        },
        ledger,
    )
    monkeypatch.setattr(updater, "link", lambda name: Path(tx["candidate"]))
    updater.complete(tx, committed=True)
    updater.discard.assert_called_once_with(Path(retired))
    result = json.loads(ledger.read_text())
    assert set(result["releases"]) == {previous, tx["candidate"]}
    assert result["backups"] == [str(paths[1]), tx["backup"]]
    assert result["failed_backup"] == str(last_incident)
    assert last_incident.is_dir()
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


def test_failed_canary_delays_only_automatic_retry_of_same_sha(rollout, monkeypatch):
    updater, tx, operations = rollout
    health = updater.health
    clock = Mock(return_value=1000)
    monkeypatch.setattr(update.time, "time", clock)

    def reject_canary(name, sha=None, **kwargs):
        if sha:
            assert name == "canary"
            raise RuntimeError("candidate playback failed")
        health(name)

    monkeypatch.setattr(updater, "health", reject_canary)
    with pytest.raises(RuntimeError, match="candidate playback failed"):
        updater.apply(tx)
    assert ("stop", "primary") not in operations
    assert operations[-1] == ("old_health", "canary")
    assert not update.JOURNAL.exists()

    remote = Mock(return_value=f"{tx['sha']} {update.REF}")
    monkeypatch.setattr(update, "output", remote)
    monkeypatch.setattr(updater, "bot", lambda *args: args)
    monkeypatch.setattr(updater, "check_layout", Mock())
    monkeypatch.setattr(updater, "revision", Mock(return_value="c" * 40))
    monkeypatch.setattr(updater, "connected", Mock(return_value=True))
    prepare = Mock(side_effect=RuntimeError("staging reached"))
    monkeypatch.setattr(updater, "prepare", prepare)
    updater.update(automatic=True)
    prepare.assert_not_called()

    with pytest.raises(RuntimeError, match="staging reached"):
        updater.update()
    prepare.assert_called_once_with(tx["sha"])
    remote.return_value = f"{'b' * 40} {update.REF}"
    with pytest.raises(RuntimeError, match="staging reached"):
        updater.update(automatic=True)
    prepare.assert_called_with("b" * 40)
    remote.return_value = f"{tx['sha']} {update.REF}"
    clock.return_value += update.AUTOMATIC_RETRY_SECONDS
    with pytest.raises(RuntimeError, match="staging reached"):
        updater.update(automatic=True)
    prepare.assert_called_with(tx["sha"])


def test_busy_probe_and_interrupted_recovery_do_not_mark_candidate_failed(rollout, monkeypatch):
    updater, tx, operations = rollout
    health = updater.health
    monkeypatch.setattr(updater, "health", Mock(side_effect=update.Busy))
    with pytest.raises(update.Busy):
        updater.apply(tx)
    pending = json.loads(update.JOURNAL.read_text())
    assert "candidate_failed" not in pending
    assert ("stop", "primary") not in operations
    # Simulate interruption while resuming a deferred cutover. Always recover
    # before contacting the remote or considering another scheduled attempt.
    pending.pop("deferred")
    update.save(pending)
    monkeypatch.setattr(updater, "health", health)
    monkeypatch.setattr(update, "output", Mock(side_effect=AssertionError("contacted remote")))
    updater.update(automatic=True)
    assert "automatic_retry" not in json.loads((update.STATE / "managed.json").read_text())
    assert not update.JOURNAL.exists()


def test_automatic_check_requires_remote_sha_before_attempting_cutover(rollout, monkeypatch):
    updater, tx, operations = rollout
    update.finish()
    monkeypatch.setattr(updater, "bot", lambda *args: args)
    remote = Mock(side_effect=[OSError("offline"), f"{tx['sha'][:12]} {update.REF}"])
    monkeypatch.setattr(update, "output", remote)
    with pytest.raises(OSError, match="offline"):
        updater.update(automatic=True)
    with pytest.raises(ValueError, match="Could not resolve"):
        updater.update(automatic=True)
    assert not (update.STATE / "managed.json").exists()
    assert not update.JOURNAL.exists()
    assert not operations
