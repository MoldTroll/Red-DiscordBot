import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock
from zipfile import ZipFile

import pytest

from tools.lavalink import probe, release


OLD = "3.7.13+red.4"
NEW = "3.7.13+red.5"
REFRESH = Path(__file__).resolve().parents[3] / "tools" / "lavalink" / "refresh.sh"


def jar(tag):
    data = io.BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", f"Implementation-Version: {tag}\n")
    return data.getvalue()


@pytest.mark.parametrize("interrupted", [False, True])
def test_update_and_interrupted_swap_can_recover(tmp_path, monkeypatch, interrupted):
    monkeypatch.chdir(tmp_path)
    old, new = jar(OLD), jar(NEW)
    release.JAR.write_bytes(old)
    asset = {"name": "Lavalink.jar", "digest": "sha256:" + hashlib.sha256(new).hexdigest()}
    latest = {"tag_name": NEW, "draft": False, "prerelease": False, "assets": [asset]}
    responses = iter([json.dumps(latest).encode(), new])
    monkeypatch.setattr(release, "urlopen", lambda *args, **kwargs: io.BytesIO(next(responses)))
    monkeypatch.setattr(release, "local_api", lambda *args: {"playingPlayers": 0})
    if interrupted:
        replace = os.replace

        def interrupt_swap(src, dst):
            if dst == release.JAR:
                raise InterruptedError
            replace(src, dst)

        with monkeypatch.context() as context:
            context.setattr(os, "replace", interrupt_swap)
            with pytest.raises(InterruptedError):
                release.update({})
        assert release.phase() == "rollback"
    else:
        assert release.update({}) == 0
        assert release.phase() == "candidate"
        assert release.JAR.read_bytes() == new
    assert release.BACKUP.read_bytes() == old
    release.rollback()
    assert release.phase() == "rollback"
    assert release.JAR.read_bytes() == old
    # A later timer can retry verification without replacing the restored file.
    with monkeypatch.context() as context:
        context.setattr(shutil, "copy2", lambda *_: pytest.fail("copied a restored JAR"))
        release.rollback()
    assert release.phase() == "rollback"


def test_legacy_pending_resumes_rollback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    release.STATE.mkdir()
    release.PENDING.touch()
    release.BACKUP.write_bytes(jar(OLD))
    release.JAR.write_bytes(jar(NEW))
    assert release.phase() == "rollback"
    release.rollback()
    assert release.jar_version(release.JAR) == OLD


@pytest.mark.parametrize("live,expected", [(OLD, 1), (NEW, 0)])
def test_running_checks_full_release_tag(tmp_path, monkeypatch, live, expected):
    (tmp_path / "Lavalink.jar").write_bytes(jar(NEW))
    (tmp_path / "application.yml").write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(release.sys, "argv", ["release.py", "running", str(tmp_path)])
    monkeypatch.setattr(release, "local_api", lambda path, config: {"version": {"semver": live}})
    assert release.main() == expected


@pytest.mark.parametrize("flags", [[], ["--available"]])
def test_optional_probe_accepts_unconfigured_channel(monkeypatch, flags):
    monkeypatch.setattr(probe.sys, "argv", ["probe.py", *flags, "test.sock"])
    connection = MagicMock()
    connection.makefile.return_value = io.BytesIO(b"unconfigured\n")
    sockets = MagicMock()
    sockets.socket.return_value.__enter__.return_value = connection
    monkeypatch.setattr(probe, "socket", sockets)
    assert probe.main() == 0
    connection.sendall.assert_called_once_with(b"available\n" if flags else b"check\n")


# Run the systemd orchestration with controlled service, API and voice observations.
SERVICES = """
unit=lavalink.service
marker=none
live=old
installed=old
restarts=0
idle=0
candidate_health=0
candidate_voice=0
old_voice=0
as_owner() {
    case "$1" in
        phase) echo "$marker" ;;
        running) [[ "$live" == "$installed" ]] ;;
        update) installed=new; marker=candidate ;;
        rollback) installed=old; marker=rollback ;;
        commit) marker=none ;;
        idle) return "$idle" ;;
        check) [[ "$live" == old ]] || return "$candidate_health" ;;
        *) return 99 ;;
    esac
}
connected() { return 0; }
systemctl() {
    [[ "$1" == restart && "$2" == "$unit" ]] || return 99
    live=$installed
    ((restarts+=1))
}
check_voice() {
    [[ "${1:-}" == --available ]] && return 0
    if [[ "$live" == new ]]; then return "$candidate_voice"; fi
    return "$old_voice"
}
"""


@pytest.mark.parametrize(
    "scenario",
    [
        # A user arrives between the preflight and the post-restart voice probe.
        """
        candidate_voice=75
        refresh
        [[ "$marker" == candidate && "$restarts" == 1 ]]
        refresh
        [[ "$marker" == candidate && "$restarts" == 1 ]]
        candidate_voice=0
        refresh
        [[ "$marker" == none && "$live" == new && "$restarts" == 1 ]]
        """,
        # The previous release is already running while its health check is pending.
        """
        candidate_health=1
        old_voice=75
        refresh
        [[ "$marker" == rollback && "$live" == old && "$restarts" == 2 ]]
        old_voice=1
        if refresh; then exit 98; fi
        [[ "$marker" == rollback && "$restarts" == 2 ]]
        old_voice=0
        refresh
        [[ "$marker" == none && "$restarts" == 2 ]]
        """,
        # An interruption after the JAR swap leaves the old process serving music.
        """
        installed=new
        marker=candidate
        idle=75
        refresh
        [[ "$marker" == candidate && "$restarts" == 0 ]]
        idle=0
        refresh
        [[ "$marker" == none && "$restarts" == 1 && "$live" == new ]]
        """,
        # An interruption after the rollback restart only requires verification.
        """
        marker=rollback
        refresh
        [[ "$marker" == none && "$restarts" == 0 ]]
        """,
    ],
    ids=["deferred-candidate", "deferred-rollback", "interrupted-candidate", "restored-rollback"],
)
def test_refresh_resumes_without_unnecessary_restart(scenario):
    bash = shutil.which("bash")
    if os.name == "nt" and (git := shutil.which("git")):
        git_bash = Path(git).resolve().parents[1] / "bin" / "bash.exe"
        if git_bash.is_file():
            bash = str(git_bash)
    if not bash:
        pytest.skip("systemd maintenance script tests require Bash")
    subprocess.run(
        [bash, "-c", 'source "$1"\n' + SERVICES + scenario, "test", REFRESH.as_posix()],
        check=True,
        timeout=10,
    )
