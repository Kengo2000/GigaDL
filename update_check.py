"""
新しいバージョンのお知らせ

GitHub に置いた version.json（最新の版番号・日付・内容・ダウンロード場所）を読み、
今の版より新しければ知らせる。通信に失敗しても何もしない（アプリの動作には影響しない）。
"""
import re

import requests

VERSION_URL = "https://raw.githubusercontent.com/Kengo2000/GigaDL/main/version.json"


def parse_version(v) -> tuple:
    """'1.3.10' → (1, 3, 10)。数字以外は無視する"""
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def is_newer(latest, current) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a) and a > b


def fetch_latest(timeout=10, session=None):
    """version.json を読んで dict を返す。読めなければ None"""
    try:
        s = session or requests
        r = s.get(VERSION_URL, timeout=timeout, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        info = r.json()
        if not isinstance(info, dict) or not info.get("version"):
            return None
        url = str(info.get("url") or "")
        if not url.startswith("https://"):       # 開くのは https のページだけ
            info["url"] = ""
        return info
    except Exception:
        return None


def check(current, skip=""):
    """新しい版があり、かつ「この版は知らせない」にしていなければ情報を返す"""
    info = fetch_latest()
    if not info:
        return None
    v = str(info["version"])
    if not is_newer(v, current) or v == skip:
        return None
    return info
