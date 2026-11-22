import hashlib
import io
import json
import os
import shutil
import socket
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


@pytest.mark.parametrize(
    ("flags", "response", "command", "code"),
    [
        ([], b"ok", b"check", 0),
        ([], b"unconfigured", b"check", 78),
        (["--available"], b"unconfigured", b"available", 78),
        (["--available", "--require-playback"], b"ready", b"available", 0),
        (["--available", "--require-playback"], b"unavailable", b"available", 78),
        (["--require-playback"], b"busy", b"verify", 75),
        (["--require-playback"], b"finished", b"verify", 0),
        (["--require-playback"], b"ok", b"verify", 1),
        (["--require-playback"], b"invalid", b"verify", 1),
        (["--idle"], b"ready", b"idle", 0),
        (["--idle"], b"busy", b"idle", 75),
        (["--idle"], b"unconfigured", b"idle", 78),
        (["--idle"], b"occupied", b"idle", 1),
        (["--rollout-check"], b"ok", b"rollout-check", 0),
        (["--rollout-check"], b"busy", b"rollout-check", 75),
        (["--rollout-check"], b"occupied", b"rollout-check", 76),
        (["--rollout-check", "--require-playback"], b"occupied", b"rollout-verify", 76),
        (["--rollout-check", "--require-playback"], b"finished", b"rollout-verify", 0),
        (["--rollout-check", "--require-playback"], b"ok", b"rollout-verify", 1),
        (["--require-playback"], b"occupied", b"verify", 1),
    ],
)
def test_probe_protocol(monkeypatch, flags, response, command, code):
    monkeypatch.setattr(probe.sys, "argv", ["probe.py", *flags, "test.sock"])
    connection = MagicMock()
    connection.makefile.return_value = io.BytesIO(response + b"\n")
    sockets = MagicMock()
    sockets.socket.return_value.__enter__.return_value = connection
    monkeypatch.setattr(probe, "socket", sockets)
    assert probe.main() == code
    connection.sendall.assert_called_once_with(command + b"\n")


@pytest.mark.parametrize(
    ("flags", "commands", "response", "code"),
    [
        (["--idle"], [b"idle", b"available"], b"busy\n", 75),
        (["--rollout-check"], [b"rollout-check", b"check"], b"ok\n", 0),
        (["--rollout-check", "--require-playback"], [b"rollout-verify", b"verify"], b"ok\n", 1),
    ],
)
def test_rollout_probe_with_older_bot(monkeypatch, flags, commands, response, code):
    monkeypatch.setattr(probe.sys, "argv", ["probe.py", *flags, "test.sock"])
    replies = iter([b"invalid\n", response])
    requests = []

    def ask(path, command):
        assert path == "test.sock"
        requests.append(command)
        return next(replies)

    monkeypatch.setattr(probe, "ask", ask)
    assert probe.main() == code
    assert requests == commands


@pytest.mark.skipif(
    os.name != "posix", reason="the maintenance service uses Unix socket ownership"
)
def test_refresh_runs_each_probe_as_its_red_unit_user(tmp_path, monkeypatch):
    script = """
        source "$1"
        helper=/usr/local/libexec/red-lavalink/release.py
        bots=(red@canary.service red@main.service)
        RED_PLAYBACK_SOCKETS="$2 $3"
        systemctl() { if [[ "$5" == red@canary.service ]]; then echo canary; else echo main; fi; }
        id() { if [[ "$2" == canary ]]; then echo 1001; else echo 1002; fi; }
        stat() { if [[ "${*: -1}" == "${second_parent}" ]]; then echo 1002; else echo 1001; fi; }
        runuser() { printf '%s %s\n' "$2" "${*: -1}"; }
        socket_for_unit() { if [[ "$1" == red@canary.service ]]; then echo "$first_socket"; else echo "$second_socket"; fi; }
        verified_voice --require-playback
    """
    first, second = [tmp_path / name / "check.sock" for name in ("canary", "main")]
    first.parent.mkdir()
    second.parent.mkdir()
    monkeypatch.chdir(tmp_path)
    with socket.socket(socket.AF_UNIX) as one, socket.socket(socket.AF_UNIX) as two:
        one.bind(str(first.relative_to(tmp_path)))
        two.bind(str(second.relative_to(tmp_path)))
        result = subprocess.run(
            ["bash", "-c", script, "test", str(REFRESH), str(first), str(second)],
            env={
                **os.environ,
                "second_parent": str(second.parent),
                "first_socket": str(first),
                "second_socket": str(second),
            },
            check=True,
            capture_output=True,
            text=True,
        )
    assert result.stdout.splitlines() == [f"canary {first}", f"main {second}"]


# Run the systemd orchestration with controlled service, API and voice observations.
SERVICES = """
unit=lavalink.service
bots=(red@canary.service red@main.service)
RED_PLAYBACK_SOCKETS="/run/redbot-probe-canary/check.sock /run/redbot-probe-main/check.sock"
voice_socket_ready() {
    case "$1" in
        red@canary.service) [[ "$2" == /run/redbot-probe-canary/check.sock ]] ;;
        red@main.service) [[ "$2" == /run/redbot-probe-main/check.sock ]] ;;
        *) return 1 ;;
    esac
}
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
        # An inaccessible channel defers verification until it is configured.
        """
        candidate_voice=78
        refresh
        [[ "$marker" == candidate && "$restarts" == 1 ]]
        candidate_voice=0
        refresh
        [[ "$marker" == none && "$restarts" == 1 ]]
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
        # An old sourced wrapper's empty foreach returned success. Neither a fresh
        # nor an already staged release may advance when either bot is unverified.
        """
        check_voice() { return 0; }
        for RED_PLAYBACK_SOCKETS in "" "/run/redbot-probe-canary/check.sock" \
            "/run/redbot-probe-main/check.sock /run/redbot-probe-canary/check.sock" \
            "/run/redbot-probe-canary/check.sock /run/redbot-probe-canary/check.sock"; do
            marker=none
            live=old
            installed=old
            refresh
            [[ "$marker" == none && "$installed" == old && "$restarts" == 0 ]]
            marker=candidate
            live=new
            installed=new
            refresh
            [[ "$marker" == candidate && "$restarts" == 0 ]]
        done
        """,
    ],
    ids=[
        "deferred-candidate",
        "deferred-rollback",
        "missing-channel",
        "interrupted-candidate",
        "restored-rollback",
        "legacy-voice-wrapper-cannot-skip-a-bot",
    ],
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
