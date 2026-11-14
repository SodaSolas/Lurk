"""Bilibili web QR login and read-only followed-live requests."""
import ctypes as ct
from ctypes import wintypes as wt
import json
import os

import requests

QR_ENDPOINT = "https://passport.bilibili.com/x/passport-login/web/qrcode"
FOLLOWED_ENDPOINT = "https://api.live.bilibili.com/xlive/web-ucenter/v1/xfetter/GetWebList"
COOKIE_NAMES = ("SESSDATA", "DedeUserID", "DedeUserID__ckMd5", "bili_jct")
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/",
           "Origin": "https://live.bilibili.com"}


class AccountError(Exception):
    pass


class LoginExpired(AccountError):
    pass


def request(url, params=None, cookies=None):
    try:
        with requests.get(url, params=params, cookies=cookies, headers=HEADERS,
                          timeout=(4, 6), allow_redirects=False) as response:
            if response.status_code != 200:
                raise AccountError(f"B 站请求失败（HTTP {response.status_code}）")
            result = response.json()
            code = result["code"]
            if code in (-101, -111):
                raise LoginExpired("登录已失效，请重新扫码")
            if code != 0:
                raise AccountError(f"B 站请求失败（{code}）")
            return result["data"], {
                cookie.name: cookie.value for cookie in response.cookies
                if cookie.name in COOKIE_NAMES and cookie.domain.lstrip(".") in (
                    "bilibili.com", "passport.bilibili.com",
                )
            }
    except (requests.RequestException, ValueError, TypeError, KeyError):
        raise AccountError("无法读取 B 站响应，请稍后重试") from None


def generate_qr():
    data, _ = request(QR_ENDPOINT + "/generate")
    return data["url"], data["qrcode_key"]


def poll_qr(key):
    data, cookies = request(QR_ENDPOINT + "/poll", {"qrcode_key": key})
    if data["code"] == 0 and not cookies.get("SESSDATA"):
        raise AccountError("B 站未返回登录凭据，请重新扫码")
    return data["code"], cookies


def followed_live(cookies):
    data, _ = request(FOLLOWED_ENDPOINT, {"hit_ab": "false"}, cookies)
    if not isinstance(data, dict) or not ("list" in data or "rooms" in data):
        raise AccountError("关注直播接口返回了未知格式")
    rooms = {}
    for item in data.get("list") or data.get("rooms") or []:
        room = str(item.get("room_id") or item.get("roomid") or "")
        if item.get("live_status") not in (1, "1") or not room.isascii() or not room.isdecimal():
            continue
        rooms[room] = {
            "uname": str(item.get("uname") or item.get("nickname") or room),
            "title": str(item.get("title") or item.get("roomname") or ""),
            "live_status": 1,
            "parent_area_name": item.get("area_v2_parent_name"),
            "area_name": item.get("area_v2_name"),
        }
    return rooms


class Blob(ct.Structure):
    _fields_ = [("size", wt.DWORD), ("data", ct.c_void_p)]


def protect(data, encrypt):
    """Use Windows DPAPI with the current user's context."""
    crypt = ct.WinDLL("crypt32", use_last_error=True)
    buffer = ct.create_string_buffer(data)
    source = Blob(len(data), ct.addressof(buffer))
    target = Blob()
    operation = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    operation.argtypes = [ct.POINTER(Blob), ct.c_void_p, ct.c_void_p,
                          ct.c_void_p, ct.c_void_p, wt.DWORD, ct.POINTER(Blob)]
    operation.restype = wt.BOOL
    if not operation(ct.byref(source), None, None, None, None, 1, ct.byref(target)):
        raise ct.WinError(ct.get_last_error())
    try:
        return ct.string_at(target.data, target.size)
    finally:
        free = ct.WinDLL("kernel32").LocalFree
        free.argtypes = [ct.c_void_p]
        free.restype = ct.c_void_p
        free(target.data)


def save_session(path, cookies):
    temp = path.with_suffix(".tmp")
    temp.write_bytes(protect(json.dumps(cookies).encode(), encrypt=True))
    os.replace(temp, path)


def load_session(path):
    cookies = json.loads(protect(path.read_bytes(), encrypt=False))
    if not isinstance(cookies, dict) or not isinstance(cookies.get("SESSDATA"), str):
        raise ValueError("Invalid Bilibili session")
    return {key: cookies[key] for key in COOKIE_NAMES if isinstance(cookies.get(key), str)}
