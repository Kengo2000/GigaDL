"""
OSごとの違いをここに集約（Mac / Windows / Linux）
"""
import os
import subprocess
import sys
from pathlib import Path

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")

APP_NAME = "GigaDL"
APP_VERSION = "1.3.4"
APP_DATE = "2026-11-24"
FILE_MANAGER = "Finder" if IS_MAC else ("エクスプローラー" if IS_WIN else "ファイルマネージャ")

_NO_WINDOW = 0x08000000 if IS_WIN else 0   # CREATE_NO_WINDOW


def data_dir() -> Path:
    if IS_MAC:
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    elif IS_WIN:
        d = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    else:
        d = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_download_dir() -> str:
    return str(Path.home() / "Downloads")


# ---------------------------------------------------------------- 通知

_tray = None


def set_tray(tray):
    """Windows/Linux ではタスクトレイのアイコンから通知を出す"""
    global _tray
    _tray = tray


def notify(title, text, error=False):
    if IS_MAC:
        esc = lambda s: str(s).replace("\\", "\\\\").replace('"', '\\"')
        sound = "Basso" if error else "Glass"
        script = (f'display notification "{esc(text)}" with title "{esc(title)}" '
                  f'sound name "{sound}"')
        try:
            subprocess.Popen(["osascript", "-e", script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return
    if _tray is not None:
        try:
            from PySide6.QtWidgets import QSystemTrayIcon
            icon = QSystemTrayIcon.Warning if error else QSystemTrayIcon.Information
            _tray.showMessage(title, text, icon, 8000)
            return
        except Exception:
            pass
    if not IS_WIN:
        try:
            subprocess.Popen(["notify-send", title, text])
        except Exception:
            pass


# ---------------------------------------------------------------- ファイルを開く

def open_folder(path):
    path = str(path)
    if IS_MAC:
        subprocess.Popen(["open", path])
    elif IS_WIN:
        os.startfile(path)  # noqa
    else:
        subprocess.Popen(["xdg-open", path])


def reveal(path):
    """ファイルを選択した状態でフォルダを開く"""
    p = Path(path)
    if not p.exists():
        if p.parent.exists():
            open_folder(p.parent)
        return
    if IS_MAC:
        subprocess.Popen(["open", "-R", str(p)])
    elif IS_WIN:
        subprocess.Popen(f'explorer /select,"{p}"', creationflags=_NO_WINDOW)
    else:
        open_folder(p.parent)


# ---------------------------------------------------------------- スリープ防止

class KeepAwake:
    """ダウンロード中だけスリープを防ぐ（画面オフはOS任せ）"""

    def __init__(self):
        self.on = False
        self.proc = None

    def set(self, on):
        if on == self.on:
            return
        self.on = on
        try:
            if IS_MAC:
                if on:
                    self.proc = subprocess.Popen(["caffeinate", "-i"])
                elif self.proc:
                    self.proc.terminate()
                    self.proc = None
            elif IS_WIN:
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
                ctypes.windll.kernel32.SetThreadExecutionState(flags)
        except Exception:
            pass
