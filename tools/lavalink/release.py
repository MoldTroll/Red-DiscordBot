"""Manage an external Red-compatible JAR as the Lavalink service account."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from zipfile import BadZipFile, ZipFile

import yaml


REPO = "Cog-Creators/Lavalink-Jars"
JAR = Path("Lavalink.jar")
STATE = Path(".lavalink-refresh")
BACKUP = STATE / "previous.jar"
PENDING = STATE / "pending"


def version(tag):
    match = re.fullmatch(r"3\.7\.(\d+)\+red\.(\d+)", tag)
    if match is None:
        raise ValueError(f"Unsupported Lavalink release line: {tag}")
    return tuple(map(int, match.groups()))


def jar_version(path):
    with ZipFile(path) as archive:
        lines = archive.read("META-INF/MANIFEST.MF").decode().splitlines()
    prefix = "Implementation-Version: "
    return next(line.removeprefix(prefix) for line in lines if line.startswith(prefix))


def sha256(path):
    with path.open("rb") as incoming:
        return hashlib.file_digest(incoming, "sha256").hexdigest()


def write_phase(value):
    with tempfile.TemporaryDirectory(dir=STATE) as work:
        tmp = Path(work) / "pending"
        tmp.write_text(value + "\n", encoding="ascii")
        os.replace(tmp, PENDING)


def phase():
    if not PENDING.exists():
        return "none"
    value = PENDING.read_text(encoding="ascii").strip()
    if value in ("", "rollback"):  # Older releases wrote an empty marker.
        return "rollback"
    tag = value.removeprefix("candidate ")
    if tag == value:
        raise ValueError("Unknown Lavalink update state")
    version(tag)
    # A process can stop between recording intent and installing the candidate.
    try:
        installed = jar_version(JAR)
    except (FileNotFoundError, BadZipFile, KeyError, StopIteration, UnicodeDecodeError):
        return "rollback"
    if installed != tag:
        return "rollback"
    return "candidate"


def rollback():
    if not PENDING.is_file() or not BACKUP.is_file():
        raise ValueError("Missing Lavalink rollback state")
    write_phase("rollback")
    if not JAR.is_file() or sha256(BACKUP) != sha256(JAR):
        with tempfile.TemporaryDirectory(dir=STATE) as work:
            saved = Path(work) / JAR
            shutil.copy2(BACKUP, saved)
            os.replace(saved, JAR)


def local_api(path, config):
    port = int(config["server"]["port"])
    headers = {"Authorization": config["lavalink"]["server"]["password"]}
    request = Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
        return json.load(response)


def update(config):
    if PENDING.exists():
        return 12
    with urlopen(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=30) as response:
        release = json.load(response)
    tag = release["tag_name"]
    current = jar_version(JAR)
    if release["draft"] or release["prerelease"] or version(tag) < version(current):
        raise ValueError("Refusing a prerelease or downgrade")
    (asset,) = [asset for asset in release["assets"] if asset["name"] == "Lavalink.jar"]
    digest = asset["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Release has no verified SHA-256 digest")
    if current == tag:
        if digest != f"sha256:{sha256(JAR)}":
            raise ValueError("Installed JAR differs from the published release")
        print(f"Lavalink {tag} is current.")
        return 10

    STATE.mkdir(mode=0o700, exist_ok=True)
    url = f"https://github.com/{REPO}/releases/download/{quote(tag, safe='')}/Lavalink.jar"
    with tempfile.TemporaryDirectory(dir=STATE) as work:
        tmp = Path(work) / JAR
        with urlopen(url, timeout=45) as response, tmp.open("wb") as destination:
            shutil.copyfileobj(response, destination)
        if digest != f"sha256:{sha256(tmp)}" or jar_version(tmp) != tag:
            raise ValueError("Downloaded JAR failed verification")
        if local_api("/v3/stats", config)["playingPlayers"] != 0:
            print("Deferring update while music is playing.")
            return 10
        backup = Path(work) / "previous.jar"
        shutil.copy2(JAR, backup)
        os.replace(backup, BACKUP)
        write_phase(f"candidate {tag}")
        os.replace(tmp, JAR)
    print(f"Installed Lavalink {tag}.")
    return 0


def main():
    action = sys.argv[1]
    os.chdir(sys.argv[2])
    if action == "phase":
        print(phase())
        return 0
    if action == "rollback":
        rollback()
        return 0
    if action == "commit":
        PENDING.unlink()
        return 0
    config = yaml.safe_load(Path("application.yml").read_text())
    if action == "port":
        print(int(config["server"]["port"]))
    elif action == "running":
        live_version = local_api("/v3/info", config)["version"]["semver"]
        return 0 if live_version == jar_version(JAR) else 1
    elif action == "update":
        return update(config)
    elif action == "check":
        local_api("/v3/stats", config)
        if config.get("plugins", {}).get("youtube", {}).get("enabled", False):
            plugins = local_api("/v3/info", config)["plugins"]
            if not any(plugin["name"] == "youtube-plugin" for plugin in plugins):
                raise ValueError("The configured YouTube plugin did not load")
            query = urlencode({"identifier": "ytsearch:Big Buck Bunny Blender Foundation"})
            if not local_api(f"/loadtracks?{query}", config)["tracks"]:
                raise ValueError("YouTube search returned no tracks")
    elif action == "idle":
        return 0 if local_api("/v3/stats", config)["playingPlayers"] == 0 else 75
    else:
        raise ValueError(f"Unknown action {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
