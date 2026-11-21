"""Ask a running Red Audio cog to check its designated test voice channel."""

import argparse
import socket
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--available", action="store_true", help="only check channel availability")
    parser.add_argument("socket_path")
    args = parser.parse_args()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(80)
            client.connect(args.socket_path)
            client.sendall(b"available\n" if args.available else b"check\n")
            with client.makefile("rb") as reader:
                response = reader.readline(32)
    except OSError as exc:
        print(f"Cannot contact Audio playback check: {exc}", file=sys.stderr)
        return 1
    if response == b"unconfigured\n":
        print("Playback check skipped: no test voice channel configured.")
        return 0
    if response == b"busy\n":
        print("Playback check deferred: voice channel or bot is busy.")
        return 75
    if args.available and response == b"ready\n":
        return 0
    if not args.available and response == b"ok\n":
        print("Lavalink reported advancing playback in Discord voice.")
        return 0
    print("Playback check failed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
