import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

DEFAULT_ROOM = "21292831"
PLAYER = Path(__file__).resolve().parent / "mpv" / "mpv.exe"


ROOM_LINK = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?live\.bilibili\.com/(?:h5/|blanc/)?([0-9]{1,20})(?![0-9])",
    re.IGNORECASE,
)
SHORT_LINK = re.compile(r"(?:https?://)?b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)


def room_number(value):
    """Return the room number from a number, a live link, or pasted share text."""
    value = value.strip()
    match = re.fullmatch(r"[0-9]{1,20}", value) or ROOM_LINK.search(value)
    number = match and (match[1] if match.re is ROOM_LINK else match[0])
    if number and int(number):
        return str(int(number))
    raise ValueError("请输入 B 站直播房间号或直播链接。")


def short_link(value):
    match = SHORT_LINK.search(value)
    if match:
        url = match[0]
        return url if url.lower().startswith("http") else "https://" + url
    return None


def resolve_room(value, timeout=8):
    """Like room_number, but also follows b23.tv share links (needs the network)."""
    url = short_link(value)
    if not url:
        return room_number(value)
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            target = response.geturl()
    except OSError as error:
        raise ValueError("短链接解析失败，请检查网络。") from error
    try:
        return room_number(target)
    except ValueError:
        raise ValueError("这个短链接不是直播间链接。") from None


def stream_command(room):
    return [
        sys.executable, "-m", "streamlink", "--stdout",
        f"https://live.bilibili.com/{room}", "httpstream,best",
    ]


def player_command(*options):
    return [
        str(PLAYER), "--no-config", "--no-video", "--no-input-terminal",
        "--term-status-msg=", *options, "-",
    ]


def main():
    parser = argparse.ArgumentParser(description="Play Bilibili live audio in mpv.")
    parser.add_argument("room", nargs="?", default=DEFAULT_ROOM)
    args = parser.parse_args()
    try:
        room = resolve_room(args.room)
    except ValueError as error:
        parser.error(str(error))
    if not PLAYER.is_file():
        parser.error(f"Player missing: {PLAYER}")

    stream = subprocess.Popen(stream_command(room), stdout=subprocess.PIPE)
    playback = None
    try:
        playback = subprocess.Popen(player_command(), stdin=stream.stdout)
        stream.stdout.close()
        player_status = playback.wait()
        if player_status:
            return player_status
        try:
            return stream.wait(timeout=2)
        except subprocess.TimeoutExpired:
            print("mpv exited while Streamlink was still running.", file=sys.stderr)
            return 1
    except KeyboardInterrupt:
        return 130
    finally:
        stream.stdout.close()
        for process in (playback, stream):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait()


if __name__ == "__main__":
    sys.exit(main())
