"""Stage and verify the shared Linux music bots, with recoverable per-instance cutover."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
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


class Busy(Exception):
    """Another maintenance operation or music listener needs the running service."""


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


def snapshot(root, stream):
    paths = sorted(data_files(root))
    if Path("core/settings.json") not in paths or Path("cogs/Audio/Audio.db") not in paths:
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
        len(names) < 2
        or len(names) != len(set(names))
        or any(not re.fullmatch("[a-z][a-z0-9_]{0,31}", name) for name in names)
    ):
        raise ValueError("RED_INSTANCES must list distinct lowercase instance names, canary first")
    return names


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

    def helper(self, operation, *args):
        return self.bot(PYTHON, "-I", Path(__file__).resolve(), operation, *args)

    def lava(self, action):
        return as_user(self.lava_user, PYTHON, "-I", HELPERS / "release.py", action, self.lava_dir)

    def probe(self, name, *flags):
        socket = Path(f"/run/redbot-probe-{name}/check.sock")
        return self.bot(PYTHON, "-I", HELPERS / "probe.py", *flags, socket)

    def idle(self, recovering=False):
        def check(args, optional=False):
            result = subprocess.run(args)
            if result.returncode == 75:
                raise Busy()
            if not optional:
                result.check_returncode()

        check(self.lava("idle"))
        for name in self.names:
            if active(name) and Path(f"/run/redbot-probe-{name}/check.sock").is_socket():
                check(self.probe(name, "--available"), optional=recovering)

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
        unit_user = output("systemctl", "show", "-p", "User", "--value", f"red@{name}.service")
        if unit_user != self.user:
            raise RuntimeError(f"Unexpected service account for {name}")
        code = (
            "import json, sys; from redbot.core import data_manager as d;"
            "d.load_basic_configuration(sys.argv[1]); c=d.basic_config;"
            "print(json.dumps([d.storage_type(), c['DATA_PATH'], c['CORE_PATH_APPEND'], c['COG_PATH_APPEND']]))"
        )
        backend, path, core, cogs = json.loads(
            output(*self.bot(link / "bin/python", "-I", "-c", code, name))
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

    def health(self, name, sha=None):
        until = time.monotonic() + self.timeout
        while time.monotonic() < until:
            runtime = self.link(name).resolve(strict=True) if sha else None
            if self.connected(name, runtime):
                if sha is None:
                    return
                if Path(f"/run/redbot-probe-{name}/check.sock").is_socket():
                    if self.revision(self.link(name)) != sha:
                        raise RuntimeError(f"{name} has an unexpected revision")
                    run(*self.lava("check"))
                    result = subprocess.run(self.probe(name))
                    if result.returncode == 75:
                        raise Busy()
                    result.check_returncode()
                    if not self.connected(name, runtime):
                        raise RuntimeError(f"{name} disconnected during the playback check")
                    return
            time.sleep(2)
        raise RuntimeError(f"Timed out waiting for {name}")

    def complete(self, tx, committed):
        ledger = STATE / "managed.json"
        known = (
            json.loads(ledger.read_text()) if ledger.exists() else {"releases": [], "backups": []}
        )
        releases = set(known["releases"]) | {tx["candidate"]}
        keep = {str(self.link(name).resolve(strict=True)) for name in self.names}
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
            failed_backup = None
        else:
            backups = known["backups"]
            failed_backup = tx["backup"]
        for retired in all_backups - set(backups) - {failed_backup}:
            path = Path(retired)
            if path.parent != STATE or not re.fullmatch("[0-9a-f]{12}-[a-zA-Z0-9_]+", path.name):
                raise ValueError("Unexpected managed backup path")
            if path.exists():
                shutil.rmtree(path)
        save(
            {
                "releases": sorted(releases & keep),
                "backups": backups,
                "failed_backup": failed_backup,
            },
            ledger,
        )
        finish()
        print("Verified new runtimes." if committed else "Restored previous runtimes.", flush=True)

    def rollback_instance(self, tx, name):
        row = tx["instances"][name]
        phase = row["phase"]
        if phase == "pending":
            return
        unit = f"red@{name}.service"
        if phase in ("backed_up", "ready"):
            archive = Path(tx["backup"]) / f"{name}.tar.gz"
            if checksum(archive) != row["checksum"]:
                raise RuntimeError(f"Offline snapshot failed its integrity check for {name}")
            if active(name):
                self.idle(recovering=True)
            run("systemctl", "stop", unit)
            with archive.open("rb") as src:
                run(*self.helper("restore", self.data[name]), stdin=src)
            run(*self.helper("switch", self.link(name), row["old"]))
            row["phase"] = "restored"
            save(tx)
        # Before "backed_up" no new code has run; after "restored" do not
        # overwrite data that the restored bot has accepted since recovery.
        run("systemctl", "start", unit)
        self.health(name)
        row["phase"] = "pending"
        save(tx)

    def rollback(self, tx):
        errors = []
        for name in reversed(self.names):
            try:
                self.rollback_instance(tx, name)
            except Busy:
                raise
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if errors:
            raise RuntimeError("Rollback incomplete: " + ", ".join(errors))
        tx["completed"] = False
        save(tx)
        self.complete(tx, committed=False)

    def update(self):
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
            if list(tx["instances"]) != self.names or tx["data"] != {
                k: str(v) for k, v in self.data.items()
            }:
                raise RuntimeError("Restore the original instance configuration before recovering")
            if "completed" in tx:
                self.complete(tx, tx["completed"])
            elif tx.pop("deferred", False):
                self.idle()
                save(tx)
                self.apply(tx)
            else:
                print("Recovering an interrupted rollout.", flush=True)
                self.rollback(tx)
            return
        remote = output(*self.bot("git", "ls-remote", "--exit-code", REPO, REF)).split()
        if len(remote) != 2 or remote[1] != REF or not re.fullmatch("[0-9a-f]{40}", remote[0]):
            raise ValueError("Could not resolve the fork branch")
        sha = remote[0]
        for name in self.names:
            self.check_layout(name)
        if all(self.revision(self.link(name)) == sha for name in self.names):
            if not all(
                self.connected(name, self.link(name).resolve(strict=True)) for name in self.names
            ):
                raise RuntimeError(
                    "Installed revision matches, but a bot is disconnected or using a different venv"
                )
            print(f"All instances already use {sha}.")
            return
        self.idle()
        if not all(self.connected(name) for name in self.names):
            raise RuntimeError("All existing bot services must be connected before an update")
        dest = self.prepare(sha)
        backup = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}-", dir=STATE))
        tx = {
            "backup": str(backup),
            "candidate": str(dest),
            "sha": sha,
            "data": {k: str(v) for k, v in self.data.items()},
            "instances": {
                name: {"old": str(self.link(name).resolve(strict=True)), "phase": "pending"}
                for name in self.names
            },
        }
        save(tx)
        staging.unlink()
        sync_dir(STATE)
        self.apply(tx)

    def apply(self, tx):
        try:
            for name in self.names:
                self.idle()
                row = tx["instances"][name]
                if row["phase"] == "pending":
                    row["phase"] = "stopping"
                    save(tx)
                    unit = f"red@{name}.service"
                    run("systemctl", "stop", unit)
                    backup = Path(tx["backup"])
                    archive = backup / f"{name}.tar.gz"
                    with archive.open("xb") as dst:
                        run(*self.helper("snapshot", self.data[name]), stdout=dst)
                        os.fsync(dst.fileno())
                    sync_dir(backup)
                    row["checksum"] = checksum(archive)
                    row["phase"] = "backed_up"
                    save(tx)
                    run(*self.helper("switch", self.link(name), tx["candidate"]))
                    run("systemctl", "start", unit)
                self.health(name, tx["sha"])
                row["phase"] = "ready"
                save(tx)
                print(f"Verified {name} at {tx['sha']}.", flush=True)
        except Busy:
            # A listener may have joined between the first and second cutover.
            # Resume when idle without stopping the healthy canary a second time.
            tx["deferred"] = True
            save(tx)
            raise
        except Exception:
            self.rollback(tx)
            raise
        tx["completed"] = True
        save(tx)
        self.complete(tx, committed=True)
        print(f"Updated all instances. Offline backups: {tx['backup']}", flush=True)


def main():
    if len(sys.argv) > 1:
        if os.geteuid() == 0:
            raise ValueError("File operations must run as the instance user")
        command, *args = sys.argv[1:]
        if command == "snapshot":
            snapshot(Path(args[0]), sys.stdout.buffer)
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
            Rollout().update()
        except Busy:
            print("Music or its test channel is busy. Run the update again when idle.")


if __name__ == "__main__":
    main()
