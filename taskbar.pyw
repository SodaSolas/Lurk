import ctypes as ct
from ctypes import wintypes as wt
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
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.request import Request, urlopen
import uuid
import winreg

from listen import (DEFAULT_ROOM, PLAYER, player_command, resolve_room, room_number,
                    short_link, stream_command)


APP_NAME = "B 站直播电台"
STATE_DIR = Path(os.environ["LOCALAPPDATA"]) / "BiliLiveAudio"
USER = ct.WinDLL("user32", use_last_error=True)
KERNEL = ct.WinDLL("kernel32", use_last_error=True)
GDI = ct.WinDLL("gdi32", use_last_error=True)


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
set_layered = native(USER, "SetLayeredWindowAttributes", wt.BOOL, wt.HWND, wt.DWORD, ct.c_ubyte, wt.DWORD)
get_style = native(USER, "GetWindowLongW", wt.LONG, wt.HWND, ct.c_int)
set_style = native(USER, "SetWindowLongW", wt.LONG, wt.HWND, ct.c_int, wt.LONG)
get_rect = native(USER, "GetWindowRect", wt.BOOL, wt.HWND, ct.POINTER(wt.RECT))
set_position = native(USER, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND,
                      ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.UINT)
show_window = native(USER, "ShowWindow", wt.BOOL, wt.HWND, ct.c_int)
visible = native(USER, "IsWindowVisible", wt.BOOL, wt.HWND)
get_dpi = native(USER, "GetDpiForWindow", wt.UINT, wt.HWND)
is_window = native(USER, "IsWindow", wt.BOOL, wt.HWND)
set_region = native(USER, "SetWindowRgn", ct.c_int, wt.HWND, wt.HANDLE, wt.BOOL)
round_region = native(GDI, "CreateRoundRectRgn", wt.HANDLE, *([ct.c_int] * 6))
delete_region = native(GDI, "DeleteObject", wt.BOOL, wt.HANDLE)
window_at = native(USER, "WindowFromPoint", wt.HWND, wt.POINT)
is_child = native(USER, "IsChild", wt.BOOL, wt.HWND, wt.HWND)
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


def wrapper(window):
    window.update_idletasks()
    return get_parent(window.winfo_id())


def update_long(hwnd, index, add, remove):
    value = (get_style(hwnd, index) & 0xFFFFFFFF | add) & ~remove & 0xFFFFFFFF
    set_style(hwnd, index, value - (1 << 32) if value & 0x80000000 else value)


def round_window(hwnd, width, height, diameter):
    region = round_region(0, 0, width, height, diameter, diameter)
    if region and not set_region(hwnd, region, True):
        delete_region(region)


def light_theme(value="SystemUsesLightTheme"):
    try:
        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as settings:
            return bool(winreg.QueryValueEx(settings, value)[0])
    except OSError:
        return False


def load_state():
    try:
        data = json.loads((STATE_DIR / "settings.json").read_text(encoding="utf-8"))
        rooms = [room_number(room) for room in data["rooms"][:6]]
        volume = max(0, min(100, int(data["volume"])))
        return rooms or [DEFAULT_ROOM], volume
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return [DEFAULT_ROOM], 60


class Playback:
    def __init__(self, room, volume):
        self.room = room
        self.pipe = r"\\.\pipe\BiliLiveAudio-" + uuid.uuid4().hex
        self.started = time.monotonic()
        self.ready = False
        self.title = self.streamer = ""
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
        url = ("https://api.live.bilibili.com/xlive/web-room/v1/index/getRoomBaseInfo"
               f"?room_ids={self.room}&req_biz=web_room_componet")
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0",
                                            "Referer": "https://live.bilibili.com/"})
            with urlopen(request, timeout=5) as response:
                data = json.load(response)
            # Keyed by the long room id, which differs from short ids such as "6".
            room = next(iter(data["data"]["by_room_ids"].values()))
            self.title = " ".join(room["title"].split())
            self.streamer = " ".join(room["uname"].split())
        except (OSError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
            logging.debug("Room info unavailable", exc_info=True)

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


class Radio:
    def __init__(self, root):
        self.root = root
        self.rooms, self.volume = load_state()
        self.session = None
        self.bar = self.anchor = None
        self.fallback_opened = False
        self.volume_save = None
        self.resolving = self.resolved = None
        self.dismissed_at = 0
        self.room = tk.StringVar(value=self.rooms[0])
        self.status = tk.StringVar(value="待播放")
        self.widget_text = tk.StringVar(value="B 站直播")
        self.transport_text = tk.StringVar(value="▶")
        self.light_panel = light_theme("AppsUseLightTheme")
        self.palette = self.colors(self.light_panel)
        bg, card, fg, muted, accent, hover = self.palette
        self.border = "#dedede" if self.light_panel else "#474747"
        self.setup_styles()
        self.chip = tk.Toplevel(root)
        self.create_chip()
        self.panel = tk.Toplevel(root)
        self.panel.withdraw()
        self.panel.title(APP_NAME)
        self.panel.overrideredirect(True)
        self.panel.attributes("-topmost", True)
        self.panel.configure(bg=bg, highlightbackground=self.border, highlightthickness=1)
        self.panel.protocol("WM_DELETE_WINDOW", self.close)
        self.panel.bind("<Escape>", lambda event: self.panel.withdraw())
        self.panel.bind("<Return>", lambda event: self.play())
        self.panel.bind("<Control-w>", lambda event: self.close())
        self.panel.bind("<FocusOut>", lambda event: self.root.after(150, self.dismiss_panel))

        content = tk.Frame(self.panel, bg=bg, padx=16, pady=14)
        content.pack(fill="both", expand=True)
        titlebar = tk.Frame(content, bg=bg)
        titlebar.pack(fill="x")
        tk.Label(titlebar, text="\uE8D6", bg=bg, fg=accent,
                 font=("Segoe Fluent Icons", 13)).pack(side="left", padx=(0, 7))
        tk.Label(titlebar, text=APP_NAME, bg=bg, fg=fg,
                 font=("Segoe UI Variable Display", 14, "bold")).pack(side="left")
        self.button(titlebar, "退出", self.close, "Header").pack(side="right")
        self.button(titlebar, "收起", self.panel.withdraw, "Header").pack(side="right")

        room_card = tk.Frame(content, bg=card, padx=12, pady=10,
                             highlightbackground=self.border, highlightthickness=1)
        room_card.pack(fill="x", pady=(12, 8))
        tk.Label(room_card, text="房间号或直播链接", bg=card, fg=muted,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 5))
        self.rooms_field = ttk.Combobox(room_card, textvariable=self.room, values=self.rooms,
                                       style="Radio.TCombobox", font=("Segoe UI", 10))
        self.rooms_field.pack(fill="x")
        self.rooms_field.bind("<<ComboboxSelected>>", lambda event: self.play())

        controls = tk.Frame(room_card, bg=card)
        controls.pack(fill="x", pady=(10, 0))
        self.button(controls, "播放", self.play, "Accent").pack(side="left")
        self.stop_button = self.button(controls, "停止", self.stop)
        self.stop_button.pack(side="left", padx=8)
        self.stop_button.configure(state="disabled")
        self.button(controls, "打开直播间 ↗", self.open_room, "Link").pack(side="right")

        sound = tk.Frame(content, bg=card, padx=12, pady=9,
                         highlightbackground=self.border, highlightthickness=1)
        sound.pack(fill="x")
        tk.Label(sound, text="\uE767", bg=card, fg=fg,
                 font=("Segoe Fluent Icons", 12)).pack(side="left", padx=(0, 6))
        tk.Label(sound, text="音量", bg=card, fg=fg, font=("Segoe UI", 10)).pack(side="left")
        self.volume_text = tk.StringVar(value=f"{self.volume}%")
        tk.Label(sound, textvariable=self.volume_text, width=5, bg=card, fg=muted,
                 font=("Segoe UI", 9)).pack(side="right")
        slider = ttk.Scale(sound, from_=0, to=100, style="Radio.Horizontal.TScale")
        slider.set(self.volume)
        slider.configure(command=self.change_volume)
        slider.pack(side="left", fill="x", expand=True, padx=10)
        self.status_label = tk.Label(content, textvariable=self.status, bg=bg, fg=muted,
                                     font=("Segoe UI", 9), anchor="w")
        self.status_label.pack(fill="x", pady=(10, 0))
        self.update_dock()
        self.update_playback()

    def create_chip(self):
        bg, _, fg, _, accent, hover = self.colors(light_theme())
        self.chip.withdraw()
        self.chip.title(APP_NAME + " · 小组件")
        self.chip.overrideredirect(True)
        self.chip.configure(bg=bg)
        style = dict(bg=bg, activebackground=hover, activeforeground=fg,
                     borderwidth=0, highlightthickness=0)
        tk.Button(self.chip, text="\uE8D6", command=self.toggle_panel, fg=accent,
                  font=("Segoe Fluent Icons", 12), **style).place(relwidth=.16, relheight=1)
        tk.Button(self.chip, textvariable=self.widget_text, command=self.toggle_panel,
                  anchor="w", fg=fg, font=("Segoe UI", 9),
                  **style).place(relx=.16, relwidth=.52, relheight=1)
        tk.Button(self.chip, textvariable=self.transport_text, command=self.toggle_playback,
                  fg=fg, font=("Segoe UI Symbol", 10),
                  **style).place(relx=.68, relwidth=.16, relheight=1)
        tk.Button(self.chip, text="⋯", command=self.toggle_panel, fg=fg,
                  font=("Segoe UI", 11), **style).place(relx=.84, relwidth=.16, relheight=1)
        self.chip_hwnd = wrapper(self.chip)
        # Become a child of the taskbar, so it hides with it for fullscreen apps and
        # auto-hide. Only a layered child gets its own surface; a plain GDI child is
        # invisible under the Windows 11 XAML taskbar.
        update_long(self.chip_hwnd, -16, 0x40000000, 0x80000000)  # WS_CHILD, not WS_POPUP
        update_long(self.chip_hwnd, -20, 0x08080000, 0x00040080)  # NOACTIVATE|LAYERED
        set_layered(self.chip_hwnd, 0, 255, 2)  # LWA_ALPHA, fully opaque

    def toggle_playback(self):
        if self.session:
            self.stop()
        else:
            self.play()

    @staticmethod
    def colors(light):
        if light:
            return "#f3f3f3", "#fdfdfd", "#202020", "#5d5d5d", "#0067c0", "#e8e8e8"
        return "#202020", "#2b2b2b", "#f5f5f5", "#c5c5c5", "#60cdff", "#3b3b3b"

    def setup_styles(self):
        bg, card, fg, muted, accent, hover = self.palette
        style = ttk.Style(self.root)
        style.theme_use("clam")
        field = "#ffffff" if self.light_panel else "#333333"
        style.configure("Radio.TCombobox", fieldbackground=field, background=field,
                        foreground=fg, arrowcolor=fg, bordercolor=self.border,
                        lightcolor=field, darkcolor=field, selectbackground=accent,
                        selectforeground="#ffffff" if self.light_panel else "#151515",
                        padding=(8, 5))
        style.map("Radio.TCombobox", bordercolor=[("focus", accent)],
                  background=[("active", hover)])
        for key, surface in (("background", field), ("foreground", fg),
                             ("selectBackground", accent),
                             ("selectForeground", "#ffffff" if self.light_panel else "#151515")):
            self.root.option_add(f"*TCombobox*Listbox.{key}", surface)
        style.configure("Radio.Horizontal.TScale", background=accent, borderwidth=0,
                        troughcolor=hover, bordercolor=card, lightcolor=accent,
                        darkcolor=accent, sliderlength=14, sliderthickness=13)
        for name, surface, text, edge in (
            ("Radio", field, fg, self.border),
            ("Accent", accent, "#ffffff" if self.light_panel else "#151515", accent),
            ("Header", bg, fg, bg),
            ("Link", card, accent, card),
        ):
            key = f"{name}.TButton"
            style.configure(key, background=surface, foreground=text, bordercolor=edge,
                            lightcolor=surface, darkcolor=surface, relief="flat",
                            padding=(11, 5), font=("Segoe UI", 9))
            active = ("#005cae" if self.light_panel else "#72d4ff") if name == "Accent" else hover
            style.map(key, background=[("disabled", card), ("pressed", hover), ("active", active)],
                      foreground=[("disabled", muted), ("pressed", fg if name == "Accent" else text)],
                      bordercolor=[("focus", accent)])

    def button(self, parent, text, action, style="Radio"):
        return ttk.Button(parent, text=text, command=action, style=f"{style}.TButton")

    def save(self):
        self.volume_save = None
        target = STATE_DIR / "settings.json"
        temp = target.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps({"rooms": self.rooms, "volume": self.volume}), encoding="utf-8")
            temp.replace(target)
        except OSError:
            logging.exception("Saving settings")

    def change_volume(self, value):
        self.volume = round(float(value))
        self.volume_text.set(f"{self.volume}%")
        if self.session and self.session.ready:
            self.session.volume(self.volume)
        if self.volume_save:
            self.root.after_cancel(self.volume_save)
        self.volume_save = self.root.after(300, self.save)

    def open_room(self):
        try:
            room = room_number(self.room.get())
            os.startfile(f"https://live.bilibili.com/{room}")
        except ValueError as error:
            messagebox.showerror(APP_NAME, str(error), parent=self.panel)
        except OSError:
            logging.exception("Opening room in the default browser")
            messagebox.showerror(APP_NAME, "无法打开默认浏览器。", parent=self.panel)

    def play(self):
        value = self.room.get()
        if short_link(value):
            # Following the redirect needs the network; keep the UI (and taskbar) responsive.
            self.stop("正在解析短链接…")
            self.resolving = request = object()

            def resolve():
                try:
                    result = resolve_room(value)
                except ValueError as error:
                    result = error
                self.resolved = request, result

            Thread(target=resolve, daemon=True).start()
            return
        try:
            room = room_number(value)
        except ValueError as error:
            self.status.set(str(error))
            return
        self.start(room)

    def start(self, room):
        self.resolving = None
        self.stop()
        if not PLAYER.is_file():
            self.status.set("找不到播放器 mpv.exe")
            return
        try:
            self.session = Playback(room, self.volume)
        except OSError:
            logging.exception("Starting playback")
            self.status.set("播放器启动失败")
            return
        self.room.set(room)
        self.status.set(f"正在连接房间 {room}…")
        self.widget_text.set(room)
        self.transport_text.set("■")
        self.stop_button.configure(state="normal")

    def stop(self, message="待播放"):
        if message == "待播放":
            self.resolving = None
        if self.session:
            self.session.close()
            self.session = None
        self.status.set(message)
        self.widget_text.set("B 站直播")
        self.transport_text.set("▶")
        self.stop_button.configure(state="disabled")

    def update_playback(self):
        if self.resolved:
            request, result = self.resolved
            self.resolved = None
            if request is self.resolving:
                if isinstance(result, ValueError):
                    self.resolving = None
                    self.status.set(str(result))
                else:
                    self.room.set(result)
                    self.start(result)
        if self.session:
            was_ready = self.session.ready
            message = self.session.status()
            if message:
                self.stop(message)
            elif self.session.ready and not was_ready:
                room = self.session.room
                self.session.volume(self.volume)
                self.rooms = [room] + [saved for saved in self.rooms if saved != room][:5]
                self.rooms_field.configure(values=self.rooms)
                self.save()
            if self.session and self.session.ready:
                session = self.session
                details = [text for text in (session.streamer, session.title) if text]
                self.status.set(" · ".join(["正在播放", *(details or [session.room])])[:40])
                self.widget_text.set(session.streamer or session.title or session.room)
        self.root.after(400, self.update_playback)

    def slot(self, bar, bounds, scale):
        width = bounds.right - bounds.left
        height = bounds.bottom - bounds.top
        if width <= height:
            return None
        gap = round(9 * scale)
        size = round(216 * scale)
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
        chip_height = min(round(32 * scale), height - 2 * gap)
        if chip_height < round(16 * scale):
            return None
        return left, bounds.top + (height - chip_height) // 2, size, chip_height

    def update_dock(self):
        bar = find_window("Shell_TrayWnd", None)
        bounds = rect(bar)
        scale = (get_dpi(bar) or 96) / 96 if bar else 1
        slot = self.slot(bar, bounds, scale) if bounds and visible(bar) else None
        if not is_window(self.chip_hwnd):
            self.bar = self.anchor = None
            self.chip.destroy()
            self.chip = tk.Toplevel(self.root)
            self.create_chip()
        if slot:
            self.bar = bar
            if get_parent(self.chip_hwnd) != bar:
                set_parent(self.chip_hwnd, bar)
                self.anchor = None
            hit = window_at(wt.POINT(slot[0] + slot[2] // 2, slot[1] + slot[3] // 2))
            covered = (hit == bar or is_child(bar, hit)) and hit != self.chip_hwnd \
                and not is_child(self.chip_hwnd, hit)
            if slot != self.anchor or covered:
                self.chip.deiconify()
                origin = wt.POINT(slot[0], slot[1])
                screen_to_client(bar, ct.byref(origin))
                # HWND_TOP among the taskbar's children, above its XAML content bridge.
                positioned = set_position(self.chip_hwnd, 0, origin.x, origin.y,
                                          *slot[2:], 0x50)
                if positioned and slot != self.anchor:
                    round_window(self.chip_hwnd, *slot[2:], round(10 * scale))
                    logging.info("Taskbar widget positioned at %s", slot)
                self.anchor = slot if positioned else None
        else:
            if self.bar:
                show_window(self.chip_hwnd, 0)
            self.anchor = None
        if self.anchor:
            self.fallback_opened = False
        if not self.anchor and not self.fallback_opened:
            self.fallback_opened = True
            self.show_panel()
        self.root.after(650, self.update_dock)

    def dismiss_panel(self):
        """Close the flyout when focus leaves the app, like a Windows flyout."""
        # Only when docked; the standalone fallback panel has no other way back.
        # Ask Tcl directly: focus_get() fails on the combobox's dropdown.
        if self.anchor and self.panel.state() != "withdrawn" and not self.root.tk.call("focus"):
            self.panel.withdraw()
            self.dismissed_at = time.monotonic()

    def toggle_panel(self):
        if self.panel.state() == "withdrawn":
            # Clicking the widget took focus and already closed the panel: leave it closed.
            if time.monotonic() - self.dismissed_at > 0.4:
                self.show_panel()
        else:
            self.panel.withdraw()

    def show_panel(self):
        self.panel.update_idletasks()
        width = max(round(360 * self.panel.winfo_fpixels("1i") / 96),
                    self.panel.winfo_reqwidth())
        height = self.panel.winfo_reqheight()
        bar = rect(self.bar)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        if self.anchor and bar:
            x = max(8, min(self.anchor[0], screen_width - width - 8))
            y = bar.bottom + 8 if bar.top < screen_height // 2 else bar.top - height - 8
        else:
            x = 8
            if bar and bar.top < screen_height // 2:
                y = bar.bottom + 8
            else:
                y = (bar.top if bar else screen_height) - height - 8
        self.panel.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        self.panel.deiconify()
        self.panel.update_idletasks()
        round_window(wrapper(self.panel), width, height,
                     round(16 * self.panel.winfo_fpixels("1i") / 96))
        self.panel.lift()
        self.rooms_field.focus_force()

    def close(self):
        self.stop()
        self.save()
        self.root.destroy()


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
        root = tk.Tk()
        root.withdraw()
        root.report_callback_exception = lambda *error: logging.error("UI callback failed", exc_info=error)
        radio = Radio(root)
        radio.show_panel()

        def check_launcher():
            if wait_event(handle, 0) == 0:
                radio.show_panel()
                logging.info("Panel reopened by launcher")
            root.after(150, check_launcher)

        check_launcher()
        try:
            root.mainloop()
        finally:
            if radio.session:
                radio.session.close()
    finally:
        close_handle(handle)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Could not start the taskbar widget")
        raise
