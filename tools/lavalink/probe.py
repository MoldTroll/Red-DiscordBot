"""Ask a running Red Audio cog to check its designated test voice channel."""

import argparse
import socket
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--available", action="store_true", help="only check channel availability")
    parser.add_argument(
        "--require-playback", action="store_true", help="require verified playback to track end"
    )
    parser.add_argument("socket_path")
    args = parser.parse_args()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(80)
            client.connect(args.socket_path)
            request = b"verify\n" if args.require_playback else b"check\n"
            client.sendall(b"available\n" if args.available else request)
            with client.makefile("rb") as reader:
                response = reader.readline(32)
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
        print("Playback check deferred: voice channel or bot is busy.")
        return 75
    if args.available and response == b"ready\n":
        return 0
    if not args.available and args.require_playback and response == b"finished\n":
        print("Lavalink reached the end of the test track while connected to Discord voice.")
        return 0
    if not args.available and not args.require_playback and response == b"ok\n":
        print("The bot passed its configured playback check.")
        return 0
    print("Playback check failed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
