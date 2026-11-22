"""Ask a running Red Audio cog to check its designated test voice channel."""

import argparse
import socket
import sys


def ask(socket_path, request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(80)
        client.connect(socket_path)
        client.sendall(request + b"\n")
        with client.makefile("rb") as reader:
            return reader.readline(32)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--available", action="store_true", help="only check channel availability")
    mode.add_argument(
        "--idle", action="store_true", help="check the bot's voice use in all guilds"
    )
    mode.add_argument(
        "--rollout-check", action="store_true", help="run a bot rollout playback check"
    )
    parser.add_argument(
        "--require-playback", action="store_true", help="require verified playback to track end"
    )
    parser.add_argument("socket_path")
    args = parser.parse_args()
    if args.available or args.idle:
        legacy_request = b"available"
    elif args.require_playback:
        legacy_request = b"verify"
    else:
        legacy_request = b"check"
    request = legacy_request
    if args.idle:
        request = b"idle"
    elif args.rollout_check:
        request = b"rollout-" + legacy_request
    try:
        response = ask(args.socket_path, request)
        if response == b"invalid\n" and request != legacy_request:
            # 구버전 봇이 새 요청을 모를 때만 기존 점검 방식으로 재시도한다.
            print("Older Audio probe: using the original test-channel check.", flush=True)
            request = legacy_request
            response = ask(args.socket_path, request)
    except FileNotFoundError:
        print("Playback unverified: the bot's test socket is missing.", file=sys.stderr)
        return 78
    except OSError as exc:
        print(f"Cannot contact Audio playback check: {exc}", file=sys.stderr)
        return 1
    if response == b"unconfigured\n":
        print("Playback unverified: no test voice channel configured.", file=sys.stderr)
        return 78
    if response == b"unavailable\n":
        print(
            "Playback unverified: the configured test voice channel is inaccessible.",
            file=sys.stderr,
        )
        return 78
    if response == b"busy\n":
        if request == b"idle" or request.startswith(b"rollout-"):
            print("Bot rollout deferred: the bot is using voice.")
        else:
            print("Playback check deferred: voice channel or bot is busy.")
        return 75
    if request.startswith(b"rollout-") and response == b"occupied\n":
        print("Playback not verified: the test voice channel is occupied.")
        return 76
    if legacy_request == b"available" and response == b"ready\n":
        return 0
    if legacy_request == b"verify" and response == b"finished\n":
        print("Lavalink reached the end of the test track while connected to Discord voice.")
        return 0
    if legacy_request == b"check" and response == b"ok\n":
        print("The bot passed its configured playback check.")
        return 0
    print("Playback check failed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
