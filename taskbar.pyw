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
            if handle:
                assign_job(job, handle)
                close_handle(handle)
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
        return (saved_rooms(data.get("rooms")), saved_rooms(data.get("favorites")),
                max(0, min(100, int(data.get("volume", 60)))))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return [], [], 60


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
                    pipe.write((json.dumps(command) + "\n").encode("ascii"))
            except OSError:
                logging.debug("mpv volume IPC unavailable", exc_info=True)

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


class Flyout(QtWidgets.QWidget):
    dismissed = QtCore.Signal()

    def __init__(self, dark):
        super().__init__(None, QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint |
                         QtCore.Qt.WindowStaysOnTopHint)
        self.dark = dark
        self.acrylic = False
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setWindowTitle(APP_NAME)
        self.setMinimumHeight(0)

    def apply_backdrop(self):
        hwnd = int(self.winId())

        def attribute(key, value):
            setting = ct.c_int(value)
            return dwm_set(hwnd, key, ct.byref(setting), ct.sizeof(setting)) == 0

        attribute(20, int(self.dark))
        attribute(33, 2)
        margins = (ct.c_int * 4)(-1, -1, -1, -1)
        self.acrylic = (light_theme("EnableTransparency")
                        and dwm_extend(hwnd, ct.byref(margins)) == 0 and attribute(38, 3))
        logging.info("DWM transient backdrop enabled: %s", self.acrylic)
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        fill = (40, 40, 40, 194) if self.dark else (250, 250, 250, 189)
        tint = 55 if self.acrylic else fill[3]
        painter.setBrush(QtGui.QColor(*fill[:3], tint))
        painter.setPen(QtGui.QColor(255, 255, 255, 23) if self.dark else QtGui.QColor(0, 0, 0, 23))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)

    def event(self, event):
        if event.type() == QtCore.QEvent.WindowDeactivate:
            self.dismissed.emit()
        return super().event(event)


class RoomDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, view, foreground, accent, selected, hover):
        super().__init__(view)
        self.foreground = QtGui.QColor(foreground)
        self.accent = QtGui.QColor(accent)
        self.selected = QtGui.QColor(selected)
        self.hover = hover

    def sizeHint(self, option, index):
        return QtCore.QSize(0, 32)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        bounds = option.rect.adjusted(3, 1, -3, -1)
        active = bool(option.state & QtWidgets.QStyle.State_Selected)
        hovered = bool(option.state & QtWidgets.QStyle.State_MouseOver)
        painter.setPen(QtCore.Qt.NoPen)
        if active or hovered:
            painter.setBrush(self.selected if active else self.hover)
            painter.drawRoundedRect(bounds, 5, 5)
        if active:
            marker = QtCore.QRect(bounds.left() + 2, bounds.center().y() - 8, 3, 16)
            painter.setBrush(self.accent)
            painter.drawRoundedRect(marker, 1.5, 1.5)
        painter.setFont(option.font)
        painter.setPen(self.foreground)
        text_bounds = bounds.adjusted(11, 0, -8, 0)
        text = painter.fontMetrics().elidedText(index.data(), QtCore.Qt.ElideRight, text_bounds.width())
        painter.drawText(text_bounds, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, text)
        if option.state & QtWidgets.QStyle.State_HasFocus and option.state & QtWidgets.QStyle.State_KeyboardFocusChange:
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.setPen(self.accent)
            painter.drawRoundedRect(bounds.adjusted(0, 0, -1, -1), 5, 5)
        painter.restore()


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
        painter.setBrush(QtGui.QColor("#ffffff"))
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

    def animate(self):
        self.phase = (self.phase + 1) % 12
        self.update()

    def refresh(self):
        active = self.radio.state in ("loading", "playing") and self.isVisible()
        if active and not self.animation.isActive():
            self.animation.start()
        elif not active:
            self.animation.stop()
        self.update()

    def mouseMoveEvent(self, event):
        self.hover_button = event.position().x() >= 198
        self.update()

    def enterEvent(self, event):
        self.update()

    def leaveEvent(self, event):
        self.hover_button = False
        self.update()

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
        fg, muted, accent = ("#ffffff", "#c5c5c5", "#60cdff") if dark else ("#1b1b1b", "#5c5c5c", "#005fb8")
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

        avatar = QtCore.QRect(6, 6, 28, 28)
        p.setBrush(QtGui.QColor("#1f3a5a" if dark else "#dbe8f8"))
        glyph, ink = "♫", accent
        if state == "error":
            p.setBrush(QtGui.QColor("#433519" if dark else "#fff4ce"))
            glyph, ink = "!", "#fce100" if dark else "#8a5300"
        elif state == "playing":
            glyph = (info.get("uname") or "♫")[0]
            p.setPen(QtGui.QColor(accent))
        if state == "loading":
            p.setBrush(QtCore.Qt.NoBrush)
            p.setPen(QtGui.QPen(QtGui.QColor(accent), 2))
            p.drawArc(avatar.adjusted(3, 3, -3, -3), self.phase * 480, 245 * 16)
        else:
            p.drawEllipse(avatar)
            p.setPen(QtGui.QColor(ink))
            p.drawText(avatar, QtCore.Qt.AlignCenter, glyph)

        labels = {
            "idle": ("B 站直播", "点一下选直播间", "▶"),
            "loading": ("正在连接…", radio.room, "■"),
            "playing": (info.get("uname") or radio.room, info.get("title") or "仅播放音频", "Ⅱ"),
            "error": (radio.error or "播放失败", radio.room, "↻"),
        }
        title, subtitle, action = labels[state]
        for text, y, size, color in ((title, 4, 12, fg), (subtitle, 20, 11, muted)):
            font = app_font(size, y == 4)
            p.setFont(font)
            p.setPen(QtGui.QColor(color))
            width = 125 if state == "playing" else 150
            p.drawText(QtCore.QRect(44, y, width, 16), QtCore.Qt.AlignVCenter,
                       p.fontMetrics().elidedText(text, QtCore.Qt.ElideRight, width))
        if state == "playing":
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QColor(accent))
            for i in range(3):
                height = (8, 11, 14, 11)[(self.phase + i) % 4]
                p.drawRoundedRect(176 + i * 5, 20 - height // 2, 3, height, 1, 1)
        p.setFont(app_font(13, True))
        p.setPen(QtGui.QColor(fg))
        p.drawText(QtCore.QRect(198, 6, 28, 28), QtCore.Qt.AlignCenter, action)


def app_font(size, bold=False):
    font = QtGui.QFont()
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI"])
    font.setPixelSize(size)
    font.setBold(bold)
    return font


class Radio(QtCore.QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.rooms, self.favorites, self.volume = load_state()
        self.room = self.rooms[0]["room"] if self.rooms else DEFAULT_ROOM
        self.info = {item["room"]: {"uname": item["name"]}
                     for item in self.favorites + self.rooms if item["name"]}
        self.room_menu_open = False
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
        self.launched.connect(self.accept_launch, QtCore.Qt.QueuedConnection)
        self.build_panel()
        self.chip = Chip(self)
        self.chip.winId()
        self.timer = QtCore.QTimer(self, interval=120)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.dock()

    def build_panel(self):
        dark = self.panel.dark
        fg, muted, accent = ("#ffffff", "#c5c5c5", "#60cdff") if dark else ("#1b1b1b", "#5c5c5c", "#005fb8")
        on_accent = "#151515" if dark else "#ffffff"
        edge = "rgba(255,255,255,20)" if dark else "rgba(0,0,0,20)"
        card = "rgba(255,255,255,15)" if dark else "rgba(255,255,255,168)"
        hover = "rgba(255,255,255,18)" if dark else "rgba(0,0,0,12)"
        self.panel.setStyleSheet(f"""
            QLabel, QLineEdit, QPushButton {{color:{fg};}}
            QLabel[muted="true"] {{color:{muted};}}
            QFrame#now {{background:{card}; border:1px solid {edge}; border-radius:6px;}}
            QPushButton {{background:transparent; border:1px solid transparent; border-radius:5px;}}
            QPushButton:hover, QPushButton[current="true"] {{background:{hover};}}
            QPushButton:focus {{border-color:{accent};}}
            QPushButton:disabled {{color:{muted};}}
            QPushButton#transport {{background:{accent}; color:{on_accent}; border-radius:20px;}}
            QPushButton#transport:hover {{border-color:{on_accent};}}
            QPushButton#stop {{background:{hover}; border-radius:16px;}}
            QPushButton#browser {{color:{accent}; text-align:left;}}
            QLineEdit {{background:{card}; border:1px solid {edge}; border-radius:5px;
                       border-bottom:1px solid {muted}; padding:0 8px;}}
            QLineEdit:focus {{border-bottom:2px solid {accent};}}
            QLabel#avatar {{background:{"#1f3a5a" if dark else "#dbe8f8"}; color:{accent}; border-radius:8px;}}
            QLabel#live {{background:{"#4a2427" if dark else "#fde7e9"};
                          color:{"#ffb3ba" if dark else "#a4262c"}; padding:2px 5px; border-radius:4px;}}
        """)
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
        main = QtWidgets.QVBoxLayout(content)
        main.setContentsMargins(16, 16, 16, 16)
        main.setSpacing(12)

        def label(text="", muted=False):
            result = QtWidgets.QLabel(text)
            result.setTextFormat(QtCore.Qt.PlainText)
            result.setProperty("muted", muted)
            return result

        def button(text, action, name="", size=None, tip=None):
            result = QtWidgets.QPushButton(text, objectName=name)
            result.setCursor(QtCore.Qt.PointingHandCursor)
            if size:
                result.setFixedSize(*size)
            if tip:
                result.setToolTip(tip)
                result.setAccessibleName(tip)
            result.clicked.connect(action)
            return result

        header = QtWidgets.QHBoxLayout()
        header.addWidget(label("♫   " + APP_NAME, True))
        header.addStretch()
        header.addWidget(button("—", self.panel.hide, size=(28, 28), tip="收起"))
        header.addWidget(button("×", self.app.quit, size=(28, 28), tip="退出"))
        main.addLayout(header)

        now = QtWidgets.QFrame(objectName="now")
        body = QtWidgets.QVBoxLayout(now)
        body.setContentsMargins(14, 14, 14, 14)
        body.setSpacing(12)
        summary = QtWidgets.QHBoxLayout()
        summary.setSpacing(10)
        self.avatar = label()
        self.avatar.setObjectName("avatar")
        self.avatar.setFixedSize(56, 56)
        self.avatar.setAlignment(QtCore.Qt.AlignCenter)
        self.avatar.setFont(app_font(24, True))
        summary.addWidget(self.avatar)
        texts = QtWidgets.QVBoxLayout()
        texts.setSpacing(3)
        category = QtWidgets.QHBoxLayout()
        self.live = label("● 直播中")
        self.live.setObjectName("live")
        self.area = label(muted=True)
        category.addWidget(self.live)
        category.addWidget(self.area, 1)
        self.favorite_button = button("☆", self.favorite_current, size=(28, 26), tip="收藏当前房间")
        category.addWidget(self.favorite_button)
        texts.addLayout(category)
        self.title = label()
        self.title.setFont(app_font(14, True))
        self.byline = label(muted=True)
        texts.addWidget(self.title)
        texts.addWidget(self.byline)
        summary.addLayout(texts, 1)
        body.addLayout(summary)
        controls = QtWidgets.QHBoxLayout()
        self.transport = button("▶", self.toggle_playback, "transport", (40, 40), "播放")
        self.stop_control = button("■", self.stop, "stop", (32, 32), "停止")
        controls.addWidget(self.transport)
        controls.addWidget(self.stop_control)
        controls.addSpacing(8)
        speaker = label("\uE767")
        speaker.setFont(QtGui.QFont("Segoe Fluent Icons", 11))
        controls.addWidget(speaker)
        slider = VolumeSlider(QtCore.Qt.Horizontal)
        slider.setRange(0, 100)
        slider.setFixedHeight(24)
        palette = slider.palette()
        palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(accent))
        palette.setColor(QtGui.QPalette.Mid, QtGui.QColor("#777777" if dark else "#aaaaaa"))
        slider.setPalette(palette)
        slider.setAccessibleName("音量")
        slider.setValue(self.volume)
        slider.valueChanged.connect(self.change_volume)
        controls.addWidget(slider, 1)
        self.volume_label = label(f"{self.volume}%", True)
        self.volume_label.setFixedWidth(33)
        self.volume_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        controls.addWidget(self.volume_label)
        body.addLayout(controls)
        main.addWidget(now)

        switch = QtWidgets.QVBoxLayout()
        switch.setSpacing(5)
        switch.addWidget(label("切换直播间", True))
        entry = QtWidgets.QHBoxLayout()
        self.input = QtWidgets.QLineEdit(placeholderText="房间号、直播链接或 b23.tv 短链")
        self.input.setAccessibleName("切换直播间")
        self.input.setFixedHeight(32)
        self.input.returnPressed.connect(self.play_input)
        entry.addWidget(self.input, 1)
        entry.addWidget(button("→", self.play_input, size=(32, 32), tip="播放这个房间"))
        switch.addLayout(entry)
        tabs = QtWidgets.QTabBar()
        tabs.setExpanding(True)
        tabs.setStyleSheet(f"""
            QTabBar::tab {{color:{muted}; padding:7px 14px; border-bottom:2px solid transparent;}}
            QTabBar::tab:hover {{background:{hover};}}
            QTabBar::tab:selected {{color:{fg}; border-bottom-color:{accent};}}
        """)
        pages = QtWidgets.QStackedWidget()
        follow_page = QtWidgets.QWidget()
        follow_layout = QtWidgets.QVBoxLayout(follow_page)
        follow_layout.setContentsMargins(0, 0, 0, 0)
        follow_layout.setSpacing(5)
        tabs.addTab("关注")
        pages.addWidget(follow_page)
        self.room_views = []
        self.saved_counts = []
        for title, hint in (("收藏", "暂无收藏，可点上方 ☆ 或右键房间"),
                            ("历史", "暂无播放历史")):
            tabs.addTab(title)
            page = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(5)
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
        tabs.currentChanged.connect(pages.setCurrentIndex)
        main.addWidget(tabs)
        main.addWidget(pages)

        followed_header = QtWidgets.QHBoxLayout()
        followed_header.addWidget(label("正在开播", True))
        followed_header.addStretch()
        self.account_refresh = button("刷新", self.refresh_followed)
        followed_header.addWidget(self.account_refresh)
        self.account_action = button("", self.toggle_account)
        followed_header.addWidget(self.account_action)
        follow_layout.addLayout(followed_header)
        self.account_status = label(muted=True)
        self.account_status.setWordWrap(True)
        follow_layout.addWidget(self.account_status)
        self.account_qr = label()
        self.account_qr.setAlignment(QtCore.Qt.AlignCenter)
        self.account_qr.hide()
        follow_layout.addWidget(self.account_qr)
        self.followed = QtWidgets.QListWidget()
        selected = "#24465a" if dark else "#dcecf9"
        for view in [self.followed, *self.room_views]:
            view.setStyleSheet(
                f"QListWidget {{background:{card}; color:{fg}; border:1px solid {edge};"
                f" border-radius:6px; outline:none;}}"
            )
            view.setItemDelegate(RoomDelegate(
                view, fg, accent, selected,
                QtGui.QColor(255, 255, 255, 18) if dark else QtGui.QColor(0, 0, 0, 12),
            ))
            view.setMouseTracking(True)
            view.setUniformItemSizes(True)
            view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            view.setFixedHeight(162)
            view.itemClicked.connect(self.play_list_room)
            view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda point, target=view: self.room_menu(target, point))
        self.followed.setAccessibleName("正在开播的关注")
        self.followed.setVisible(bool(self.cookies))
        follow_layout.addWidget(self.followed)
        follow_layout.addStretch()
        self.update_account_controls()
        self.account_status.setText("刷新可查看关注直播" if self.cookies else "扫码登录后查看")
        line = QtWidgets.QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{edge};")
        main.addWidget(line)
        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(button("在浏览器中打开 ↗", self.open_room, "browser"))
        footer.addStretch()
        self.status = label(muted=True)
        footer.addWidget(self.status)
        main.addLayout(footer)
        main.addLayout(switch)
        escape = QtGui.QShortcut(QtGui.QKeySequence("Escape"), self.panel)
        escape.activated.connect(self.panel.hide)
        self.update_display()

    def update_account_controls(self):
        busy = self.account_task is not None
        self.followed.setVisible(bool(self.cookies))
        self.account_refresh.setVisible(bool(self.cookies))
        self.account_refresh.setEnabled(not busy)
        self.account_action.setText("退出登录" if self.cookies else
                                    "取消登录" if self.qr_key else "扫码登录")
        self.account_action.setEnabled(not busy or bool(self.cookies or self.qr_key))

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
            self.account_status.setText("正在读取关注直播…")
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
                    self.account_status.setText("用哔哩哔哩 App 扫码并确认")
                elif action == "poll":
                    code, cookies = result
                    if code == 0:
                        account.save_session(self.account_path, cookies)
                        self.cookies = cookies
                        self.end_qr("登录成功")
                        self.refresh_followed()
                    elif code == 86090:
                        self.account_status.setText("已扫码，请在手机上确认")
                    elif code != 86101:
                        self.end_qr("二维码已失效，请重新扫码")
                else:
                    self.followed.clear()
                    for room, info in result.items():
                        item = QtWidgets.QListWidgetItem(f"{info['uname']}  ·  {info['title'] or room}")
                        item.setData(QtCore.Qt.UserRole, (room, info))
                        self.followed.addItem(item)
                    self.sync_room_selection()
                    self.account_status.setText(f"{len(result)} 个房间正在直播" if result else
                                                "关注的主播暂时没有开播")
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
    def elide(label, text, width):
        label.setText(label.fontMetrics().elidedText(text, QtCore.Qt.ElideRight, width))
        label.setToolTip(text)

    def update_display(self):
        info = self.details()
        name = info.get("uname") or ""
        self.avatar.setText(name[:1] or "♫")
        self.live.setVisible(info.get("live_status") == 1 or self.state == "playing")
        areas = dict.fromkeys(filter(None, (info.get("parent_area_name"), info.get("area_name"))))
        self.elide(self.area, " · ".join(areas), 140)
        self.elide(self.title, info.get("title") or f"房间 {self.room}", 226)
        self.elide(self.byline, f"{name} · 房间 {self.room}" if name else f"房间 {self.room}", 226)
        active = self.state in ("playing", "loading")
        self.transport.setText({"playing": "Ⅱ", "loading": "■", "error": "↻"}.get(self.state, "▶"))
        self.transport.setAccessibleName("停止" if active else "播放")
        self.stop_control.setEnabled(active)
        status = {"idle": "待播放", "playing": "已连接 · 仅播放音频",
                  "loading": "正在连接…", "error": self.error}[self.state]
        self.elide(self.status, status, 180)
        if hasattr(self, "chip"):
            self.chip.refresh()
        favorite = self.is_favorite(self.room)
        self.favorite_button.setText("★" if favorite else "☆")
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
                detail = info.get("title") or room
                item = QtWidgets.QListWidgetItem(f"{name}  ·  {detail}")
                item.setData(QtCore.Qt.UserRole, (room, info))
                view.addItem(item)
            view.verticalScrollBar().setValue(scroll)
            count.setText(f"{len(records)} 个房间" if records else hint)


    def save(self):
        target = STATE_DIR / "settings.json"
        temp = target.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps({"rooms": self.rooms, "favorites": self.favorites, "volume": self.volume},
                                       ensure_ascii=False), encoding="utf-8")
            temp.replace(target)
        except OSError:
            logging.exception("Saving settings")

    def change_volume(self, value):
        self.volume = value
        self.volume_label.setText(f"{value}%")
        self.save_timer.start()

    def open_room(self):
        try:
            os.startfile(f"https://live.bilibili.com/{self.room}")
        except OSError:
            logging.exception("Opening room in the default browser")
            self.status.setText("无法打开默认浏览器")

    def play_input(self):
        value = self.input.text()
        if not value.strip():
            self.input.setFocus()
            return
        if not short_link(value):
            try:
                value = room_number(value)
            except ValueError as error:
                self.elide(self.status, str(error), 180)
                return
        self.start(value)

    def start(self, value):
        self.stop()
        self.room = value if not short_link(value) else self.room
        self.state = "loading"
        self.update_display()
        self.starting = (self.request, self.network_pool.submit(resolve_room, value))

    def toggle_playback(self):
        if self.state in ("loading", "playing"):
            self.stop()
        else:
            self.start(self.room)

    def stop(self, checked=False):
        self.request += 1
        if self.starting:
            self.starting[1].cancel()
            self.starting = None
        if self.session:
            self.player_pool.submit(self.session.close)
            self.session = None
        self.state, self.error = "idle", ""
        self.update_display()

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
                if session.ready and getattr(session, "applied_volume", None) != volume:
                    session.volume(volume)
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
            self.fail(error)
            return
        changed = session.info and self.details() != session.info
        if changed:
            self.info[self.room] = session.info
        if session.ready and (self.state != "playing" or changed):
            self.state = "playing"
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
        self.check_playback()
        self.check_session()
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
                self.chip.update()
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

    def place_panel(self):
        scale = (get_dpi(self.bar) or 96) / 96 if self.bar else self.panel.devicePixelRatioF()
        bar = rect(self.bar)
        screen = self.app.primaryScreen().geometry()
        gap = round(8 * scale)
        at_top = bar and bar.top < round(screen.height() * scale) // 2
        top = ((self.anchor[1] + self.anchor[3] if self.anchor else bar.bottom) + gap) if at_top else gap
        bottom = (round(screen.height() * scale) - gap if at_top else
                  (self.anchor[1] if self.anchor else bar.top if bar else round(screen.height() * scale)) - gap)
        desired = self.content.sizeHint().height()
        if self.cookies:
            # Keep the logged-in viewport stable when history and follow data arrive.
            desired = max(560, desired)
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
        self.stop()
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
