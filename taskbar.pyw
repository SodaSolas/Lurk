import ctypes as ct
from ctypes import wintypes as wt
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
from threading import Thread
import time
from urllib.request import Request, urlopen
import uuid
import winreg

from PySide6 import QtCore, QtGui, QtWidgets
import qrcode

import bili_account as account
from listen import (DEFAULT_ROOM, PLAYER, player_command, resolve_room, room_number,
                    short_link, stream_command)


APP_NAME = "B 站直播电台"
STATE_DIR = Path(os.environ["LOCALAPPDATA"]) / "BiliLiveAudio"
USER = ct.WinDLL("user32", use_last_error=True)
KERNEL = ct.WinDLL("kernel32", use_last_error=True)
DWM = ct.WinDLL("dwmapi")


def native(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = arguments
    return function


find_window = native(USER, "FindWindowW", wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
find_child = native(USER, "FindWindowExW", wt.HWND, wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
get_parent = native(USER, "GetParent", wt.HWND, wt.HWND)
set_parent = native(USER, "SetParent", wt.HWND, wt.HWND, wt.HWND)
screen_to_client = native(USER, "ScreenToClient", wt.BOOL, wt.HWND, ct.POINTER(wt.POINT))
get_style = native(USER, "GetWindowLongW", wt.LONG, wt.HWND, ct.c_int)
set_style = native(USER, "SetWindowLongW", wt.LONG, wt.HWND, ct.c_int, wt.LONG)
get_rect = native(USER, "GetWindowRect", wt.BOOL, wt.HWND, ct.POINTER(wt.RECT))
set_position = native(USER, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND,
                      ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.UINT)
show_window = native(USER, "ShowWindow", wt.BOOL, wt.HWND, ct.c_int)
visible = native(USER, "IsWindowVisible", wt.BOOL, wt.HWND)
get_dpi = native(USER, "GetDpiForWindow", wt.UINT, wt.HWND)
is_window = native(USER, "IsWindow", wt.BOOL, wt.HWND)
invalidate_rect = native(USER, "InvalidateRect", wt.BOOL, wt.HWND, ct.c_void_p, wt.BOOL)
get_foreground = native(USER, "GetForegroundWindow", wt.HWND)
monitor_from_window = native(USER, "MonitorFromWindow", wt.HANDLE, wt.HWND, wt.DWORD)
is_zoomed = native(USER, "IsZoomed", wt.BOOL, wt.HWND)
dwm_set = native(DWM, "DwmSetWindowAttribute", ct.c_long, wt.HWND, wt.DWORD, ct.c_void_p, wt.DWORD)
dwm_extend = native(DWM, "DwmExtendFrameIntoClientArea", ct.c_long, wt.HWND, ct.c_void_p)
create_event = native(KERNEL, "CreateEventW", wt.HANDLE, ct.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR)
signal_event = native(KERNEL, "SetEvent", wt.BOOL, wt.HANDLE)
wait_event = native(KERNEL, "WaitForSingleObject", wt.DWORD, wt.HANDLE, wt.DWORD)
close_handle = native(KERNEL, "CloseHandle", wt.BOOL, wt.HANDLE)
wait_pipe = native(KERNEL, "WaitNamedPipeW", wt.BOOL, wt.LPCWSTR, wt.DWORD)
create_job = native(KERNEL, "CreateJobObjectW", wt.HANDLE, ct.c_void_p, wt.LPCWSTR)
set_job_info = native(KERNEL, "SetInformationJobObject", wt.BOOL,
                      wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD)
assign_job = native(KERNEL, "AssignProcessToJobObject", wt.BOOL, wt.HANDLE, wt.HANDLE)
open_process = native(KERNEL, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)


class JobLimits(ct.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION"""
    _fields_ = [
        ("per_process_time", ct.c_int64), ("per_job_time", ct.c_int64),
        ("flags", wt.DWORD), ("min_working_set", ct.c_size_t),
        ("max_working_set", ct.c_size_t), ("active_processes", wt.DWORD),
        ("affinity", ct.c_size_t), ("priority", wt.DWORD), ("scheduling", wt.DWORD),
        ("io", ct.c_uint64 * 6), ("process_memory", ct.c_size_t),
        ("job_memory", ct.c_size_t), ("peak_process_memory", ct.c_size_t),
        ("peak_job_memory", ct.c_size_t),
    ]


def kill_on_close_job(*processes):
    """Group processes so closing the handle, or this app dying, ends all of them."""
    job = create_job(None, None)
    if not job:
        return None
    limits = JobLimits(flags=0x2000)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if set_job_info(job, 9, ct.byref(limits), ct.sizeof(limits)):
        for process in processes:
            handle = open_process(0x0101, False, process.pid)  # SET_QUOTA | TERMINATE
            if not handle:
                break
            try:
                if not assign_job(job, handle):
                    break
            finally:
                close_handle(handle)
        else:
            return job
    close_handle(job)
    return None


def rect(hwnd):
    bounds = wt.RECT()
    if hwnd and get_rect(hwnd, ct.byref(bounds)):
        return bounds
    return None


def update_long(hwnd, index, add, remove):
    value = (get_style(hwnd, index) & 0xFFFFFFFF | add) & ~remove & 0xFFFFFFFF
    set_style(hwnd, index, value - (1 << 32) if value & 0x80000000 else value)


def light_theme(value="SystemUsesLightTheme"):
    try:
        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as settings:
            return bool(winreg.QueryValueEx(settings, value)[0])
    except OSError:
        return False


def system_accent(dark):
    """The Windows accent shade Fluent uses for this theme, or None if unavailable.
    AccentPalette holds eight RGBA colors: light 3..1, the accent itself, dark 1..3."""
    try:
        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as settings:
            data = winreg.QueryValueEx(settings, "AccentPalette")[0]
        index = 1 if dark else 4  # AccentLight2 on dark, AccentDark1 on light
        red, green, blue = data[index * 4:index * 4 + 3]
        return f"#{red:02x}{green:02x}{blue:02x}"
    except (OSError, ValueError, TypeError):
        return None


def theme_text(dark, follow_accent=True):
    """Foreground, secondary and accent colors for a light or dark surface."""
    fg, muted, accent = ("#ffffff", "#c5c5c5", "#60cdff") if dark else ("#1b1b1b", "#5c5c5c", "#005fb8")
    return fg, muted, (follow_accent and system_accent(dark)) or accent


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "BiliLiveAudio"
LAUNCHER = Path(__file__).resolve().with_name("Bili Live Audio.exe")


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as run:
            winreg.QueryValueEx(run, RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as run:
        if enabled:
            winreg.SetValueEx(run, RUN_NAME, 0, winreg.REG_SZ, f'"{LAUNCHER}"')
        else:
            try:
                winreg.DeleteValue(run, RUN_NAME)
            except FileNotFoundError:
                pass


OPTIONS = {"reconnect": True, "media_keys": True, "wheel_volume": True,
           "avatar": True, "system_accent": True}


def saved_rooms(items):
    rooms = {}
    for item in items if isinstance(items, list) else []:
        try:
            room, name = (item["room"], item.get("name", "")) if isinstance(item, dict) else (item, "")
            room = room_number(room)
            rooms.setdefault(room, {"room": room, "name": str(name or "")})
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
    return list(rooms.values())


def load_state():
    try:
        data = json.loads((STATE_DIR / "settings.json").read_text(encoding="utf-8"))
        saved = data.get("options")
        options = {key: bool(saved.get(key, value)) if isinstance(saved, dict) else value
                   for key, value in OPTIONS.items()}
        return (saved_rooms(data.get("rooms")), saved_rooms(data.get("favorites")),
                max(0, min(100, int(data.get("volume", 60)))), options)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return [], [], 60, dict(OPTIONS)


def web_get(url, timeout=5):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/"})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_face(uid):
    """Return (url, image bytes) for a streamer's avatar, cached for a week."""
    try:
        uid = int(uid)
    except (ValueError, TypeError, OverflowError):
        return None, None
    cache = STATE_DIR / "faces"
    image, link = cache / f"{uid}.img", cache / f"{uid}.url"
    try:
        if image.exists() and time.time() - image.stat().st_mtime < 7 * 86400:
            return link.read_text(encoding="utf-8"), image.read_bytes()
    except OSError:
        pass
    try:
        data = json.loads(web_get(f"https://api.live.bilibili.com/live_user/v1/Master/info?uid={uid}"))
        url = data["data"]["info"]["face"].replace("http://", "https://", 1)
        content = web_get(url, 10)
        cache.mkdir(exist_ok=True)
        image.write_bytes(content)
        link.write_text(url, encoding="utf-8")
        return url, content
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        logging.debug("Avatar unavailable for %s", uid, exc_info=True)
        return None, None


def room_info(rooms):
    query = "&".join(f"room_ids={room}" for room in rooms)
    url = ("https://api.live.bilibili.com/xlive/web-room/v1/index/getRoomBaseInfo"
           f"?{query}&req_biz=web_room_componet")
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0",
                                       "Referer": "https://live.bilibili.com/"})
        with urlopen(request, timeout=5) as response:
            entries = list(json.load(response)["data"]["by_room_ids"].items())
        info = {key: item for key, item in entries}
        if len(rooms) == len(entries) == 1:
            info[rooms[0]] = entries[0][1]
        for key, entry in entries:
            if entry.get("short_id"):
                info[str(entry["short_id"])] = entry
        return info
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        logging.debug("Room info unavailable", exc_info=True)
        return {}


class Playback:
    def __init__(self, room, volume):
        self.room = room
        self.pipe = r"\\.\pipe\BiliLiveAudio-" + uuid.uuid4().hex
        self.started = time.monotonic()
        self.ready = False
        self.info = {}
        self.stream = self.player = self.job = None
        self.logs = tempfile.TemporaryDirectory(prefix="playback-", dir=STATE_DIR,
                                                ignore_cleanup_errors=True)
        self.stream_log = Path(self.logs.name) / "stream.txt"
        self.player_log = Path(self.logs.name) / "player.txt"
        try:
            # Use the base interpreter so terminating the stream also closes its pipe.
            command = stream_command(room)
            command[0] = getattr(sys, "_base_executable", sys.executable)
            env = dict(os.environ, PYTHONPATH=sysconfig.get_path("purelib"))
            with self.stream_log.open("wb") as errors:
                self.stream = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=errors,
                    env=env, creationflags=subprocess.CREATE_NO_WINDOW,
                )
            try:
                self.player = subprocess.Popen(
                    player_command(f"--input-ipc-server={self.pipe}",
                                   f"--log-file={self.player_log}", f"--volume={volume}"),
                    stdin=self.stream.stdout, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                )
            finally:
                self.stream.stdout.close()
            self.job = kill_on_close_job(self.stream, self.player)
            if not self.job:
                raise OSError("Could not attach playback processes to the Windows job")
        except Exception:
            self.close()
            raise
        Thread(target=self.fetch_info, daemon=True).start()

    def fetch_info(self):
        self.info = room_info([self.room]).get(self.room, {})

    def close(self):
        """Stop playback without blocking the UI thread, which the taskbar shares."""
        if self.job:
            close_handle(self.job)
            self.job = None
        for process in (self.player, self.stream):
            if process is not None and process.poll() is None:
                process.kill()
        Thread(target=self.reap).start()

    def reap(self):
        for process in (self.player, self.stream):
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logging.warning("Process %s did not exit", process.pid)
        self.logs.cleanup()

    def volume(self, value):
        if self.player.poll() is None and wait_pipe(self.pipe, 1):
            try:
                command = {"command": ["set_property", "volume", value]}
                with open(self.pipe, "wb", buffering=0) as pipe:
                    payload = (json.dumps(command) + "\n").encode("ascii")
                    return pipe.write(payload) == len(payload)
            except OSError:
                logging.debug("mpv volume IPC unavailable", exc_info=True)
        return False

    def status(self):
        if not self.ready and self.player_log.exists():
            with self.player_log.open("rb") as player_log:
                self.ready = b"AO: [" in player_log.read(128 * 1024)
        if self.player.poll() is None:
            if self.ready or time.monotonic() - self.started < 35:
                return None
            return "连接超时，请重试"
        if self.ready:
            return "直播已结束或连接中断"
        errors = self.stream_log.read_text(encoding="utf-8", errors="replace")
        if "No playable streams" in errors or "No streams found" in errors:
            return "直播间未开播或没有可用直播流"
        logging.warning("Playback failed for %s: %s", self.room, errors[-500:])
        return "无法播放，请检查直播间和网络"


class MediaControls(QtCore.QObject):
    """Windows system media controls: media keys and the volume flyout's media card.
    A MediaPlayer with its command manager off gives a desktop app its own session
    without a window handle; mpv still plays the audio."""

    pressed = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self.player = self.controls = self.token = None
        self.shown = None

    def enable(self, enabled):
        if enabled and not self.player:
            try:
                from winrt.windows.media.playback import MediaPlayer
                from winrt.windows.media import MediaPlaybackType
                self.player = MediaPlayer()
                self.player.command_manager.is_enabled = False
                controls = self.player.system_media_transport_controls
                controls.is_play_enabled = controls.is_pause_enabled = controls.is_stop_enabled = True
                controls.display_updater.type = MediaPlaybackType.MUSIC
                controls.is_enabled = True
                self.token = controls.add_button_pressed(self.button_pressed)
                self.controls = controls
            except Exception:
                logging.exception("System media controls unavailable")
                self.player = self.controls = None
        elif not enabled and self.player:
            try:
                self.controls.remove_button_pressed(self.token)
                self.controls.is_enabled = False
                self.controls.display_updater.clear_all()
                self.player.close()
            except Exception:
                logging.debug("Closing system media controls", exc_info=True)
            self.player = self.controls = self.token = None
        self.shown = None
        return self.player is not None

    def button_pressed(self, sender, args):
        # Called on a WinRT worker thread; the queued signal hands it to the UI thread.
        self.pressed.emit({0: "play", 1: "pause", 2: "stop"}.get(int(args.button), ""))

    def show(self, state, title, artist, thumbnail):
        if not self.controls or self.shown == (state, title, artist, thumbnail):
            return
        self.shown = state, title, artist, thumbnail
        try:
            from winrt.windows.media import MediaPlaybackStatus
            from winrt.windows.foundation import Uri
            from winrt.windows.storage.streams import RandomAccessStreamReference
            updater = self.controls.display_updater
            updater.music_properties.title = title
            updater.music_properties.artist = artist
            updater.thumbnail = RandomAccessStreamReference.create_from_uri(Uri(thumbnail)) if thumbnail else None
            updater.update()
            self.controls.playback_status = {
                "loading": MediaPlaybackStatus.CHANGING, "playing": MediaPlaybackStatus.PLAYING,
            }.get(state, MediaPlaybackStatus.STOPPED)
        except Exception:
            logging.exception("Updating system media controls")


class Flyout(QtWidgets.QWidget):
    dismissed = QtCore.Signal()
    theme_changed = QtCore.Signal()

    def __init__(self, dark):
        # No FramelessWindowHint: Qt makes frameless translucent windows layered, and DWM
        # draws no system backdrop behind layered windows. nativeEvent hides the frame.
        super().__init__(None, QtCore.Qt.Tool | QtCore.Qt.WindowStaysOnTopHint)
        self.dark = dark
        self.acrylic = False
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setWindowTitle(APP_NAME)
        self.setMinimumHeight(0)

    def nativeEvent(self, kind, message):
        msg = wt.MSG.from_address(int(message))
        if msg.message == 0x0083 and msg.wParam:  # WM_NCCALCSIZE: the whole window is client
            return True, 0
        if msg.message == 0x0084:  # WM_NCHITTEST: no resize edges or caption
            return True, 1
        # WM_SETTINGCHANGE "ImmersiveColorSet": light/dark mode switched. Only top-level
        # windows get the broadcast, so the panel relays it for the taskbar chip as well.
        if msg.message == 0x001A and msg.lParam and ct.wstring_at(msg.lParam) == "ImmersiveColorSet":
            self.theme_changed.emit()
        return super().nativeEvent(kind, message)

    def apply_backdrop(self):
        hwnd = int(self.winId())
        update_long(hwnd, -16, 0, 0x000B0000)  # drop WS_SYSMENU and the caption buttons
        set_position(hwnd, None, 0, 0, 0, 0, 0x37)  # SWP_FRAMECHANGED, keep the rest

        def attribute(key, value):
            setting = ct.c_int(value)
            return dwm_set(hwnd, key, ct.byref(setting), ct.sizeof(setting)) == 0

        attribute(20, int(self.dark))  # DWMWA_USE_IMMERSIVE_DARK_MODE
        attribute(33, 2)  # DWMWA_WINDOW_CORNER_PREFERENCE: round
        margins = (ct.c_int * 4)(-1, -1, -1, -1)
        self.acrylic = bool(light_theme("EnableTransparency")
                            and dwm_extend(hwnd, ct.byref(margins)) == 0
                            and attribute(38, 3))  # DWMSBT_TRANSIENTWINDOW (acrylic)
        logging.info("DWM acrylic backdrop enabled: %s", self.acrylic)
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        # DWM draws the rounded corners, border and shadow. Over acrylic only add a light
        # tint; without it fall back to the solid Windows 11 flyout color.
        if self.acrylic:
            # Enough tint to keep text readable over dark or busy windows behind.
            fill = QtGui.QColor(36, 36, 36, 150) if self.dark else QtGui.QColor(250, 250, 250, 175)
        else:
            fill = QtGui.QColor("#2c2c2c" if self.dark else "#f9f9f9")
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(), fill)

    def event(self, event):
        if event.type() == QtCore.QEvent.WindowDeactivate:
            self.dismissed.emit()
        return super().event(event)


class ElidedLabel(QtWidgets.QLabel):
    """Elides to the width it is given and never asks for more, so long room names,
    titles or areas cannot widen the panel past its edge."""

    def __init__(self, text="", muted=False):
        super().__init__()
        self.setTextFormat(QtCore.Qt.PlainText)
        self.setProperty("muted", muted)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.full_text = ""
        self.set_full_text(text)

    def set_full_text(self, text):
        self.full_text = text
        self.refresh()

    def refresh(self):
        shown = self.fontMetrics().elidedText(self.full_text, QtCore.Qt.ElideRight, max(0, self.width()))
        super().setText(shown)
        self.setToolTip(self.full_text if shown != self.full_text else "")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()

    def minimumSizeHint(self):
        return QtCore.QSize(0, super().minimumSizeHint().height())


ROW_TEXT = QtCore.Qt.UserRole + 1  # (name, detail) shown on a room row


def room_item(room, info, name, detail):
    item = QtWidgets.QListWidgetItem(f"{name} · {detail}")
    item.setData(QtCore.Qt.UserRole, (room, info))
    item.setData(ROW_TEXT, (name, detail))
    item.setToolTip(f"{name}\n{detail}")
    return item


class RoomDelegate(QtWidgets.QStyledItemDelegate):
    """A Fluent list row: avatar initial, name over a muted detail line."""

    def __init__(self, view):
        super().__init__(view)
        self.colors = {}  # set by Radio.apply_theme

    def sizeHint(self, option, index):
        return QtCore.QSize(0, 46)

    def paint(self, painter, option, index):
        c = self.colors
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        bounds = option.rect.adjusted(0, 1, -2, -1)
        active = bool(option.state & QtWidgets.QStyle.State_Selected)
        hovered = bool(option.state & QtWidgets.QStyle.State_MouseOver)
        painter.setPen(QtCore.Qt.NoPen)
        if active or hovered:
            painter.setBrush(c["selected"] if active else c["hover"])
            painter.drawRoundedRect(bounds, 5, 5)
        if active:
            painter.setBrush(c["accent"])
            painter.drawRoundedRect(QtCore.QRectF(bounds.left(), bounds.center().y() - 8, 3, 16), 1.5, 1.5)
        name, detail = index.data(ROW_TEXT) or (index.data(), "")
        left = bounds.left() + 14
        width = bounds.right() - left - 8
        for text, top, size, color in ((name, -18, 13, "fg"), (detail, 1, 11, "muted")):
            painter.setFont(app_font(size))
            painter.setPen(c[color])
            painter.drawText(QtCore.QRect(left, bounds.center().y() + top, width, 17),
                             QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                             painter.fontMetrics().elidedText(text, QtCore.Qt.ElideRight, width))
        if option.state & QtWidgets.QStyle.State_HasFocus and option.state & QtWidgets.QStyle.State_KeyboardFocusChange:
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.setPen(c["accent"])
            painter.drawRoundedRect(bounds.adjusted(0, 0, -1, -1), 5, 5)
        painter.restore()


class SelectorBar(QtWidgets.QTabBar):
    """Windows 11 SelectorBar with icon tabs and a short accent pill under the selected one."""

    def __init__(self):
        super().__init__()
        self.colors = {}  # set by Radio.apply_theme
        self.active = True  # False while a page outside the tabs (settings) is shown
        self.setDrawBase(False)
        self.setExpanding(False)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.hovered = -1

    def add(self, title, glyph):
        index = self.addTab(title)  # the text stays for screen readers
        self.setTabData(index, GLYPHS[glyph])
        self.setTabToolTip(index, title)

    def tabSizeHint(self, index):
        return QtCore.QSize(48, 36)

    def mouseMoveEvent(self, event):
        hovered = self.tabAt(event.position().toPoint())
        if hovered != self.hovered:
            self.hovered = hovered
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hovered = -1
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        c = self.colors
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        for index in range(self.count()):
            bounds = self.tabRect(index)
            selected = index == self.currentIndex() and self.active
            if index == self.hovered and not selected:
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(c["hover"])
                p.drawRoundedRect(QtCore.QRectF(bounds).adjusted(2, 3, -2, -3), 5, 5)
            p.setFont(icon_font(16))
            p.setPen(c["accent"] if selected else c["fg"] if index == self.hovered else c["muted"])
            p.drawText(bounds.adjusted(0, 0, 0, -3), QtCore.Qt.AlignCenter, self.tabData(index))
            if selected:
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(c["accent"])
                p.drawRoundedRect(QtCore.QRectF(bounds.center().x() - 8, bounds.bottom() - 3, 16, 3), 1.5, 1.5)
            if self.hasFocus() and selected:
                p.setBrush(QtCore.Qt.NoBrush)
                p.setPen(c["accent"])
                p.drawRoundedRect(QtCore.QRectF(bounds).adjusted(1, 1, -1, -1), 5, 5)


class VolumeSlider(QtWidgets.QSlider):
    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        width = self.width() - 20
        y = self.height() / 2
        x = 10 + width * self.value() / 100
        accent = self.palette().highlight().color()
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(self.palette().mid().color())
        painter.drawRoundedRect(QtCore.QRectF(10, y - 2, width, 4), 2, 2)
        painter.setBrush(accent)
        painter.drawRoundedRect(QtCore.QRectF(10, y - 2, x - 10, 4), 2, 2)
        painter.setPen(accent if self.hasFocus() else self.palette().mid().color())
        painter.setBrush(self.palette().button().color())
        painter.drawEllipse(QtCore.QPointF(x, y), 9.5, 9.5)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(QtCore.QPointF(x, y), 6, 6)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.setFocus()
            self.mouseMoveEvent(event)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & QtCore.Qt.LeftButton:
            self.setValue(round(100 * (event.position().x() - 10) / max(1, self.width() - 20)))


class ToggleSwitch(QtWidgets.QAbstractButton):
    """Windows 11 ToggleSwitch: an outlined pill when off, an accent pill when on."""

    def __init__(self):
        super().__init__()
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.TabFocus)
        self.setFixedSize(40, 20)
        self.colors = {}  # set by Radio.apply_theme

    def paintEvent(self, event):
        c = self.colors
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        track = QtCore.QRectF(0.5, 0.5, 39, 19)
        if self.isChecked():
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(c["accent"])
        else:
            p.setPen(QtGui.QPen(c["muted"], 1))
            p.setBrush(QtCore.Qt.NoBrush)
        p.drawRoundedRect(track, 9.5, 9.5)
        radius = 6 if self.underMouse() else 5
        x = 30 if self.isChecked() else 10
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(c["on_accent"] if self.isChecked() else c["muted"])
        p.drawEllipse(QtCore.QPointF(x, 10), radius, radius)
        if self.hasFocus():
            p.setBrush(QtCore.Qt.NoBrush)
            p.setPen(QtGui.QPen(c["fg"], 1))
            p.drawRoundedRect(QtCore.QRectF(0.5, 0.5, 39, 19), 9.5, 9.5)

    def enterEvent(self, event):
        self.update()

    def leaveEvent(self, event):
        self.update()


class LiveBadge(QtWidgets.QWidget):
    """The red "live" pill. Painted so the dot is centered geometrically; a text
    bullet sits on the font baseline and looks low next to CJK glyphs."""

    def __init__(self, text):
        super().__init__()
        self.text = text
        self.colors = {}  # set by Radio.apply_theme
        self.setFont(app_font(11, True))
        self.setFixedSize(18 + self.fontMetrics().horizontalAdvance(text) + 9, 20)  # dot area, text, end padding
        self.setAccessibleName(text)

    def paintEvent(self, event):
        c = self.colors
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(c["background"])
        p.drawRoundedRect(QtCore.QRectF(self.rect()), 10, 10)
        p.setBrush(c["dot"])
        p.drawEllipse(QtCore.QPointF(11, self.height() / 2), 3, 3)
        p.setPen(c["text"])
        p.drawText(self.rect().adjusted(18, 0, 0, 0), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, self.text)


class SettingRow(QtWidgets.QWidget):
    """A settings line: title and hint on the left, a switch on the right.
    Clicking anywhere on the row flips the switch."""

    def __init__(self, title, hint):
        super().__init__()
        self.setCursor(QtCore.Qt.PointingHandCursor)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(12)
        texts = QtWidgets.QVBoxLayout()
        texts.setSpacing(1)
        name = QtWidgets.QLabel(title)
        name.setFont(app_font(13))
        texts.addWidget(name)
        detail = ElidedLabel(hint, muted=True)
        detail.setFont(app_font(11))
        texts.addWidget(detail)
        layout.addLayout(texts, 1)
        self.switch = ToggleSwitch()
        self.switch.setAccessibleName(title)
        layout.addWidget(self.switch, 0, QtCore.Qt.AlignVCenter)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.switch.click()


class Chip(QtWidgets.QWidget):
    def __init__(self, radio):
        super().__init__(None, QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint |
                         QtCore.Qt.WindowDoesNotAcceptFocus)
        self.radio = radio
        self.dark = not light_theme()
        self.setWindowTitle(APP_NAME + " · 小组件")
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setFixedSize(232, 40)
        self.hover_button = False
        self.phase = 0
        self.animation = QtCore.QTimer(self, interval=110)
        self.animation.timeout.connect(self.animate)
        self.volume_hint = False  # briefly show the volume after a wheel scroll
        self.hint_timer = QtCore.QTimer(self, interval=1200, singleShot=True)
        self.hint_timer.timeout.connect(self.end_volume_hint)

    def end_volume_hint(self):
        self.volume_hint = False
        self.redraw()

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120
        if not self.radio.options["wheel_volume"] or not steps:
            return
        self.radio.slider.setValue(max(0, min(100, self.radio.volume + round(steps * 5))))
        self.volume_hint = True
        self.hint_timer.start()
        self.redraw()

    def redraw(self):
        """Paint now. Embedded in Explorer's taskbar, the widget never gets Qt's expose
        and deferred update requests, so update() would leave it stuck on its first frame."""
        handle = self.windowHandle()
        if handle and not handle.isExposed():
            invalidate_rect(int(self.winId()), None, False)
        self.repaint()

    def animate(self):
        self.phase = (self.phase + 1) % 12
        self.redraw()

    def refresh(self):
        active = self.radio.state in ("loading", "playing") and self.isVisible()
        if active and not self.animation.isActive():
            self.animation.start()
        elif not active:
            self.animation.stop()
        self.redraw()

    def mouseMoveEvent(self, event):
        self.hover_button = event.position().x() >= 198
        self.redraw()

    def enterEvent(self, event):
        self.redraw()

    def leaveEvent(self, event):
        self.hover_button = False
        self.redraw()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            if event.position().x() >= 198:
                self.radio.toggle_playback()
            else:
                self.radio.toggle_panel()

    def paintEvent(self, event):
        radio = self.radio
        state = radio.state
        info = radio.details()
        dark = self.dark
        fg, muted, accent = theme_text(dark, radio.options["system_accent"])
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(QtCore.Qt.NoPen)
        # A single alpha step keeps the entire layered hit area interactive.
        p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 1))
        if self.underMouse() or radio.panel.isVisible():
            p.setBrush(QtGui.QColor(255, 255, 255, 20 if dark else 158))
            p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 6, 6)
        if self.hover_button:
            p.setBrush(QtGui.QColor(255, 255, 255, 22) if dark else QtGui.QColor(0, 0, 0, 16))
            p.drawRoundedRect(198, 6, 28, 28, 5, 5)

        # The left slot shows playback state: sound bars (moving while playing, flat and
        # grey when idle), a spinner while connecting, a warning on errors.
        slot = QtCore.QRect(10, 10, 20, 20)
        if state == "loading":
            p.setBrush(QtCore.Qt.NoBrush)
            p.setPen(QtGui.QPen(QtGui.QColor(accent), 2))
            p.drawArc(slot.adjusted(3, 3, -3, -3), self.phase * 480, 245 * 16)
        elif state == "error":
            p.setPen(QtGui.QColor("#fce100" if dark else "#9d5d00"))
            p.setFont(icon_font(15))
            p.drawText(slot, QtCore.Qt.AlignCenter, GLYPHS["warning"])
        else:
            playing = state == "playing"
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QColor(accent if playing else muted))
            for i in range(3):
                height = (6, 10, 14, 10)[(self.phase + i) % 4] if playing else (6, 9, 6)[i]
                p.drawRoundedRect(QtCore.QRectF(slot.left() + 4 + i * 5, 20 - height / 2, 3, height), 1.5, 1.5)

        name = info.get("uname") or f"房间 {radio.room}"
        detail = {
            "idle": "未播放",
            "loading": radio.connecting_text(),
            "playing": info.get("title") or "播放中",
            "error": radio.error or "播放失败",
        }[state]
        if self.volume_hint:
            detail = f"音量 {radio.volume}"
        action = GLYPHS[{"idle": "play", "loading": "stop", "playing": "pause", "error": "refresh"}[state]]
        width = 158
        for text, y, size, color in ((name, 4, 12, fg), (detail, 20, 11, muted)):
            p.setFont(app_font(size, y == 4))
            p.setPen(QtGui.QColor(color))
            p.drawText(QtCore.QRect(36, y, width, 16), QtCore.Qt.AlignVCenter,
                       p.fontMetrics().elidedText(text, QtCore.Qt.ElideRight, width))
        p.setFont(icon_font(14))
        p.setPen(QtGui.QColor(fg))
        p.drawText(QtCore.QRect(198, 6, 28, 28), QtCore.Qt.AlignCenter, action)


def app_font(size, bold=False):
    font = QtGui.QFont()
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI"])
    font.setPixelSize(size)
    font.setBold(bold)
    return font


# Segoe Fluent Icons (Windows 11), with the Windows 10 MDL2 font as fallback.
GLYPHS = {
    "play": "", "pause": "", "stop": "", "refresh": "",
    "star": "", "starred": "", "go": "", "hide": "",
    "power": "", "more": "", "volume": "", "mute": "",
    "music": "", "open": "", "people": "", "history": "",
    "warning": "", "settings": "",
}


def icon_font(size):
    font = QtGui.QFont()
    font.setFamilies(["Segoe Fluent Icons", "Segoe MDL2 Assets"])
    font.setPixelSize(size)
    return font


def glyph_icon(name, color, size=16):
    pixmap = QtGui.QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setFont(icon_font(size - 4))
    painter.setPen(QtGui.QColor(color))
    painter.drawText(QtCore.QRect(0, 0, size, size), QtCore.Qt.AlignCenter, GLYPHS[name])
    painter.end()
    return QtGui.QIcon(pixmap)


def initial(name):
    return (name or "").strip()[:1].upper()


class Radio(QtCore.QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.rooms, self.favorites, self.volume, self.options = load_state()
        self.notice = ""  # footer message, e.g. a setting that could not be applied
        self.retries = 0  # reconnect attempts since playback last dropped
        self.retry_at = 0
        self.faces = {}  # uid -> (url, QPixmap or None)
        self.face_task = None
        self.media = MediaControls()
        self.media.pressed.connect(self.media_button)
        self.media.enable(self.options["media_keys"])
        self.room = self.rooms[0]["room"] if self.rooms else DEFAULT_ROOM
        self.info = {item["room"]: {"uname": item["name"]}
                     for item in self.favorites + self.rooms if item["name"]}
        self.room_menu_open = False
        self.placing = False
        self.state, self.error = "idle", ""
        self.bar = self.anchor = None
        self.dismissed_at = 0
        self.fallback_opened = False
        self.session = None
        self.request = 0
        self.starting = None
        self.polling = None
        self.recent_info = None
        self.dock_at = 0
        self.closing = False
        self.player_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="player")
        self.network_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="room")
        self.account_path = STATE_DIR / "account.dpapi"
        try:
            self.cookies = account.load_session(self.account_path)
        except (OSError, ValueError):
            self.cookies = {}
        self.account_task = None
        self.qr_key = None
        self.qr_deadline = 0
        self.next_poll = 0
        self.save_timer = QtCore.QTimer(self, interval=300, singleShot=True)
        self.save_timer.timeout.connect(self.save)
        self.panel = Flyout(not light_theme("AppsUseLightTheme"))
        self.panel.dismissed.connect(self.dismiss_panel)
        # The registry may lag the broadcast slightly, so read it a moment later.
        self.panel.theme_changed.connect(lambda: QtCore.QTimer.singleShot(300, self.follow_theme))
        self.launched.connect(self.accept_launch, QtCore.Qt.QueuedConnection)
        self.build_panel()
        self.chip = Chip(self)
        self.chip.winId()
        self.timer = QtCore.QTimer(self, interval=120)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.dock()

    def follow_theme(self):
        """Light/dark mode or the accent color changed."""
        panel_dark = not light_theme("AppsUseLightTheme")
        if panel_dark != self.panel.dark:
            self.panel.dark = panel_dark
            self.panel.apply_backdrop()  # DWM dark-mode frame and tint
        self.chip.dark = not light_theme()
        self.apply_theme()
        self.update_display()  # also repaints the chip; the favorite star carries the accent

    def apply_theme(self):
        dark = self.panel.dark
        fg, muted, accent = theme_text(dark, self.options["system_accent"])
        on_accent = "#000000" if dark else "#ffffff"
        # Fluent's hovered accent fill is the accent at 90% opacity.
        accent_hover = "rgba({}, {}, {}, 230)".format(*QtGui.QColor(accent).getRgb()[:3])
        edge = "rgba(255,255,255,20)" if dark else "rgba(0,0,0,16)"
        card = "rgba(255,255,255,13)" if dark else "rgba(255,255,255,150)"
        field = "rgba(255,255,255,15)" if dark else "rgba(255,255,255,200)"
        hover = "rgba(255,255,255,15)" if dark else "rgba(0,0,0,10)"
        pressed = "rgba(255,255,255,8)" if dark else "rgba(0,0,0,6)"
        thumb = "rgba(255,255,255,90)" if dark else "rgba(0,0,0,70)"
        menu = "#2c2c2c" if dark else "#f9f9f9"
        self.accent = accent
        row_colors = {
            "fg": fg, "muted": muted, "accent": accent,
            "hover": "#1affffff" if dark else "#0d000000",
            "selected": "#24ffffff" if dark else "#14000000",
            "avatar": "#1f3a5a" if dark else "#dbe8f8",
            "avatar_text": "#99d4ff" if dark else "#005fb8",
        }
        self.panel.setStyleSheet(f"""
            QLabel, QLineEdit, QPushButton {{color:{fg};}}
            QLabel[muted="true"] {{color:{muted};}}
            QFrame#now {{background:{card}; border:1px solid {edge}; border-radius:8px;}}
            QFrame#divider {{background:{edge}; border:none;}}
            QPushButton {{background:transparent; border:1px solid transparent; border-radius:5px; padding:0 8px;}}
            QPushButton:hover {{background:{hover};}}
            QPushButton:pressed {{background:{pressed}; color:{muted};}}
            QPushButton:focus {{border-color:{accent};}}
            QPushButton:disabled {{color:{muted};}}
            QPushButton[glyph="true"] {{padding:0;}}
            QPushButton#transport {{background:{accent}; color:{on_accent}; border-radius:20px;}}
            QPushButton#transport:hover {{background:{accent_hover};}}
            QPushButton#stop {{background:{hover}; border:1px solid {edge}; border-radius:16px;}}
            QPushButton#stop:hover {{background:{field};}}
            QPushButton#link {{color:{accent}; padding:0 6px;}}
            QPushButton#login {{background:{accent}; color:{on_accent}; padding:0 16px;}}
            QPushButton#login:hover {{background:{accent_hover};}}
            QLineEdit {{background:{field}; border:1px solid {edge}; border-radius:5px;
                       border-bottom:1px solid {muted}; padding:0 6px; selection-background-color:{accent};
                       selection-color:{on_accent};}}
            QLineEdit:focus {{border-bottom:2px solid {accent};}}
            QLabel#avatar {{background:{row_colors["avatar"]}; color:{row_colors["avatar_text"]}; border-radius:8px;}}
            QListWidget {{background:transparent; border:none; outline:none;}}
            QScrollBar:vertical {{background:transparent; width:8px; margin:2px 1px;}}
            QScrollBar::handle:vertical {{background:{thumb}; border-radius:3px; min-height:24px;}}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{height:0;}}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{background:transparent;}}
            QMenu {{background:{menu}; color:{fg}; border:1px solid {edge}; border-radius:8px; padding:4px;}}
            QMenu::item {{padding:7px 28px 7px 12px; border-radius:4px;}}
            QMenu::item:selected {{background:{hover};}}
        """)
        self.logo.setStyleSheet(f"color:{accent};")
        palette = self.slider.palette()
        palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(accent))
        palette.setColor(QtGui.QPalette.Mid, QtGui.QColor("#8b8b8b" if dark else "#8a8a8a"))
        palette.setColor(QtGui.QPalette.Button, QtGui.QColor("#454545" if dark else "#ffffff"))
        self.slider.setPalette(palette)
        self.tabs.colors = {key: QtGui.QColor(row_colors[key]) for key in ("fg", "muted", "accent", "hover")}
        for view in (self.followed, *self.room_views):
            view.itemDelegate().colors = {key: QtGui.QColor(value) for key, value in row_colors.items()}
            view.viewport().update()
        switch_colors = {"fg": QtGui.QColor(fg), "muted": QtGui.QColor(muted),
                         "accent": QtGui.QColor(accent), "on_accent": QtGui.QColor(on_accent)}
        for row in self.setting_rows.values():
            row.switch.colors = switch_colors
            row.switch.update()
        self.live.colors = {key: QtGui.QColor(value) for key, value in zip(
            ("background", "text", "dot"),
            ("#4a2427", "#ffb3ba", "#ff99a4") if dark else ("#fde7e9", "#a4262c", "#c42b1c"))}
        self.live.update()
        if self.pages.currentWidget() is self.settings_page:
            self.settings_button.setStyleSheet(f"color:{accent};")
        self.go_action.setIcon(glyph_icon("go", fg))
        self.browser.setIcon(glyph_icon("open", accent, 14))
        self.tabs.update()
        self.panel.update()

    def build_panel(self):
        self.follow_height = None
        self.panel.setFixedWidth(368)
        self.panel.setFont(app_font(12))
        outer = QtWidgets.QVBoxLayout(self.panel)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea, QWidget#flyoutContent {background:transparent;}")
        scroll.viewport().setAutoFillBackground(False)
        content = QtWidgets.QWidget(objectName="flyoutContent")
        scroll.setWidget(content)
        outer.addWidget(scroll)
        self.content = content
        # Resize the panel whenever its content relayouts (lists filled, QR code shown...);
        # otherwise the outer scroll bar appears and squeezes the content from the right.
        content.installEventFilter(self)
        main = QtWidgets.QVBoxLayout(content)
        main.setContentsMargins(16, 12, 16, 14)
        main.setSpacing(12)

        def label(text="", muted=False):
            result = QtWidgets.QLabel(text)
            result.setTextFormat(QtCore.Qt.PlainText)
            result.setProperty("muted", muted)
            return result

        def button(text, action, name="", size=None, tip=None):
            result = QtWidgets.QPushButton(text, objectName=name)
            result.setCursor(QtCore.Qt.PointingHandCursor)
            result.setFocusPolicy(QtCore.Qt.TabFocus)  # focus ring for keyboard use only
            if size:
                result.setFixedSize(*size)
            if tip:
                result.setToolTip(tip)
                result.setAccessibleName(tip)
            result.clicked.connect(action)
            return result

        def icon_button(glyph, action, tip, size=32, glyph_size=16, name=""):
            result = button(GLYPHS[glyph], action, name, (size, size), tip)
            result.setFont(icon_font(glyph_size))
            result.setProperty("glyph", True)  # not "icon": QPushButton already has that property
            return result

        def divider():
            line = QtWidgets.QFrame(objectName="divider")
            line.setFixedHeight(1)
            return line

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)
        self.logo = logo = label(GLYPHS["music"])
        logo.setFont(icon_font(14))
        header.addWidget(logo)
        header.addWidget(label(APP_NAME, True))
        header.addStretch()
        header.addWidget(icon_button("hide", self.panel.hide, "收起", glyph_size=12))
        header.addWidget(icon_button("power", self.app.quit, "退出", glyph_size=14))
        main.addLayout(header)

        now = QtWidgets.QFrame(objectName="now")
        body = QtWidgets.QVBoxLayout(now)
        body.setContentsMargins(14, 14, 14, 12)
        body.setSpacing(12)
        summary = QtWidgets.QHBoxLayout()
        summary.setSpacing(12)
        self.avatar = label()
        self.avatar.setObjectName("avatar")
        self.avatar.setFixedSize(56, 56)
        self.avatar.setAlignment(QtCore.Qt.AlignCenter)
        summary.addWidget(self.avatar, 0, QtCore.Qt.AlignVCenter)
        texts = QtWidgets.QVBoxLayout()
        texts.setSpacing(0)
        category = QtWidgets.QHBoxLayout()
        category.setSpacing(8)
        self.live = LiveBadge("直播中")
        self.area = ElidedLabel(muted=True)
        self.area.setFont(app_font(11))
        category.addWidget(self.live)
        category.addWidget(self.area, 1)
        texts.addLayout(category)
        texts.addSpacing(6)
        self.title = ElidedLabel()
        self.title.setFont(app_font(14, True))
        self.byline = ElidedLabel(muted=True)
        texts.addWidget(self.title)
        texts.addSpacing(3)
        texts.addWidget(self.byline)
        summary.addLayout(texts, 1)
        # The star sits in its own column so it does not stretch the badge row.
        self.favorite_button = icon_button("star", self.favorite_current, "收藏当前房间", 28, 14)
        summary.addWidget(self.favorite_button, 0, QtCore.Qt.AlignTop)
        body.addLayout(summary)
        body.addSpacing(4)
        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(8)
        self.transport = icon_button("play", self.toggle_playback, "播放", 40, 16, "transport")
        self.stop_control = icon_button("stop", self.stop, "停止", 32, 12, "stop")
        controls.addWidget(self.transport)
        controls.addWidget(self.stop_control)
        controls.addSpacing(6)
        self.speaker = label(GLYPHS["mute" if self.volume == 0 else "volume"])
        self.speaker.setFont(icon_font(16))
        controls.addWidget(self.speaker)
        self.slider = slider = VolumeSlider(QtCore.Qt.Horizontal)
        slider.setRange(0, 100)
        slider.setFixedHeight(28)
        slider.setAccessibleName("音量")
        slider.setValue(self.volume)
        slider.valueChanged.connect(self.change_volume)
        controls.addWidget(slider, 1)
        self.volume_label = label(f"{self.volume}", True)
        self.volume_label.setFixedWidth(26)
        self.volume_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        controls.addWidget(self.volume_label)
        body.addLayout(controls)
        main.addWidget(now)

        self.tabs = tabs = SelectorBar()
        pages = QtWidgets.QStackedWidget()
        follow_page = QtWidgets.QWidget()
        follow_layout = QtWidgets.QVBoxLayout(follow_page)
        follow_layout.setContentsMargins(0, 0, 0, 0)
        follow_layout.setSpacing(6)
        tabs.add("关注", "people")
        pages.addWidget(follow_page)
        self.room_views = []
        self.saved_counts = []
        for title, glyph, hint in (("收藏", "star", "暂无收藏"), ("历史", "history", "暂无记录")):
            tabs.add(title, glyph)
            page = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)
            count = label(hint, True)
            count.setFixedHeight(24)
            layout.addWidget(count)
            view = QtWidgets.QListWidget()
            view.setAccessibleName(title)
            layout.addWidget(view)
            layout.addStretch()
            pages.addWidget(page)
            self.room_views.append(view)
            self.saved_counts.append((count, hint))
        settings_page = QtWidgets.QWidget()
        settings_layout = QtWidgets.QVBoxLayout(settings_page)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(2)
        self.setting_rows = {}
        for key, title, hint in (
            ("autostart", "开机自动启动", "登录 Windows 后在任务栏显示"),
            ("reconnect", "断线自动重连", "掉线后最多重试 5 次"),
            ("media_keys", "媒体键控制", "键盘媒体键和系统音量浮窗"),
            ("wheel_volume", "滚轮调音量", "在任务栏小组件上滚动"),
            ("avatar", "显示主播头像", "正在播放卡片上的头像"),
            ("system_accent", "跟随系统强调色", "关闭时使用默认蓝色"),
        ):
            row = SettingRow(title, hint)
            row.switch.setChecked(autostart_enabled() if key == "autostart" else self.options[key])
            row.switch.toggled.connect(lambda checked, option=key: self.set_option(option, checked))
            settings_layout.addWidget(row)
            self.setting_rows[key] = row
        settings_layout.addStretch()
        pages.addWidget(settings_page)
        self.pages = pages
        self.settings_page = settings_page
        tabs.currentChanged.connect(self.switch_page)
        tabs.tabBarClicked.connect(self.switch_page)  # also leaves settings via the current tab
        main.addWidget(tabs)
        main.addWidget(pages)

        followed_header = QtWidgets.QHBoxLayout()
        followed_header.setSpacing(4)
        self.account_status = label(muted=True)
        self.account_status.setWordWrap(True)
        self.account_status.setFixedHeight(32)
        followed_header.addWidget(self.account_status, 1)
        self.account_refresh = icon_button("refresh", self.refresh_followed, "刷新", 28, 13)
        followed_header.addWidget(self.account_refresh)
        self.account_more = icon_button("more", self.account_menu, "更多", 28, 14)
        followed_header.addWidget(self.account_more)
        follow_layout.addLayout(followed_header)
        self.account_action = button("", self.toggle_account, "login")
        self.account_action.setFixedHeight(32)
        follow_layout.addWidget(self.account_action, 0, QtCore.Qt.AlignLeft)
        self.account_qr = label()
        self.account_qr.setAlignment(QtCore.Qt.AlignCenter)
        self.account_qr.hide()
        follow_layout.addWidget(self.account_qr)
        self.followed = QtWidgets.QListWidget()
        for view in [self.followed, *self.room_views]:
            view.setItemDelegate(RoomDelegate(view))
            view.setMouseTracking(True)
            view.setUniformItemSizes(True)
            view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
            view.itemClicked.connect(self.play_list_room)
            view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda point, target=view: self.room_menu(target, point))
        self.followed.setAccessibleName("正在开播的关注")
        follow_layout.addWidget(self.followed)
        follow_layout.addStretch()
        self.update_account_controls()
        self.account_status.setText("点刷新查看" if self.cookies else "登录后查看关注开播")

        self.input = QtWidgets.QLineEdit(placeholderText="房间号或直播链接")
        self.input.setAccessibleName("切换直播间")
        self.input.setFixedHeight(34)
        self.go_action = go = self.input.addAction(QtGui.QIcon(), QtWidgets.QLineEdit.TrailingPosition)
        go.setToolTip("播放这个房间")
        go.triggered.connect(self.play_input)
        self.input.returnPressed.connect(self.play_input)
        main.addWidget(self.input)

        main.addWidget(divider())
        footer = QtWidgets.QHBoxLayout()
        self.browser = browser = button("网页打开", self.open_room, "link")
        browser.setLayoutDirection(QtCore.Qt.RightToLeft)
        browser.setFixedHeight(28)
        footer.addWidget(browser)
        # Errors and notices only; the playback state is already on the card and the chip.
        self.status = ElidedLabel(muted=True)
        self.status.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        footer.addWidget(self.status, 1)
        self.settings_button = icon_button("settings", self.toggle_settings, "设置", 28, 15)
        footer.addWidget(self.settings_button)
        main.addLayout(footer)
        escape = QtGui.QShortcut(QtGui.QKeySequence("Escape"), self.panel)
        escape.activated.connect(self.panel.hide)
        self.apply_theme()
        self.update_display()

    def account_menu(self):
        menu = QtWidgets.QMenu(self.panel)
        sign_out = menu.addAction("退出登录")
        self.room_menu_open = True
        try:
            point = self.account_more.mapToGlobal(QtCore.QPoint(0, self.account_more.height()))
            if menu.exec(point) == sign_out and self.cookies:
                self.toggle_account()
        finally:
            self.room_menu_open = False

    def update_account_controls(self):
        busy = self.account_task is not None
        self.account_refresh.setVisible(bool(self.cookies))
        self.account_refresh.setEnabled(not busy)
        self.account_more.setVisible(bool(self.cookies))
        self.account_action.setVisible(not self.cookies)
        self.account_action.setText("取消登录" if self.qr_key else "扫码登录")
        self.account_action.setEnabled(not busy or bool(self.qr_key))
        self.fit_lists()

    def fit_lists(self):
        """Keep the signed-in follow page stable while its list refreshes."""
        for view in [self.followed, *self.room_views]:
            rows = view.count()
            if view is self.followed:
                view.setFixedHeight(4 * 46)
                view.setVisible(bool(self.cookies))
            else:
                view.setFixedHeight(min(rows, 4) * 46)
                view.setVisible(rows > 0)
        current = self.pages.currentIndex()
        for index in range(self.pages.count()):
            # QStackedWidget sizes to its largest page unless the others are Ignored.
            policy = (QtWidgets.QSizePolicy.Preferred if index == current
                      else QtWidgets.QSizePolicy.Ignored)
            self.pages.widget(index).setSizePolicy(policy, policy)
            self.pages.widget(index).updateGeometry()
        self.pages.updateGeometry()
        self.content.layout().invalidate()
        self.content.layout().activate()
        if self.panel.isVisible():
            # Measure after Qt has processed the size changes posted above.
            QtCore.QTimer.singleShot(0, self.place_panel)

    def switch_page(self, index):
        if index < 0:
            return
        self.tabs.active = True
        self.tabs.update()
        self.pages.setCurrentIndex(index)
        self.follow_height = None
        self.settings_button.setStyleSheet("")
        self.fit_lists()

    def toggle_settings(self):
        if self.pages.currentWidget() is self.settings_page:
            self.switch_page(self.tabs.currentIndex())
            return
        self.tabs.active = False  # no tab is selected while settings are open
        self.tabs.update()
        self.pages.setCurrentWidget(self.settings_page)
        self.settings_button.setStyleSheet(f"color:{self.accent};")
        self.fit_lists()

    def notify(self, message):
        self.notice = message
        self.elide(self.status, message)

    def set_option(self, key, value):
        if key == "autostart":
            try:
                set_autostart(value)
            except OSError:
                logging.exception("Changing autostart")
                switch = self.setting_rows[key].switch
                switch.blockSignals(True)
                switch.setChecked(not value)
                switch.blockSignals(False)
                self.notify("无法修改开机启动")
            return
        self.options[key] = value
        self.save()
        if key == "media_keys" and not self.media.enable(value) and value:
            self.notify("系统媒体控制不可用")
        elif key == "system_accent":
            self.apply_theme()
        elif key == "reconnect" and not value and self.retry_at:
            self.stop()
        self.update_display()

    @QtCore.Slot(str)
    def media_button(self, button):
        active = self.state in ("loading", "playing")
        if button == "play" and not active:
            self.start(self.room)
        elif button in ("pause", "stop") and active:
            self.stop()  # a live stream cannot pause; stopping also frees the bandwidth

    def connecting_text(self):
        return f"重新连接（{self.retries}/5）…" if self.retries else "连接中…"

    def account_request(self, action, function, *args):
        self.account_task = action, self.network_pool.submit(function, *args)
        self.update_account_controls()

    def toggle_account(self):
        if self.cookies:
            try:
                self.account_path.unlink(missing_ok=True)
            except OSError:
                self.account_status.setText("无法移除本机登录凭据，请重试")
                return
            self.cookies = {}
            self.account_task = None
            self.followed.clear()
            self.followed.hide()
            self.account_status.setText("已退出本机登录")
        elif self.qr_key:
            self.end_qr("已取消登录")
        else:
            self.account_status.setText("正在获取二维码…")
            self.account_request("generate", account.generate_qr)
        self.update_account_controls()
        if self.panel.isVisible():
            self.place_panel()

    def end_qr(self, message):
        self.account_task = None
        self.qr_key = None
        self.account_qr.clear()
        self.account_qr.hide()
        self.account_status.setText(message)

    def refresh_followed(self):
        if self.cookies and not self.account_task:
            self.account_status.setText("读取中…")
            self.account_request("followed", account.followed_live, self.cookies.copy())

    def show_qr(self, url):
        matrix = qrcode.QRCode(box_size=1, border=4)
        matrix.add_data(url)
        matrix.make()
        grid = matrix.get_matrix()
        size = len(grid)
        image = QtGui.QImage(size * 4, size * 4, QtGui.QImage.Format_RGB32)
        image.fill(QtCore.Qt.white)
        painter = QtGui.QPainter(image)
        for y, row in enumerate(grid):
            for x, filled in enumerate(row):
                if filled:
                    painter.fillRect(x * 4, y * 4, 4, 4, QtCore.Qt.black)
        painter.end()
        self.account_qr.setPixmap(QtGui.QPixmap.fromImage(image))
        self.account_qr.show()

    def update_account(self):
        if self.account_task:
            action, future = self.account_task
            if not future.done():
                return
            self.account_task = None
            try:
                result = future.result()
                if action == "generate":
                    url, self.qr_key = result
                    self.qr_deadline = time.monotonic() + 180
                    self.show_qr(url)
                    self.account_status.setText("用 B 站 App 扫码")
                elif action == "poll":
                    code, cookies = result
                    if code == 0:
                        account.save_session(self.account_path, cookies)
                        self.cookies = cookies
                        self.end_qr("登录成功")
                        self.refresh_followed()
                    elif code == 86090:
                        self.account_status.setText("请在手机上确认")
                    elif code != 86101:
                        self.end_qr("二维码已失效，请重新扫码")
                else:
                    self.followed.clear()
                    for room, info in result.items():
                        self.followed.addItem(room_item(room, info, info["uname"],
                                                        info["title"] or f"房间 {room}"))
                    self.sync_room_selection()
                    self.account_status.setText(f"{len(result)} 人开播" if result else "暂无开播")
            except account.LoginExpired as error:
                try:
                    self.account_path.unlink(missing_ok=True)
                except OSError:
                    logging.warning("Cannot remove expired account session")
                self.cookies = {}
                self.followed.clear()
                self.followed.hide()
                self.end_qr(str(error))
            except (account.AccountError, OSError, ValueError, KeyError, TypeError) as error:
                message = str(error) if isinstance(error, account.AccountError) else "登录数据无法读取或保存"
                self.end_qr(message)
            self.update_account_controls()
            if self.panel.isVisible():
                self.place_panel()
        if self.qr_key and not self.account_task:
            now = time.monotonic()
            if now >= self.qr_deadline:
                self.end_qr("二维码已过期，请重新扫码")
                self.update_account_controls()
                if self.panel.isVisible():
                    self.place_panel()
            elif now >= self.next_poll:
                self.next_poll = now + 2
                self.account_request("poll", account.poll_qr, self.qr_key)

    def play_list_room(self, item):
        room, info = item.data(QtCore.Qt.UserRole)
        self.info[room] = {**self.info.get(room, {}), **info}
        self.start(room)

    def sync_room_selection(self):
        for view in [self.followed, *self.room_views]:
            for index in range(view.count()):
                item = view.item(index)
                item.setSelected(item.data(QtCore.Qt.UserRole)[0] == self.room)

    def favorite_current(self):
        self.toggle_favorite(self.room, self.details())

    def is_favorite(self, room):
        return any(item["room"] == room for item in self.favorites)

    def toggle_favorite(self, room, info):
        if self.is_favorite(room):
            self.favorites = [item for item in self.favorites if item["room"] != room]
        else:
            self.favorites.insert(0, {"room": room, "name": info.get("uname") or ""})
        self.save()
        self.update_display()

    def room_menu(self, view, point):
        item = view.itemAt(point)
        if not item:
            return
        room, info = item.data(QtCore.Qt.UserRole)
        menu = QtWidgets.QMenu(self.panel)
        action = menu.addAction("取消收藏" if self.is_favorite(room) else "收藏房间")
        self.room_menu_open = True
        try:
            if menu.exec(view.viewport().mapToGlobal(point)) == action:
                self.toggle_favorite(room, info)
        finally:
            self.room_menu_open = False

    def details(self):
        return self.info.get(self.room, {})

    @staticmethod
    def elide(label, text, width=None):
        label.set_full_text(text)

    def update_display(self):
        info = self.details()
        name = info.get("uname") or ""
        self.avatar.setVisible(self.options["avatar"])
        face_url, face = self.faces.get(info.get("uid"), (None, None))
        if face:
            self.avatar.setPixmap(face)
        else:
            self.avatar.setFont(app_font(24, True) if initial(name) else icon_font(24))
            self.avatar.setText(initial(name) or GLYPHS["music"])
        if self.options["avatar"]:
            self.want_face(info.get("uid"))
        self.live.setVisible(info.get("live_status") == 1 or self.state == "playing")
        areas = dict.fromkeys(filter(None, (info.get("parent_area_name"), info.get("area_name"))))
        self.elide(self.area, " · ".join(areas), 140)
        self.elide(self.title, info.get("title") or f"房间 {self.room}", 226)
        self.elide(self.byline, f"{name} · {self.room}" if name else f"房间 {self.room}", 226)
        active = self.state in ("playing", "loading")
        self.transport.setText(GLYPHS[{"playing": "pause", "loading": "stop",
                                       "error": "refresh"}.get(self.state, "play")])
        self.transport.setAccessibleName("停止" if active else "播放")
        self.stop_control.setEnabled(active)
        self.elide(self.status, self.error if self.state == "error" else self.notice)
        if hasattr(self, "chip"):
            self.chip.refresh()
        self.media.show(self.state, info.get("title") or f"房间 {self.room}",
                        name or APP_NAME, face_url or info.get("cover"))
        favorite = self.is_favorite(self.room)
        self.favorite_button.setText(GLYPHS["starred" if favorite else "star"])
        self.favorite_button.setStyleSheet(f"color:{self.accent};" if favorite else "")
        favorite_label = "取消收藏" if favorite else "收藏当前房间"
        self.favorite_button.setToolTip(favorite_label)
        self.favorite_button.setAccessibleName(favorite_label)
        self.update_saved_lists()
        self.sync_room_selection()

    def update_saved_lists(self):
        for view, records, (count, hint) in zip(
            self.room_views, (self.favorites, self.rooms), self.saved_counts,
        ):
            scroll = view.verticalScrollBar().value()
            view.clear()
            for record in records:
                room = record["room"]
                info = self.info.get(room, {})
                name = info.get("uname") or record["name"] or f"房间 {room}"
                view.addItem(room_item(room, info, name, info.get("title") or f"房间 {room}"))
            view.verticalScrollBar().setValue(scroll)
            count.setText(hint)
            count.setVisible(not records)
        self.fit_lists()


    def want_face(self, uid):
        if uid and uid not in self.faces and not (self.face_task and self.face_task[0] == uid):
            self.face_task = uid, self.network_pool.submit(fetch_face, uid)

    def check_face(self):
        if not (self.face_task and self.face_task[1].done()):
            return
        uid, future = self.face_task
        self.face_task = None
        url, content = future.result()
        image = QtGui.QImage.fromData(content) if content else QtGui.QImage()
        self.faces[uid] = (url, self.rounded(image) if not image.isNull() else None)
        if uid == self.details().get("uid"):
            self.update_display()

    def rounded(self, image):
        """Crop to a square and round the corners like the avatar tile."""
        ratio = max(self.panel.devicePixelRatioF(), self.app.primaryScreen().devicePixelRatio(), 1)
        size = round(56 * ratio)
        side = min(image.width(), image.height())
        square = image.copy((image.width() - side) // 2, (image.height() - side) // 2, side, side)
        square = square.scaled(size, size, QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.SmoothTransformation)
        result = QtGui.QPixmap(size, size)
        result.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(result)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(0, 0, size, size), 8 * ratio, 8 * ratio)
        painter.setClipPath(path)
        painter.drawImage(0, 0, square)
        painter.end()
        result.setDevicePixelRatio(ratio)
        return result

    def save(self):
        target = STATE_DIR / "settings.json"
        temp = target.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps({"rooms": self.rooms, "favorites": self.favorites,
                                        "volume": self.volume, "options": self.options},
                                       ensure_ascii=False), encoding="utf-8")
            temp.replace(target)
        except OSError:
            logging.exception("Saving settings")

    def change_volume(self, value):
        self.volume = value
        self.volume_label.setText(f"{value}")
        self.speaker.setText(GLYPHS["mute" if value == 0 else "volume"])
        self.save_timer.start()

    def open_room(self):
        try:
            os.startfile(f"https://live.bilibili.com/{self.room}")
        except OSError:
            logging.exception("Opening room in the default browser")
            self.notify("无法打开默认浏览器")

    def play_input(self):
        value = self.input.text()
        if not value.strip():
            self.input.setFocus()
            return
        if not short_link(value):
            try:
                value = room_number(value)
            except ValueError as error:
                self.notify(str(error))
                return
        self.start(value)

    def start(self, value, retry=False):
        self.notice = ""
        self.stop(retry=retry)
        self.room = value if not short_link(value) else self.room
        self.state = "loading"
        self.update_display()
        self.starting = (self.request, self.network_pool.submit(resolve_room, value))

    def toggle_playback(self):
        if self.state in ("loading", "playing"):
            self.stop()
        else:
            self.start(self.room)

    def stop(self, checked=False, retry=False):
        self.request += 1
        if self.starting:
            self.starting[1].cancel()
            self.starting = None
        if self.session:
            self.player_pool.submit(self.session.close)
            self.session = None
        if not retry:
            self.retries, self.retry_at = 0, 0
        self.state, self.error = "idle", ""
        self.update_display()

    def reconnect(self, error):
        """Retry after a dropped stream, backing off; returns False to give up."""
        offline = "未开播" in error
        if (not self.options["reconnect"] or self.closing or self.retries >= 5
                or offline and self.retries):  # the stream ended, not the network
            return False
        self.stop(retry=True)
        self.retries += 1
        self.retry_at = time.monotonic() + (2, 4, 8, 15, 30)[self.retries - 1]
        self.state = "loading"
        logging.info("Playback dropped (%s); reconnect %d/5", error, self.retries)
        self.update_display()
        return True

    def check_playback(self):
        if self.starting and self.starting[1].done():
            request, future = self.starting
            self.starting = None
            if request == self.request:
                try:
                    self.room = future.result()
                    if not PLAYER.is_file():
                        raise ValueError("找不到播放器 mpv.exe")
                except ValueError as error:
                    self.fail(str(error))
                else:
                    self.input.clear()
                    self.update_display()
                    launching = self.player_pool.submit(Playback, self.room, self.volume)
                    launching.add_done_callback(lambda f, token=request: self.launched.emit(token, f))

    launched = QtCore.Signal(int, object)

    @QtCore.Slot(int, object)
    def accept_launch(self, token, future):
        try:
            session = future.result()
        except Exception:
            logging.exception("Starting playback")
            if token == self.request:
                self.fail("播放器启动失败")
            return
        if token == self.request and not self.closing:
            self.session = session
        else:
            session.close()

    def check_session(self):
        if not self.polling:
            session = self.session
            if not session:
                return
            volume = self.volume

            def poll():
                status = session.status()
                if (not status and session.ready
                        and getattr(session, "applied_volume", None) != volume
                        and session.volume(volume)):
                    session.applied_volume = volume
                return status

            self.polling = session, self.player_pool.submit(poll)
        session, future = self.polling
        if not future.done():
            return
        self.polling = None
        if session is not self.session:
            return
        try:
            error = future.result()
        except OSError:
            logging.exception("Checking playback")
            error = "无法读取播放状态"
        if error:
            # Only streams that were already playing are retried, not a room that never started.
            if (session.ready or self.retries) and self.reconnect(error):
                return
            self.fail("直播已结束" if self.retries and "未开播" in error else error)
            return
        changed = session.info and {**self.details(), **session.info} != self.details()
        if changed:
            self.info[self.room] = {**self.details(), **session.info}
        if session.ready and (self.state != "playing" or changed):
            self.state = "playing"
            self.retries = 0
            self.remember(self.room)
            self.update_display()

    def remember(self, room):
        saved = next((item["name"] for item in self.rooms if item["room"] == room), "")
        self.rooms = [{"room": room, "name": self.details().get("uname") or saved}] + [
            item for item in self.rooms if item["room"] != room][:49]
        if not self.save_timer.isActive():
            self.save()

    def fail(self, message):
        self.stop()
        self.state, self.error = "error", message
        self.update_display()

    def tick(self):
        self.update_account()
        if self.retry_at and time.monotonic() >= self.retry_at:
            self.retry_at = 0
            self.start(self.room, retry=True)
        self.check_playback()
        self.check_session()
        self.check_face()
        if self.recent_info and self.recent_info.done():
            info = self.recent_info.result()
            self.info.update(info)
            changed = False
            for item in self.rooms + self.favorites:
                name = info.get(item["room"], {}).get("uname")
                if name and name != item["name"]:
                    item["name"] = name
                    changed = True
            if changed and not self.save_timer.isActive():
                self.save()
            self.recent_info = None
            self.update_display()
            if self.panel.isVisible():
                self.place_panel()
        if time.monotonic() >= self.dock_at:
            self.dock_at = time.monotonic() + .35
            self.dock()

    def slot(self, bar, bounds, scale):
        width, height = bounds.right - bounds.left, bounds.bottom - bounds.top
        if width <= height or height < round(28 * scale):
            return None
        gap, size = round(8 * scale), round(232 * scale)
        start = rect(find_child(bar, None, "Start", None))
        apps = rect(find_child(bar, None, "ReBarWindow32", None))
        tray = rect(find_child(bar, None, "TrayNotifyWnd", None))
        if not (start and apps and tray):
            return None
        left = bounds.left + gap
        if left + size + gap > start.left:
            left = max(start.right, apps.right) + gap
            if left + size + gap > tray.left:
                return None
        chip_height = min(height, round(40 * scale))
        return left, bounds.top + (height - chip_height) // 2, size, chip_height

    def dock(self):
        bar = find_window("Shell_TrayWnd", None)
        bounds = rect(bar)
        scale = (get_dpi(bar) or 96) / 96 if bar else 1
        slot = self.slot(bar, bounds, scale) if bounds else None
        hwnd = int(self.chip.winId())
        if not is_window(hwnd):
            self.chip.deleteLater()
            self.chip = Chip(self)
            hwnd = int(self.chip.winId())
            self.anchor = None
        self.bar = bar
        if slot:
            if not visible(bar) or self.fullscreen(bar, scale):
                self.anchor = slot
                self.chip.hide()
                self.chip.refresh()
                self.panel.hide()
                return
            if not self.chip.isVisible():
                self.chip.show()
                hwnd = int(self.chip.winId())
                self.chip.refresh()
            if get_parent(hwnd) != bar:
                update_long(hwnd, -16, 0x40000000, 0x80000000)
                set_parent(hwnd, bar)
                self.chip.redraw()
            origin = wt.POINT(*slot[:2])
            screen_to_client(bar, ct.byref(origin))
            positioned = get_parent(hwnd) == bar and set_position(hwnd, 0, origin.x, origin.y, *slot[2:], 0x50)
            self.anchor = slot if positioned else None
        else:
            self.anchor = None
        if self.anchor:
            self.fallback_opened = False
        else:
            if self.chip.isVisible():
                self.chip.hide()
                self.chip.refresh()
            if not self.fallback_opened:
                self.fallback_opened = True
                self.show_panel()

    def fullscreen(self, bar, scale):
        foreground = get_foreground()
        if not foreground or is_zoomed(foreground) or foreground in (
            bar, int(self.panel.winId()), int(self.chip.winId()),
        ):
            return False
        if monitor_from_window(foreground, 0) != monitor_from_window(bar, 0):
            return False
        bounds = rect(foreground)
        screen = self.app.primaryScreen().geometry()
        return bool(bounds and bounds.left <= 0 and bounds.top <= 0
                    and bounds.right >= round(screen.width() * scale)
                    and bounds.bottom >= round(screen.height() * scale))

    def dismiss_panel(self):
        if self.anchor and self.panel.isVisible() and not self.room_menu_open:
            self.dismissed_at = time.monotonic()
            self.panel.hide()
            self.chip.refresh()

    def toggle_panel(self):
        if self.panel.isVisible():
            self.panel.hide()
            self.chip.refresh()
        elif time.monotonic() - self.dismissed_at >= 0.4:
            self.show_panel()

    def show_panel(self):
        if not self.panel.isVisible():
            self.follow_height = None
        self.refresh_followed()
        if not self.recent_info:
            rooms = list(dict.fromkeys([self.room, *(item["room"] for item in self.rooms[:6] + self.favorites[:6])]))
            self.recent_info = self.network_pool.submit(room_info, rooms)
        self.panel.ensurePolished()
        self.content.layout().activate()
        if not self.panel.isVisible():
            self.place_panel()
        if not getattr(self.panel, "backdrop_set", False):
            self.panel.apply_backdrop()
            self.panel.backdrop_set = True
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()
        self.chip.refresh()

    def eventFilter(self, watched, event):
        if (watched is self.content and event.type() == QtCore.QEvent.LayoutRequest
                and self.panel.isVisible() and not self.placing):
            self.placing = True
            QtCore.QTimer.singleShot(0, self.place_panel)
        return False

    def place_panel(self):
        self.placing = False
        scale = (get_dpi(self.bar) or 96) / 96 if self.bar else self.panel.devicePixelRatioF()
        bar = rect(self.bar)
        screen = self.app.primaryScreen().geometry()
        gap = round(8 * scale)
        at_top = bar and bar.top < round(screen.height() * scale) // 2
        top = ((self.anchor[1] + self.anchor[3] if self.anchor else bar.bottom) + gap) if at_top else gap
        bottom = (round(screen.height() * scale) - gap if at_top else
                  (self.anchor[1] if self.anchor else bar.top if bar else round(screen.height() * scale)) - gap)
        self.content.layout().activate()
        desired = self.content.sizeHint().height()
        if self.cookies and self.pages.currentWidget() is self.followed.parentWidget():
            if self.follow_height is None:
                self.follow_height = desired
            desired = self.follow_height
        else:
            self.follow_height = None
        logical_height = min(desired, max(1, int((bottom - top) / scale)))
        width, height = round(368 * scale), round(logical_height * scale)
        x = self.anchor[0] if self.anchor else round(8 * scale)
        y = top if at_top else bottom - height
        x = max(0, min(x, round(screen.width() * scale) - width))
        geometry = QtCore.QRect(screen.left() + round(x / scale),
                                screen.top() + round(max(0, y) / scale), 368, logical_height)
        if self.panel.geometry() != geometry:
            self.panel.setGeometry(geometry)

    def close(self):
        self.closing = True
        self.timer.stop()
        # Qt may still deliver events to the panel while Python tears this object down.
        self.content.removeEventFilter(self)
        self.stop()
        self.media.enable(False)
        self.save()
        self.player_pool.shutdown(wait=True)
        QtCore.QCoreApplication.sendPostedEvents(self, QtCore.QEvent.MetaCall)
        self.network_pool.shutdown(wait=False, cancel_futures=True)


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=STATE_DIR / "widget.log", encoding="utf-8", level=logging.INFO)
    handle = create_event(None, False, False, "Local\\BiliLiveAudioShowPanel")
    already_running = ct.get_last_error() == 183
    if not handle:
        raise ct.WinError(ct.get_last_error())
    try:
        if already_running:
            if not signal_event(handle):
                raise ct.WinError(ct.get_last_error())
            return
        USER.SetProcessDpiAwarenessContext(ct.c_void_p(-4))
        app = QtWidgets.QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        radio = Radio(app)
        radio.show_panel()
        launcher = QtCore.QTimer(interval=150)

        def check_launcher():
            if wait_event(handle, 0) == 0:
                radio.show_panel()
                logging.info("Panel reopened by launcher")

        launcher.timeout.connect(check_launcher)
        launcher.start()
        try:
            app.exec()
        finally:
            radio.close()
    finally:
        close_handle(handle)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Could not start the taskbar widget")
        raise
