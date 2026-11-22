"""Stage and verify the shared Linux music bots, with recoverable per-instance cutover."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid


REPO = "https://github.com/MoldTroll/Red-DiscordBot.git"
REF = "refs/heads/V3/develop"
STATE = Path("/var/lib/redbot-fork-update")
JOURNAL = STATE / "transaction.json"
LOCK = Path("/run/lock/redbot-audio-maintenance.lock")
HELPERS = Path("/usr/local/libexec/red-lavalink")
PYTHON = "/usr/bin/python3"
AUTOMATIC_RETRY_SECONDS = 6 * 60 * 60
# This deployed predecessor writes format 1, but predates the packaged declaration.
FIRST_KNOWN_DATA_FORMAT = {"1ef34110f9362172a7aa8eb9ffd4ad4f374b529a": 1}


class Busy(Exception):
    """Another maintenance operation or music listener needs the running service."""


def runtime_info():
    """Run inside the inspected venv. Detect installed code, not mutable branch state."""
    from importlib import metadata

    try:
        from redbot import _fork
    except ImportError:
        _fork = None  # A runtime installed before the fork has no format declaration.

    red = metadata.distribution("Red-DiscordBot")
    origin = json.loads(red.read_text("direct_url.json") or "{}")
    revision = origin.get("vcs_info", {}).get("commit_id")
    data_format = getattr(_fork, "ROLLBACK_DATA_FORMAT", FIRST_KNOWN_DATA_FORMAT.get(revision))
    if type(data_format) is not int or data_format < 1:
        data_format = None
    files = sorted(
        p
        for p in (red.files or ())
        if str(p).startswith(("redbot/cogs/audio/", "redbot/core/", "redbot/__main__.py"))
        and p.suffix == ".py"
    )
    if not any(str(p).startswith("redbot/cogs/audio/") for p in files):
        return {"music": None, "format": data_format}
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path).encode() + b"\0" + bytes.fromhex(checksum(red.locate_file(path))))
    dependencies = sorted(
        (d.metadata["Name"].lower(), d.version)
        for d in metadata.distributions()
        if d.metadata["Name"].lower() != "red-discordbot"
    )
    digest.update(json.dumps(dependencies).encode())
    return {"music": digest.hexdigest(), "format": data_format}


def audio_schema(root):
    """Additional offline check for on-disk migrations, including SQLite WAL state."""
    import sqlite3

    path = root / "cogs/Audio/Audio.db"
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            schema = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return ""
    if not schema:
        return ""
    return hashlib.sha256(json.dumps([version, schema]).encode()).hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(transaction, path=None):
    if path is None:
        path = JOURNAL
    with tempfile.NamedTemporaryFile(dir=STATE, mode="w", delete=False) as stream:
        json.dump(transaction, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)
    sync_dir(STATE)


def finish():
    JOURNAL.unlink()
    sync_dir(STATE)


def selected(path):
    return path.suffix == ".json" or path.name in (
        "Audio.db",
        "Audio.db-wal",
        "Audio.db-shm",
        "Audio.db-journal",
    )


def data_files(root):
    def raise_error(error):
        raise error

    for directory, _, names in os.walk(root, onerror=raise_error):
        for name in names:
            path = Path(directory, name)
            if selected(path):
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError(f"Cannot snapshot non-regular data file {path}")
                yield path.relative_to(root)


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def snapshot(root, stream, *, recovery=False):
    paths = sorted(data_files(root))
    required = {Path("core/settings.json"), Path("cogs/Audio/Audio.db")}
    if not recovery and not required.issubset(paths):
        raise RuntimeError(
            "Expected the music instance's core/settings.json and cogs/Audio/Audio.db"
        )
    with tarfile.open(fileobj=stream, mode="w|gz", dereference=True) as archive:
        for path in paths:
            archive.add(root / path, arcname=str(path), recursive=False)


def restore(root, stream):
    # This operation runs as the bot user on an offline instance. An interrupted
    # restore can replay the same private archive without losing its original data.
    remaining = set(data_files(root))
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for item in archive:
            path = Path(item.name)
            if not item.isfile() or not selected(path) or path.is_absolute() or ".." in path.parts:
                raise ValueError("Unexpected entry in instance backup")
            target = root / path
            if target.parent.resolve() != target.parent:
                raise ValueError("Symlinked backup destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as dest:
                with archive.extractfile(item) as source:
                    shutil.copyfileobj(source, dest)
                dest.flush()
                os.fsync(dest.fileno())
            os.replace(dest.name, target)
            sync_dir(target.parent)
            remaining.discard(path)
    for path in remaining:
        (root / path).unlink()
        sync_dir((root / path).parent)


def switch(link, target):
    if not link.is_symlink() or not (target / "bin/python").is_file():
        raise RuntimeError(
            "Expected a provisioned instance link and a working virtual environment"
        )
    with tempfile.TemporaryDirectory(dir=link.parent) as directory:
        pending = Path(directory, "link")
        pending.symlink_to(target, target_is_directory=True)
        os.replace(pending, link)
    sync_dir(link.parent)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def as_user(user, *args):
    return ["/usr/sbin/runuser", "-u", user, "--", *args]


def output(*args):
    return run(*args, stdout=subprocess.PIPE, text=True).stdout.strip()


def active(name):
    return (
        subprocess.run(["systemctl", "is-active", "--quiet", f"red@{name}.service"]).returncode
        == 0
    )


def instance_names(value):
    names = value.split()
    if (
        not names
        or len(names) != len(set(names))
        or any(not re.fullmatch("[a-z][a-z0-9_]{0,31}", name) for name in names)
    ):
        raise ValueError("RED_INSTANCES must list distinct lowercase instance names")
    return names


def request_completion_dm(sha, sender, names):
    """선택한 봇 계정의 전용 소켓으로 배포 완료 알림을 요청한다."""
    if (
        not re.fullmatch("[0-9a-f]{40}", sha)
        or instance_names(" ".join(names)) != names
        or sender not in names
    ):
        raise ValueError("Invalid deployment receipt")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(f"/run/redbot-probe-{sender}/check.sock")
        client.sendall(f"updated {sha} {' '.join(names)}\n".encode("ascii"))
        with client.makefile("rb") as reader:
            if reader.readline(32) != b"notified\n":
                raise RuntimeError("The bot did not acknowledge the deployment receipt")


class Rollout:
    def __init__(self):
        import pwd

        self.user = os.environ["RED_USER"]
        self.lava_user = os.environ["LAVALINK_USER"]
        if any(pwd.getpwnam(user).pw_uid == 0 for user in (self.user, self.lava_user)):
            raise ValueError("Red and Lavalink must run as unprivileged users")
        self.root = Path(os.environ["RED_ROOT"])
        self.python = Path(os.environ["RED_PYTHON"])
        self.lava_dir = Path(os.environ["LAVALINK_DIR"])
        self.names = instance_names(os.environ["RED_INSTANCES"])
        main = os.environ.get("RED_MAIN_INSTANCE", "").strip() or self.names[0]
        if main not in self.names:
            raise ValueError("RED_MAIN_INSTANCE must name one instance in RED_INSTANCES")
        self.names = [main, *(name for name in self.names if name != main)]
        self.dm_sender = os.environ.get("RED_UPDATE_DM_SENDER", "").strip() or None
        if self.dm_sender is not None and self.dm_sender not in self.names:
            raise ValueError("RED_UPDATE_DM_SENDER must name an instance in RED_INSTANCES")
        self.dm_fallbacks = os.environ.get("RED_UPDATE_DM_FALLBACK", "").split()
        if self.dm_fallbacks and (
            not self.dm_sender
            or len(self.dm_fallbacks) != len(set(self.dm_fallbacks))
            or self.dm_sender in self.dm_fallbacks
            or not set(self.dm_fallbacks).issubset(self.names)
        ):
            raise ValueError(
                "RED_UPDATE_DM_FALLBACK requires a sender and distinct backup instances "
                "from RED_INSTANCES"
            )
        self.data = {name: Path(os.environ[f"RED_DATA_{name.upper()}"]) for name in self.names}
        if not all(
            p.is_absolute() for p in (self.root, self.python, self.lava_dir, *self.data.values())
        ):
            raise ValueError("Use absolute paths in /etc/default/redbot-fork-update")
        if any(
            path.is_relative_to(other_path)
            for name, path in self.data.items()
            for other, other_path in self.data.items()
            if name != other
        ):
            raise ValueError("Bot data paths must be distinct and non-overlapping")
        self.port = int(os.environ.get("LAVALINK_PORT", "2333"))
        self.timeout = int(os.environ.get("HEALTH_TIMEOUT", "120"))

    def bot(self, *args):
        return as_user(self.user, *args)

    def instance_user(self, name):
        import pwd

        user = os.environ.get(f"RED_USER_{name.upper()}", self.user)
        unit_user = output("systemctl", "show", "-p", "User", "--value", f"red@{name}.service")
        if pwd.getpwnam(user).pw_uid == 0 or unit_user != user:
            raise ValueError(f"Unexpected service account for {name}")
        return user

    def helper(self, operation, *args):
        command = [PYTHON, "-I", Path(__file__).resolve(), operation, *args]
        if operation in ("snapshot", "restore", "recovery-snapshot", "audio-schema"):
            for name, path in self.data.items():
                if args == (path,):
                    return as_user(self.instance_user(name), *command)
            raise ValueError("Unexpected instance data path")
        return self.bot(*command)

    def backup_data(self, name, path, operation):
        # A crash can leave a partial file, but only a complete archive is published.
        tmp = path.with_suffix(".partial")
        try:
            with tmp.open("wb") as dst:
                run(*self.helper(operation, self.data[name]), stdout=dst)
                os.fsync(dst.fileno())
            os.replace(tmp, path)
            sync_dir(path.parent)
            return checksum(path)
        finally:
            tmp.unlink(missing_ok=True)

    def lava(self, action):
        return as_user(self.lava_user, PYTHON, "-I", HELPERS / "release.py", action, self.lava_dir)

    def probe(self, name, *flags):
        socket = Path(f"/run/redbot-probe-{name}/check.sock")
        return as_user(
            self.instance_user(name), PYTHON, "-I", HELPERS / "probe.py", *flags, socket
        )

    def socket_disabled(self, name):
        # A missing socket can also mean that the Audio cog is still starting.
        # Only the actual running process can confirm that the probe was disabled.
        pid = output("systemctl", "show", "-p", "MainPID", "--value", f"red@{name}.service")
        if not pid.isdecimal() or pid == "0":
            return False
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except FileNotFoundError:
            return False
        prefix = b"RED_AUDIO_PROBE_SOCKET="
        return not any(value.startswith(prefix) and value != prefix for value in env)

    def invocation(self, name):
        return output("systemctl", "show", "-p", "InvocationID", "--value", f"red@{name}.service")

    def running_instances(self):
        running = []
        for name in self.names:
            unit = f"red@{name}.service"
            states = dict(
                line.split("=", 1)
                for line in output(
                    "systemctl", "show", "-p", "LoadState,ActiveState,SubState,Result", unit
                ).splitlines()
            )
            state = states["ActiveState"], states["SubState"]
            if states["LoadState"] == "loaded" and state == ("active", "running"):
                running.append(name)
            elif (
                states["LoadState"] != "loaded"
                or state != ("inactive", "dead")
                or states["Result"] != "success"
            ):
                raise RuntimeError(f"Cannot select rollout targets: {unit} is {states}")
        return running

    def idle(self, recovering=False, require_playback=False, participants=None):
        def check(args, optional=False):
            result = subprocess.run(args)
            if result.returncode == 75:
                raise Busy()
            if result.returncode == 78:
                if require_playback:
                    raise Busy(
                        "Music update deferred: configure an accessible test voice channel."
                    )
                return
            if not optional:
                result.check_returncode()

        check(self.lava("idle"))
        if recovering:
            # 교체 도중 중지되거나 실패한 인스턴스도 기존 트랜잭션으로 복구한다.
            running = [name for name in self.names if active(name)]
        else:
            running = self.running_instances()
            expected = self.names if participants is None else participants
            for name in expected:
                if name not in running:
                    raise RuntimeError(f"{name} stopped during the preflight check.")
        for name in running:
            if not recovering and not self.connected(name):
                raise RuntimeError(f"{name} is not connected to Lavalink.")
            socket = Path(f"/run/redbot-probe-{name}/check.sock")
            if socket.is_socket():
                check(self.probe(name, "--idle"), optional=recovering)
            if not socket.is_socket() and not recovering:
                if not self.socket_disabled(name):
                    raise Busy(f"Update deferred: waiting for the enabled {name} Audio probe.")
                if require_playback:
                    raise Busy(f"Music update deferred: {name} playback check socket is disabled.")

    def link(self, name):
        return self.root / "instances" / name

    def revision(self, venv):
        code = (
            "import json, importlib.metadata as m;"
            "d=json.loads(m.distribution('Red-DiscordBot').read_text('direct_url.json') or '{}');"
            "print(d.get('vcs_info', {}).get('commit_id', ''))"
        )
        return output(*self.bot(venv / "bin/python", "-I", "-c", code))

    def prepare(self, sha):
        releases = self.root / "releases"
        run(*self.bot("mkdir", "-p", releases))
        dest = releases / f"{sha}-{uuid.uuid4().hex}"
        save(str(dest), STATE / "staging.json")
        run(*self.bot("mkdir", "-m", "0700", dest))
        try:
            run(*self.bot(self.python, "-I", "-m", "venv", dest))
            python = dest / "bin/python"
            run(
                *self.bot(
                    python,
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "--no-cache-dir",
                    f"Red-DiscordBot @ git+{REPO}@{sha}",
                )
            )
            run(*self.bot(python, "-I", "-m", "pip", "check"))
            run(*self.bot(python, "-I", "-c", "import redbot.cogs.audio"))
            if self.revision(dest) != sha:
                raise RuntimeError("Candidate does not contain the requested Git commit")
        except BaseException:
            self.discard(dest)
            (STATE / "staging.json").unlink()
            sync_dir(STATE)
            raise
        return dest

    def discard(self, path):
        # The ledger only contains new environments we allocated. Also constrain
        # the directory and its exact generated name before running as the bot user.
        if path.parent != self.root / "releases" or not re.fullmatch(
            "[0-9a-f]{40}-[0-9a-f]{32}", path.name
        ):
            raise ValueError("Unexpected managed release path")
        if path.exists():
            run(*self.helper("discard", path))

    def check_layout(self, name):
        link = self.link(name)
        if not link.is_symlink():
            raise RuntimeError(f"Missing service runtime link for {name}")
        user = self.instance_user(name)
        code = (
            "import json, sys; from redbot.core import data_manager as d;"
            "d.load_basic_configuration(sys.argv[1]); c=d.basic_config;"
            "print(json.dumps([d.storage_type(), c['DATA_PATH'], c['CORE_PATH_APPEND'], c['COG_PATH_APPEND']]))"
        )
        backend, path, core, cogs = json.loads(
            output(*as_user(user, link / "bin/python", "-I", "-c", code, name))
        )
        if (backend, Path(path).resolve(), core, cogs) != (
            "JSON",
            self.data[name],
            "core",
            "cogs",
        ):
            raise RuntimeError(
                f"Configured JSON data layout does not match service instance {name}"
            )

    def connected(self, name, runtime=None):
        if not active(name):
            return False
        pid = output("systemctl", "show", "-p", "MainPID", "--value", f"red@{name}.service")
        peers = output("ss", "-Htnp", "state", "established", f"( dport = :{self.port} )")
        if not pid.isdecimal() or pid == "0" or f"pid={pid}," not in peers:
            return False
        if runtime is not None:
            # Native Audio dependencies are mapped from the physical venv, even
            # when systemd invoked Python via the stable per-instance symlink.
            try:
                maps = Path(f"/proc/{pid}/maps").read_text(errors="replace").splitlines()
            except FileNotFoundError:
                return False
            return any(f" {runtime}/" in line and "/site-packages/" in line for line in maps)
        return True

    def health(self, name, sha=None, *, require_playback=False):
        until = time.monotonic() + self.timeout
        socket = Path(f"/run/redbot-probe-{name}/check.sock")
        while time.monotonic() < until:
            runtime = self.link(name).resolve(strict=True) if sha else None
            if self.connected(name, runtime):
                if sha is None:
                    return
                ready = socket.is_socket()
                if not ready and not self.socket_disabled(name):
                    time.sleep(2)
                    continue
                if self.revision(self.link(name)) != sha:
                    raise RuntimeError(f"{name} has an unexpected revision")
                run(*self.lava("check"))
                if ready:
                    flags = ["--require-playback"] if require_playback else []
                    result = subprocess.run(self.probe(name, "--rollout-check", *flags))
                    if result.returncode == 75:
                        raise Busy()
                    if result.returncode == 76:
                        print(
                            f"{name}: skipped voice playback check (test channel occupied).",
                            flush=True,
                        )
                    elif result.returncode == 78:
                        if not socket.is_socket():
                            time.sleep(2)
                            continue
                        if require_playback:
                            raise Busy(f"Music update deferred: {name} has no test voice channel.")
                        print(
                            f"{name}: voice playback not verified (no test channel).", flush=True
                        )
                    else:
                        result.check_returncode()
                elif require_playback:
                    raise Busy(f"Music update deferred: {name} playback check socket is disabled.")
                else:
                    print(f"{name}: voice playback not verified (probe disabled).", flush=True)
                if not self.connected(name, runtime):
                    raise RuntimeError(f"{name} disconnected during the playback check")
                return
            time.sleep(2)
        raise RuntimeError(f"Timed out waiting for {name}")

    @staticmethod
    def pending_deliveries(receipt):
        if "deliveries" in receipt:
            return receipt["deliveries"]
        # 구버전 대기 알림은 마지막 봇이 보내기로 한 통합 DM이다.
        names = receipt["instances"]
        return {names[-1]: names}

    def completion_deliveries(self, tx):
        if not self.dm_sender:
            return {name: [name] for name in tx["instances"]}
        running = tx.get("running_instances", tx["instances"])
        priority = (self.dm_sender, *self.dm_fallbacks)
        sender = next(
            (name for name in priority if name in running and active(name)), self.dm_sender
        )
        # 오프라인인 대표 봇은 같은 버전으로 돌아와야 수신 확인을 할 수 있다.
        display_names = [name for name in self.names if name in running or name == sender]
        return {sender: display_names}

    def deliver_completion_dm(self, managed):
        receipt = managed.get("pending_notification")
        if not receipt:
            return
        sha = receipt["sha"]
        deliveries = self.pending_deliveries(receipt)
        for sender, display_names in list(deliveries.items()):
            if sender not in self.names or not active(sender):
                print(f"Completion DM waiting for {sender} at {sha[:12]}.", flush=True)
                continue
            try:
                run(
                    *as_user(
                        self.instance_user(sender),
                        PYTHON,
                        "-I",
                        Path(__file__).resolve(),
                        "completion-dm",
                        sha,
                        sender,
                        *display_names,
                    ),
                    timeout=40,
                )
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                print(f"Completion DM deferred for {sender} at {sha[:12]}: {exc}", flush=True)
                continue
            del deliveries[sender]
            if deliveries:
                managed["pending_notification"] = {"sha": sha, "deliveries": deliveries}
            else:
                del managed["pending_notification"]
            save(managed, STATE / "managed.json")
            print(f"Completion DM acknowledged by {sender} for {sha[:12]}.", flush=True)

    def complete(self, tx, committed):
        ledger = STATE / "managed.json"
        known = (
            json.loads(ledger.read_text()) if ledger.exists() else {"releases": [], "backups": []}
        )
        releases = set(known["releases"]) | {tx["candidate"]}
        keep = {
            str(self.link(name).resolve(strict=name in tx["instances"])) for name in self.names
        }
        keep.update(row["old"] for row in tx["instances"].values())
        if not committed:
            keep.update(known["releases"])
        for retired in releases - keep:
            self.discard(Path(retired))
        all_backups = set(known["backups"]) | {tx["backup"]}
        if previous_failure := known.get("failed_backup"):
            all_backups.add(previous_failure)
        if committed:
            backups = list(dict.fromkeys([*known["backups"], tx["backup"]]))[-2:]
            # A successful retry must not delete the only copy of user changes
            # made by a canary before an incompatible rollback.
            failed_backup = previous_failure
        else:
            backups = known["backups"]
            failed_backup = tx["backup"]
        for retired in all_backups - set(backups) - {failed_backup}:
            path = Path(retired)
            if path.parent != STATE or not re.fullmatch("[0-9a-f]{12}-[a-zA-Z0-9_]+", path.name):
                raise ValueError("Unexpected managed backup path")
            if path.exists():
                shutil.rmtree(path)
        managed = {
            "releases": sorted(releases & keep),
            "backups": backups,
            "failed_backup": failed_backup,
        }
        if committed:
            # 전송 대기 중인 알림은 마지막으로 검증한 배포 건만 유지한다.
            deliveries = self.completion_deliveries(tx)
            pending = known.get("pending_notification")
            if pending and pending["sha"] == tx["sha"]:
                # 같은 대상의 재시도는 원문을 유지하고, 새 그룹의 통합 알림은 최신 내용을 보낸다.
                for sender, previous in self.pending_deliveries(pending).items():
                    if sender not in deliveries or set(previous) == set(deliveries[sender]):
                        deliveries[sender] = previous
            managed["pending_notification"] = {"sha": tx["sha"], "deliveries": deliveries}
        else:
            if "pending_notification" in known:
                managed["pending_notification"] = known["pending_notification"]
            if "retry_after" in tx:
                managed["automatic_retry"] = {"sha": tx["sha"], "after": tx["retry_after"]}
            elif "automatic_retry" in known:
                managed["automatic_retry"] = known["automatic_retry"]
        save(managed, ledger)
        finish()
        print("Verified new runtimes." if committed else "Restored previous runtimes.", flush=True)
        if committed:
            self.deliver_completion_dm(managed)

    def rollback_instance(self, tx, name):
        row = tx["instances"][name]
        phase = row["phase"]
        if phase == "pending":
            return
        unit = f"red@{name}.service"
        if phase in ("backed_up", "ready", "rolling_back"):
            backup = Path(tx["backup"])
            archive = backup / f"{name}.tar.gz"
            if checksum(archive) != row["checksum"]:
                raise RuntimeError(f"Offline snapshot failed its integrity check for {name}")
            was_active = active(name)
            if was_active:
                self.idle(recovering=True)
            run("systemctl", "stop", unit)
            current = backup / f"{name}-before-rollback.tar.gz"
            if phase != "rolling_back":
                # Publish and journal the latest offline data before mutating anything.
                # Include surviving files even if a failed migration removed its database.
                try:
                    recovered_hash = self.backup_data(name, current, "recovery-snapshot")
                    schema = row.get("audio_schema")
                    keep_data = bool(
                        phase == "ready"
                        and schema
                        and output(*self.helper("audio-schema", self.data[name])) == schema
                    )
                    row.update(
                        phase="rolling_back", keep_data=keep_data, recovery_checksum=recovered_hash
                    )
                    save(tx)
                except Exception:
                    if row["phase"] == "ready" and was_active:
                        run("systemctl", "start", unit)
                    raise
            if checksum(current) != row["recovery_checksum"]:
                raise RuntimeError(f"Pre-rollback snapshot failed its integrity check for {name}")
            # The runtime switch follows the data restore. If it already completed,
            # replaying the old snapshot could erase writes accepted after a reboot.
            already_switched = phase == "rolling_back" and (
                self.link(name).resolve(strict=True) == Path(row["old"])
            )
            if phase == "rolling_back" and was_active:
                # systemd may have started the bot on reboot. Preserve additional
                # writes without replacing the original pre-rollback evidence.
                resumed = backup / f"{name}-after-restart.tar.gz"
                row["restart_checksum"] = self.backup_data(name, resumed, "recovery-snapshot")
                save(tx)
                print(f"{name}: saved restarted service data to {resumed}", flush=True)
            if not row["keep_data"] and not already_switched:
                with archive.open("rb") as src:
                    run(*self.helper("restore", self.data[name]), stdin=src)
            run(*self.helper("switch", self.link(name), row["old"]))
            row["phase"] = "restored"
            save(tx)
            action = (
                "kept current data"
                if row["keep_data"] or already_switched
                else "restored pre-update data"
            )
            print(f"{name}: {action}. Pre-rollback data: {current}", flush=True)
        # Before "backed_up" no new code has run; after "restored" do not
        # overwrite data that the restored bot has accepted since recovery.
        run("systemctl", "start", unit)
        self.health(name)
        row["phase"] = "pending"
        save(tx)

    def rollback(self, tx):
        errors = []
        for name in reversed(tx["instances"]):
            try:
                self.rollback_instance(tx, name)
            except Busy:
                raise
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if errors:
            raise RuntimeError("Rollback incomplete: " + ", ".join(errors))
        tx["completed"] = False
        if tx.get("candidate_failed"):
            tx["retry_after"] = time.time() + AUTOMATIC_RETRY_SECONDS
        save(tx)
        self.complete(tx, committed=False)

    def update(self, *, automatic=False):
        staging = STATE / "staging.json"
        if staging.exists():
            # An incomplete install never changes a service link. Once the
            # transaction exists, it owns the candidate through commit/rollback.
            if not JOURNAL.exists():
                self.discard(Path(json.loads(staging.read_text())))
            staging.unlink()
            sync_dir(STATE)
        if JOURNAL.exists():
            tx = json.loads(JOURNAL.read_text())
            targets = list(tx["instances"])
            running = tx.get("running_instances", self.names)
            if (
                not targets
                or list(tx["data"]) != self.names
                or tx["data"] != {k: str(v) for k, v in self.data.items()}
                or running != [n for n in self.names if n in running]
                or targets != [n for n in running if n in targets]
            ):
                raise RuntimeError("Restore the original instance configuration before recovering")
            if "completed" in tx:
                self.complete(tx, tx["completed"])
            elif tx.pop("deferred", False):
                save(tx)
                self.apply(tx)
            else:
                print("Recovering an interrupted rollout.", flush=True)
                self.rollback(tx)
            return
        ledger = STATE / "managed.json"
        managed = json.loads(ledger.read_text()) if ledger.exists() else {}
        self.deliver_completion_dm(managed)
        running = self.running_instances()
        if not running:
            print("No running instances to update.")
            return
        if self.names[0] not in running:
            print(f"Update deferred: main instance {self.names[0]} is stopped.", flush=True)
            return
        remote = output(*self.bot("git", "ls-remote", "--exit-code", REPO, REF)).split()
        if len(remote) != 2 or remote[1] != REF or not re.fullmatch("[0-9a-f]{40}", remote[0]):
            raise ValueError("Could not resolve the fork branch")
        sha = remote[0]
        targets = []
        for name in running:
            self.check_layout(name)
            runtime = self.link(name).resolve(strict=True)
            if self.revision(runtime) != sha:
                targets.append(name)
            elif not self.connected(name, runtime):
                raise RuntimeError(
                    f"{name} has the requested revision but is disconnected or using a different venv"
                )
        if not targets:
            print(f"All running instances already use {sha}.")
            return
        if automatic:
            retry = managed.get("automatic_retry")
            if retry and retry["sha"] == sha and time.time() < retry["after"]:
                print(f"Delaying automatic retry of {sha} after its failed rollout.", flush=True)
                return
        self.idle(participants=running)
        if not all(self.connected(name) for name in running):
            raise RuntimeError("All existing bot services must be connected before an update")
        dest = self.prepare(sha)
        inspected = {}
        for venv in dict.fromkeys([dest, *(self.link(n).resolve(strict=True) for n in targets)]):
            inspected[venv] = json.loads(
                output(
                    *self.bot(venv / "bin/python", "-I", Path(__file__).resolve(), "runtime-info")
                )
            )
        incoming = inspected[dest]
        backup = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}-", dir=STATE))
        tx = {
            "backup": str(backup),
            "candidate": str(dest),
            "sha": sha,
            "data": {k: str(v) for k, v in self.data.items()},
            "running_instances": running,
            "instances": {},
            "require_playback": False,
        }
        for name in targets:
            old = self.link(name).resolve(strict=True)
            previous = inspected[old]
            tx["require_playback"] |= (
                not incoming["music"] or incoming["music"] != previous["music"]
            )
            tx["instances"][name] = {
                "old": str(old),
                "phase": "pending",
                "compatible_data": bool(
                    incoming["format"] and incoming["format"] == previous["format"]
                ),
            }
        save(tx)
        staging.unlink()
        sync_dir(STATE)
        self.apply(tx)

    def verify_main_before_secondary(self, tx):
        main = self.names[0]
        row = tx["instances"].get(main)
        if row is not None and row["phase"] != "ready":
            raise RuntimeError(f"Main instance {main} has not passed rollout verification")
        runtime = (
            Path(tx["candidate"]) if row is not None else self.link(main).resolve(strict=True)
        )
        if self.revision(self.link(main)) != tx["sha"] or not self.connected(main, runtime):
            raise RuntimeError(f"Main instance {main} is not running the expected revision")
        invocation = self.invocation(main)
        verified = row.get("verified_invocation") if row is not None else tx.get("verified_main")
        if invocation and verified == invocation:
            return
        # 이미 같은 커밋인 메인도 현재 프로세스의 검증이 끝나야 보조 봇을 교체한다.
        require_playback = tx.get("require_playback", True)
        self.health(main, tx["sha"], require_playback=require_playback)
        self.idle(
            require_playback=require_playback,
            participants=tx.get("running_instances", tx["instances"]),
        )
        if not invocation or invocation != self.invocation(main):
            raise RuntimeError(f"Main instance {main} restarted during verification")
        if row is not None:
            row["verified_invocation"] = invocation
        else:
            tx["verified_main"] = invocation
        save(tx)

    def apply(self, tx):
        checking_candidate = False
        require_playback = tx.get("require_playback", True)
        try:
            for name, row in tx["instances"].items():
                if (
                    row["phase"] == "ready"
                    and row.get("verified_invocation")
                    and row["verified_invocation"] == self.invocation(name)
                    and self.connected(name, Path(tx["candidate"]))
                    and self.revision(self.link(name)) == tx["sha"]
                ):
                    # Re-check both bots' idleness before the next cutover. Do not
                    # rejoin their shared channel for a trial this process completed.
                    continue
                self.idle(
                    require_playback=require_playback,
                    participants=tx.get("running_instances", tx["instances"]),
                )
                if name != self.names[0]:
                    self.verify_main_before_secondary(tx)
                needs_start = row["phase"] == "pending"
                if needs_start:
                    row["phase"] = "stopping"
                    save(tx)
                    unit = f"red@{name}.service"
                    run("systemctl", "stop", unit)
                    backup = Path(tx["backup"])
                    archive = backup / f"{name}.tar.gz"
                    row["checksum"] = self.backup_data(name, archive, "snapshot")
                    if row.get("compatible_data"):
                        row["audio_schema"] = output(*self.helper("audio-schema", self.data[name]))
                    row["phase"] = "backed_up"
                    save(tx)
                    run(*self.helper("switch", self.link(name), tx["candidate"]))
                checking_candidate = True
                if needs_start:
                    run("systemctl", "start", unit)
                invocation = self.invocation(name)
                self.health(name, tx["sha"], require_playback=require_playback)
                if invocation != self.invocation(name):
                    raise RuntimeError(f"{name} restarted during candidate verification")
                checking_candidate = False
                row["phase"] = "ready"
                row["verified_invocation"] = invocation
                save(tx)
                print(f"Verified {name} at {tx['sha']}.", flush=True)
            for name in tx.get("running_instances", ()):
                if name in tx["instances"]:
                    continue
                runtime = self.link(name).resolve(strict=True)
                if self.revision(runtime) != tx["sha"] or not self.connected(name, runtime):
                    raise RuntimeError(f"{name} is no longer running the expected revision")
        except Busy:
            # 두 봇을 교체하는 사이에 봇이 음성 채널에 연결될 수 있다.
            # 다음 점검에서는 검증이 끝난 봇을 다시 중지하지 않는다.
            tx["deferred"] = True
            save(tx)
            raise
        except Exception:
            try:
                if checking_candidate:
                    tx["candidate_failed"] = True
                    save(tx)
            finally:
                self.rollback(tx)
            raise
        tx["completed"] = True
        save(tx)
        self.complete(tx, committed=True)
        print(f"Updated all instances. Offline backups: {tx['backup']}", flush=True)


def main():
    automatic = sys.argv[1:] == ["--automatic"]
    if len(sys.argv) > 1 and not automatic:
        if os.geteuid() == 0:
            raise ValueError("File operations must run as the instance user")
        command, *args = sys.argv[1:]
        if command == "runtime-info":
            print(json.dumps(runtime_info()))
        elif command == "completion-dm":
            request_completion_dm(args[0], args[1], args[2:])
        elif command == "audio-schema":
            print(audio_schema(Path(args[0])))
        elif command in ("snapshot", "recovery-snapshot"):
            snapshot(Path(args[0]), sys.stdout.buffer, recovery=command == "recovery-snapshot")
        elif command == "restore":
            restore(Path(args[0]), sys.stdin.buffer)
        elif command == "switch":
            switch(*map(Path, args))
        elif command == "discard":
            shutil.rmtree(args[0])
        else:
            raise ValueError("Unknown action")
        return
    import fcntl

    if os.geteuid() != 0:
        raise ValueError("Start the root-owned systemd service to run the rollout")
    with os.fdopen(os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "r+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another audio maintenance job is running.")
            return
        try:
            Rollout().update(automatic=automatic)
        except Busy as exc:
            print(str(exc) or "A music bot is busy in voice. Run the update again when idle.")


if __name__ == "__main__":
    main()
