from io import BytesIO
from importlib import metadata
import json
from pathlib import Path, PurePosixPath
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

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
    updater.dm_sender = None
    updater.dm_fallbacks = []
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
        elif "completion-dm" in args:
            return
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
    monkeypatch.setattr(
        updater, "running_instances", lambda: [name for name in updater.names if running[name]]
    )
    monkeypatch.setattr(updater, "helper", lambda *args: args)
    monkeypatch.setattr(updater, "idle", lambda **kwargs: None)
    monkeypatch.setattr(updater, "health", health)
    monkeypatch.setattr(updater, "invocation", lambda name: name + "-running")
    monkeypatch.setattr(updater, "instance_user", lambda name: "red-" + name)
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


@pytest.mark.parametrize(
    "load,active,sub,result,expected",
    [
        ("loaded", "active", "running", "success", ["canary"]),
        ("loaded", "inactive", "dead", "success", []),
        ("loaded", "inactive", "dead", "exit-code", None),
        ("loaded", "failed", "failed", "exit-code", None),
        ("loaded", "activating", "auto-restart", "exit-code", None),
        ("not-found", "inactive", "dead", "success", None),
    ],
)
def test_select_running_instances(rollout, monkeypatch, load, active, sub, result, expected):
    updater, _, _ = rollout
    updater.names = ["canary"]
    monkeypatch.setattr(
        update,
        "output",
        lambda *args: f"LoadState={load}\nActiveState={active}\nSubState={sub}\nResult={result}",
    )
    if expected is None:
        with pytest.raises(RuntimeError, match="red@canary.service"):
            update.Rollout.running_instances(updater)
    else:
        assert update.Rollout.running_instances(updater) == expected


@pytest.mark.parametrize("running", [[], ["primary"]])
def test_stopped_main_does_not_start_an_update(rollout, monkeypatch, running):
    updater, _, operations = rollout
    update.finish()
    monkeypatch.setattr(updater, "running_instances", lambda: running)
    monkeypatch.setattr(updater, "bot", lambda *args: pytest.fail("unexpected remote lookup"))
    updater.update(automatic=True)
    assert not operations
    assert not update.JOURNAL.exists()


@pytest.mark.parametrize(
    "order,running,stale",
    [
        (["canary"], ["canary"], ["canary"]),
        (["canary", "primary"], ["canary"], ["canary"]),
        (["canary", "primary"], ["canary", "primary"], ["primary"]),
        (["primary", "canary"], ["primary", "canary"], ["primary", "canary"]),
    ],
)
def test_only_stale_running_instances_are_updated(rollout, monkeypatch, order, running, stale):
    updater, tx, operations = rollout
    update.finish()
    updater.names = order
    updater.data = {name: updater.data[name] for name in order}
    sha = tx["sha"]
    dest = Path(tx["candidate"])
    layout = Mock()
    monkeypatch.setattr(updater, "check_layout", layout)
    monkeypatch.setattr(updater, "running_instances", lambda: running)
    monkeypatch.setattr(updater, "bot", lambda *args: args)

    def revision(runtime):
        if runtime.name in stale and ("switch", runtime.name) not in operations:
            return "b" * 40
        return sha

    monkeypatch.setattr(updater, "revision", revision)
    monkeypatch.setattr(
        update,
        "output",
        lambda *args: (
            json.dumps({"music": "same", "format": None})
            if args[-1] == "runtime-info"
            else f"{sha} {update.REF}"
        ),
    )
    snapshots = []
    apply = updater.apply

    def prepare(revision):
        update.save(str(dest), update.STATE / "staging.json")
        return dest

    def apply_transaction(transaction):
        snapshots.append(json.loads(update.JOURNAL.read_text()))
        apply(transaction)

    monkeypatch.setattr(updater, "prepare", prepare)
    monkeypatch.setattr(updater, "apply", apply_transaction)
    updater.update(automatic=True)
    assert [args.args[0] for args in layout.call_args_list] == running
    assert snapshots[0]["running_instances"] == running
    assert list(snapshots[0]["instances"]) == stale
    assert list(snapshots[0]["data"]) == order
    assert [name for action, name in operations if action == "switch"] == stale
    for name in stale:
        assert ("new_health", name) in operations
        if name != order[0]:
            assert operations.index(("new_health", order[0])) < operations.index(("stop", name))


@pytest.mark.parametrize("reject", ["wrong revision", "health failure", "restarted"])
def test_secondary_waits_for_successful_main_verification(rollout, monkeypatch, reject):
    updater, tx, operations = rollout
    del tx["instances"]["canary"]
    tx["running_instances"] = ["canary", "primary"]
    if reject == "wrong revision":
        monkeypatch.setattr(updater, "revision", lambda link: "b" * 40)
    elif reject == "health failure":
        monkeypatch.setattr(updater, "health", Mock(side_effect=RuntimeError("probe failed")))
    else:
        monkeypatch.setattr(updater, "invocation", Mock(side_effect=["previous", "restarted"]))
    with pytest.raises(RuntimeError):
        updater.apply(tx)
    assert not any(name == "primary" for _, name in operations)


def test_changing_main_during_recovery_requires_original_order(rollout):
    updater, _, operations = rollout
    updater.names.reverse()
    with pytest.raises(RuntimeError, match="original instance configuration"):
        updater.update()
    assert not operations and update.JOURNAL.exists()


def test_recover_one_instance_even_when_cutover_left_it_stopped(rollout, monkeypatch):
    updater, tx, operations = rollout
    tx["running_instances"] = ["primary"]
    del tx["instances"]["canary"]
    tx["instances"]["primary"]["phase"] = "stopping"
    update.save(tx)
    monkeypatch.setattr(updater, "running_instances", lambda: pytest.fail("cohort was recomputed"))
    updater.update()
    assert operations == [("start", "primary"), ("old_health", "primary")]
    assert not update.JOURNAL.exists()


def test_partial_cutover_keeps_offline_instance_runtime(rollout):
    updater, tx, _ = rollout
    del tx["instances"]["primary"]
    tx["running_instances"] = ["canary"]
    offline_runtime = updater.link("primary").resolve()
    update.save({"releases": [str(offline_runtime)], "backups": []}, update.STATE / "managed.json")
    updater.apply(tx)
    managed = json.loads((update.STATE / "managed.json").read_text())
    assert str(offline_runtime) in managed["releases"]
    assert all(call.args[0] != offline_runtime for call in updater.discard.call_args_list)


def test_current_instance_must_be_running_its_linked_revision(rollout, monkeypatch):
    updater, tx, operations = rollout
    update.finish()
    monkeypatch.setattr(updater, "bot", lambda *args: args)
    monkeypatch.setattr(update, "output", lambda *args: f"{tx['sha']} {update.REF}")
    monkeypatch.setattr(updater, "check_layout", lambda name: None)
    monkeypatch.setattr(
        updater, "revision", lambda link: tx["sha"] if link.name == "primary" else ""
    )
    monkeypatch.setattr(updater, "connected", lambda name, runtime: False)
    with pytest.raises(RuntimeError, match="primary.*different venv"):
        updater.update()
    assert not operations


@pytest.mark.parametrize(
    "main,expected",
    [
        (None, ["sub", "extra", "main"]),
        ("   ", ["sub", "extra", "main"]),
        ("main", ["main", "sub", "extra"]),
        ("extra", ["extra", "sub", "main"]),
        ("other", None),
        ("sub main", None),
    ],
)
def test_main_instance_defines_rollout_order(monkeypatch, tmp_path, main, expected):
    monkeypatch.setitem(
        sys.modules, "pwd", SimpleNamespace(getpwnam=lambda _: SimpleNamespace(pw_uid=1001))
    )
    for key in ("RED_UPDATE_DM_SENDER", "RED_UPDATE_DM_FALLBACK", "RED_MAIN_INSTANCE"):
        monkeypatch.delenv(key, raising=False)
    for key in ("RED_ROOT", "RED_PYTHON", "LAVALINK_DIR"):
        monkeypatch.setenv(key, str(tmp_path))
    monkeypatch.setenv("RED_USER", "red")
    monkeypatch.setenv("LAVALINK_USER", "lavalink")
    names = ["sub", "extra", "main"]
    monkeypatch.setenv("RED_INSTANCES", " ".join(names))
    for name in names:
        monkeypatch.setenv(f"RED_DATA_{name.upper()}", str(tmp_path / name))
    if main is not None:
        monkeypatch.setenv("RED_MAIN_INSTANCE", main)
    if expected is None:
        with pytest.raises(ValueError, match="RED_MAIN_INSTANCE"):
            update.Rollout()
    else:
        updater = update.Rollout()
        assert updater.names == expected
        assert list(updater.data) == expected


def test_dm_retried_after_commit_without_restarting_services(rollout, monkeypatch):
    updater, tx, operations = rollout
    execute = update.run
    attempts = []
    ledger = update.STATE / "managed.json"

    def run(*args, **kwargs):
        if "completion-dm" not in args:
            return execute(*args, **kwargs)
        sender = args[-1]
        assert args[:4] == ("/usr/sbin/runuser", "-u", "red-" + sender, "--")
        assert args[-4:] == ("completion-dm", tx["sha"], sender, sender)
        assert not update.JOURNAL.exists()
        assert json.loads(ledger.read_text())["pending_notification"]["sha"] == tx["sha"]
        assert operations[-1] == ("new_health", "primary")
        attempts.append(sender)
        if attempts == ["canary"]:
            raise update.subprocess.TimeoutExpired(args, 40)

    monkeypatch.setattr(update, "run", run)
    updater.apply(tx)
    assert attempts == ["canary", "primary"]
    assert json.loads(ledger.read_text())["pending_notification"]["deliveries"] == {
        "canary": ["canary"]
    }
    before = operations.copy()
    updater.dm_sender = "primary"
    monkeypatch.setattr(updater, "check_layout", lambda name: None)
    monkeypatch.setattr(updater, "bot", lambda *args: args)
    monkeypatch.setattr(update, "output", lambda *args: f"{tx['sha']} {update.REF}")
    updater.update(automatic=True)
    updater.update(automatic=True)
    assert attempts == ["canary", "primary", "canary"]
    assert "pending_notification" not in json.loads(ledger.read_text())
    assert operations == before


@pytest.mark.parametrize(
    ("participants", "sender", "fallbacks", "expected_sender"),
    [
        (["canary", "primary"], "canary", ["primary"], "canary"),
        (["primary"], None, [], "primary"),
        (["primary"], "canary", ["primary"], "primary"),
        (["canary", "primary"], "standby", ["primary", "canary"], "primary"),
    ],
)
def test_completion_dm_sender_selection(
    rollout, monkeypatch, participants, sender, fallbacks, expected_sender
):
    updater, tx, _ = rollout
    updater.dm_sender = sender
    updater.dm_fallbacks = fallbacks
    if sender and sender not in updater.names:
        updater.names.append(sender)
        (updater.root / "instances" / sender).mkdir()
    tx["running_instances"] = participants
    tx["instances"] = {name: tx["instances"][name] for name in participants}
    monkeypatch.setattr(update, "active", participants.__contains__)
    run = Mock()
    monkeypatch.setattr(update, "run", run)
    updater.complete(tx, committed=True)
    run.assert_called_once()
    args = run.call_args.args
    assert args[:4] == ("/usr/sbin/runuser", "-u", "red-" + expected_sender, "--")
    assert args[args.index("completion-dm") :] == (
        "completion-dm",
        tx["sha"],
        expected_sender,
        *participants,
    )
    assert "pending_notification" not in json.loads((update.STATE / "managed.json").read_text())


def test_only_running_bot_waits_for_its_configured_sender(rollout, monkeypatch):
    updater, tx, _ = rollout
    updater.dm_sender = "canary"
    tx["running_instances"] = ["primary"]
    tx["instances"].pop("canary")
    online = {"primary"}
    monkeypatch.setattr(update, "active", online.__contains__)
    send = Mock()
    monkeypatch.setattr(update, "run", send)
    updater.complete(tx, committed=True)
    send.assert_not_called()
    ledger = update.STATE / "managed.json"
    receipt = json.loads(ledger.read_text())["pending_notification"]
    assert receipt["deliveries"] == {"canary": ["canary", "primary"]}

    online.add("canary")
    send.side_effect = [update.subprocess.CalledProcessError(1, "completion-dm"), None]
    updater.deliver_completion_dm(json.loads(ledger.read_text()))
    assert json.loads(ledger.read_text())["pending_notification"] == receipt
    updater.deliver_completion_dm(json.loads(ledger.read_text()))
    assert send.call_count == 2
    for args, _ in send.call_args_list:
        assert args[2] == "red-canary"
        assert args[-5:] == ("completion-dm", tx["sha"], "canary", "canary", "primary")
    assert "pending_notification" not in json.loads(ledger.read_text())


def test_uncertain_main_delivery_is_not_retried_by_a_fallback(rollout, monkeypatch):
    updater, tx, _ = rollout
    updater.dm_sender = "primary"
    updater.dm_fallbacks = ["canary"]
    run = Mock(side_effect=update.subprocess.TimeoutExpired("completion-dm", 40))
    monkeypatch.setattr(update, "run", run)
    updater.complete(tx, committed=True)
    run.assert_called_once()
    assert run.call_args.args[2] == "red-primary"
    ledger = update.STATE / "managed.json"
    monkeypatch.setattr(update, "active", lambda name: name == "canary")
    updater.deliver_completion_dm(json.loads(ledger.read_text()))
    run.assert_called_once()
    assert list(json.loads(ledger.read_text())["pending_notification"]["deliveries"]) == [
        "primary"
    ]


def test_legacy_pending_dm_retries_only_original_last_sender(rollout, monkeypatch):
    updater, tx, _ = rollout
    updater.dm_sender = "canary"
    ledger = update.STATE / "managed.json"
    managed = {"pending_notification": {"sha": tx["sha"], "instances": updater.names.copy()}}
    update.save(managed, ledger)
    run = Mock()
    monkeypatch.setattr(update, "run", run)
    updater.deliver_completion_dm(managed)
    run.assert_called_once()
    args = run.call_args.args
    assert args[:4] == ("/usr/sbin/runuser", "-u", "red-primary", "--")
    assert args[args.index("completion-dm") :] == (
        "completion-dm",
        tx["sha"],
        "primary",
        "canary",
        "primary",
    )
    assert "pending_notification" not in json.loads(ledger.read_text())


def test_later_rollout_of_same_revision_preserves_pending_dm(rollout, monkeypatch):
    updater, tx, _ = rollout
    ledger = update.STATE / "managed.json"
    old_delivery = {"canary": ["canary", "primary"]}
    update.save(
        {
            "releases": [],
            "backups": [],
            "pending_notification": {"sha": tx["sha"], "deliveries": old_delivery},
        },
        ledger,
    )
    tx["running_instances"] = ["primary"]
    tx["instances"].pop("canary")
    monkeypatch.setattr(updater, "running_instances", lambda: ["primary"])
    delivered = []

    def send(*args, **kwargs):
        sender, *names = args[args.index("completion-dm") + 2 :]
        if sender == "canary":
            raise update.subprocess.CalledProcessError(1, args)
        delivered.append((sender, names))

    monkeypatch.setattr(update, "run", send)
    updater.complete(tx, committed=True)
    assert delivered == [("primary", ["primary"])]
    assert json.loads(ledger.read_text())["pending_notification"]["deliveries"] == old_delivery


def test_second_service_failure_restores_both_offline(rollout, monkeypatch):
    updater, tx, operations = rollout
    health = updater.health

    def reject_primary(name, sha=None, **kwargs):
        if sha and name == "primary":
            raise RuntimeError("failed primary probe")
        health(name, sha)

    monkeypatch.setattr(updater, "health", reject_primary)
    notification = Mock()
    monkeypatch.setattr(updater, "deliver_completion_dm", notification)
    with pytest.raises(RuntimeError, match="failed primary probe"):
        updater.apply(tx)
    assert [name for action, name in operations if action == "restore"] == ["primary", "canary"]
    assert operations[-1] == ("old_health", "canary")
    assert not update.JOURNAL.exists()
    notification.assert_not_called()
    assert "pending_notification" not in json.loads((update.STATE / "managed.json").read_text())


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


@pytest.mark.parametrize("primary_started_later", [False, True])
def test_cutover_waits_when_second_bot_is_connected_to_voice(
    rollout, monkeypatch, primary_started_later
):
    updater, tx, operations = rollout
    monkeypatch.setattr(updater, "idle", update.Rollout.idle.__get__(updater))
    monkeypatch.setattr(updater, "probe", lambda name, *flags: ["probe", name, *flags])
    monkeypatch.setattr(updater, "lava", lambda action: ["lava", action])
    monkeypatch.setattr(Path, "is_socket", lambda self: True)
    tx["require_playback"] = False
    if primary_started_later:
        tx["running_instances"] = ["canary"]
        del tx["instances"]["primary"]
    checked = []

    def run(args):
        checked.append(args)
        return update.subprocess.CompletedProcess(
            args, 75 if args == ["probe", "primary", "--idle"] else 0
        )

    monkeypatch.setattr(update.subprocess, "run", run)
    with pytest.raises(update.Busy):
        updater.apply(tx)
    assert checked == [
        ["lava", "idle"],
        ["probe", "canary", "--idle"],
        ["probe", "primary", "--idle"],
    ]
    assert not operations


def test_music_cutover_finishes_with_a_listener_in_the_test_channel(rollout, monkeypatch, capsys):
    updater, tx, operations = rollout
    updater.timeout = 30
    tx["require_playback"] = True
    monkeypatch.setattr(updater, "idle", update.Rollout.idle.__get__(updater))
    monkeypatch.setattr(updater, "health", update.Rollout.health.__get__(updater))
    monkeypatch.setattr(updater, "lava", lambda action: ["lava", action])
    monkeypatch.setattr(updater, "probe", lambda name, *flags: ["probe", name, *flags])
    monkeypatch.setattr(Path, "is_socket", lambda self: True)
    execute = update.run

    def run(*args, **kwargs):
        if args == ("lava", "check"):
            operations.append(args)
        else:
            return execute(*args, **kwargs)

    def probe(args):
        if args == ["lava", "idle"] or args[2:] == ["--idle"]:
            return update.subprocess.CompletedProcess(args, 0)
        assert args[2:] == ["--rollout-check", "--require-playback"]
        operations.append(("occupied", args[1]))
        return update.subprocess.CompletedProcess(args, 76)

    monkeypatch.setattr(update, "run", run)
    monkeypatch.setattr(update.subprocess, "run", probe)
    updater.apply(tx)

    output = capsys.readouterr().out
    assert [name for action, name in operations if action == "switch"] == ["canary", "primary"]
    assert operations.index(("occupied", "canary")) < operations.index(("stop", "primary"))
    assert operations.count(("lava", "check")) == 2
    for name in updater.names:
        assert f"{name}: skipped voice playback check (test channel occupied)." in output
        assert tx["instances"][name]["phase"] == "ready"
    assert tx["completed"] and not update.JOURNAL.exists()


@pytest.mark.parametrize(
    "strict, result, expected",
    [
        (True, 0, None),
        (True, 75, update.Busy),
        (True, 76, None),
        (False, 76, None),
        (True, 78, update.Busy),
        (False, 78, None),
        (True, 1, update.subprocess.CalledProcessError),
    ],
)
def test_candidate_voice_check_results(rollout, monkeypatch, capsys, strict, result, expected):
    updater, tx, _ = rollout
    updater.timeout = 30
    connected = Mock(return_value=True)
    monkeypatch.setattr(updater, "connected", connected)
    monkeypatch.setattr(updater, "revision", lambda path: tx["sha"])
    monkeypatch.setattr(updater, "lava", lambda action: ["lava", action])
    lava_check = Mock()
    monkeypatch.setattr(update, "run", lava_check)
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
    probe.assert_called_once_with(
        "canary", "--rollout-check", *(["--require-playback"] if strict else [])
    )
    if result == 76:
        assert "skipped voice playback check (test channel occupied)" in capsys.readouterr().out
        lava_check.assert_called_once_with("lava", "check")
        assert connected.call_count == 2


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


def test_bot_joining_voice_between_switches_defers_then_resumes(rollout, monkeypatch):
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
    assert update.instance_names("main") == ["main"]
    assert update.instance_names("test01 main") == ["test01", "main"]
    for names in ("", "main main", "main ../other", "main --other"):
        with pytest.raises(ValueError):
            update.instance_names(names)


@pytest.mark.parametrize(
    ("sender", "fallbacks", "invalid"),
    [
        (None, None, None),
        ("", "", None),
        ("canary", None, None),
        ("primary", "canary", None),
        ("outside", None, "RED_UPDATE_DM_SENDER"),
        (None, "primary", "RED_UPDATE_DM_FALLBACK"),
        ("canary", "primary primary", "RED_UPDATE_DM_FALLBACK"),
        ("canary", "canary", "RED_UPDATE_DM_FALLBACK"),
        ("canary", "outside", "RED_UPDATE_DM_FALLBACK"),
    ],
)
def test_update_dm_sender_configuration(tmp_path, monkeypatch, sender, fallbacks, invalid):
    monkeypatch.setitem(
        sys.modules, "pwd", SimpleNamespace(getpwnam=lambda _: SimpleNamespace(pw_uid=1001))
    )
    env = {
        "RED_USER": "red",
        "LAVALINK_USER": "lavalink",
        "RED_ROOT": str(tmp_path),
        "RED_PYTHON": str(tmp_path / "python"),
        "LAVALINK_DIR": str(tmp_path / "lavalink"),
        "RED_INSTANCES": "canary primary",
        "RED_MAIN_INSTANCE": "primary",
        "RED_DATA_CANARY": str(tmp_path / "canary"),
        "RED_DATA_PRIMARY": str(tmp_path / "primary"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key, value in (("RED_UPDATE_DM_SENDER", sender), ("RED_UPDATE_DM_FALLBACK", fallbacks)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    if invalid:
        with pytest.raises(ValueError, match=invalid):
            update.Rollout()
    else:
        updater = update.Rollout()
        assert updater.names == ["primary", "canary"]
        assert updater.dm_sender == (sender or None)
        assert updater.dm_fallbacks == (fallbacks or "").split()
        monkeypatch.setattr(update, "active", updater.names.__contains__)
        tx = {"instances": dict.fromkeys(updater.names), "running_instances": updater.names}
        if sender:
            assert updater.completion_deliveries(tx) == {sender: ["primary", "canary"]}
        else:
            assert updater.completion_deliveries(tx) == {
                "primary": ["primary"],
                "canary": ["canary"],
            }
            solo = {"instances": {"canary": {}}, "running_instances": ["canary"]}
            assert updater.completion_deliveries(solo) == {"canary": ["canary"]}


@pytest.mark.parametrize("response", [b"notified\n", b"retry\n", b"invalid\n", b""])
@pytest.mark.parametrize(("sender", "names"), [("sub", ["sub", "main"]), ("main", ["main"])])
def test_dm_client_requires_an_acknowledgement(monkeypatch, response, sender, names):
    client = MagicMock()
    client.__enter__.return_value = client
    client.makefile.return_value = BytesIO(response)
    monkeypatch.setattr(update, "socket", MagicMock(socket=Mock(return_value=client)))
    sha = "f" * 40
    if response == b"notified\n":
        update.request_completion_dm(sha, sender, names)
    else:
        with pytest.raises(RuntimeError, match="acknowledge"):
            update.request_completion_dm(sha, sender, names)
    client.connect.assert_called_once_with(f"/run/redbot-probe-{sender}/check.sock")
    client.sendall.assert_called_once_with(f"updated {sha} {' '.join(names)}\n".encode())


@pytest.mark.parametrize("sender", ["outside", "../main"])
def test_dm_client_rejects_unlisted_sender_before_connect(monkeypatch, sender):
    sockets = Mock()
    monkeypatch.setattr(update, "socket", sockets)
    with pytest.raises(ValueError):
        update.request_completion_dm("f" * 40, sender, ["sub", "main"])
    sockets.socket.assert_not_called()


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
