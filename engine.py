"""
GigaDL エンジン部
ギガファイル便のページ解析（fireattack/gfile のロジックを参考）と、
Rangeリクエストによるレジューム・自動再接続付きダウンロードを行う。
GUIには依存しない。
"""
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PRIMARY_DOMAIN = "gigafile.jp"
URL_RE = re.compile(r"^https?://(\d+?)\.gigafile\.(?:jp|nu)/([a-z0-9-]+)/?$", re.I)
PART_EXT = ".gdlpart"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# 状態
QUEUED, RUNNING, PAUSED, DONE, ERROR, RETRYING, MOVING = (
    "待機中", "ダウンロード中", "一時停止", "完了", "エラー", "再接続中", "移動中")

MAX_RETRIES = 30          # 回線断などでの自動再接続の上限
CHUNK = 1024 * 1024       # 1MB


class GigaError(Exception):
    pass


class KeyRequired(GigaError):
    pass


# ---------------------------------------------------------------- 解析

def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    return re.sub(r"\.gigafile\.nu\b", f".{PRIMARY_DOMAIN}", url)


def is_gigafile_url(url: str) -> bool:
    return bool(URL_RE.match(url.strip()))


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().strip(".")
    # Windowsの予約名（CON, NUL など）を避ける
    if re.match(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", name, re.I):
        name = "_" + name
    if len(name) > 200:   # 長すぎる名前は拡張子を残して短縮
        stem, dot, ext = name.rpartition(".")
        name = (stem[:190] + dot + ext) if dot and len(ext) <= 8 else name[:200]
    return name or "download"


def _real_filename(session, server_id, file_id, fallback):
    try:
        api = (f"https://{server_id}.{PRIMARY_DOMAIN}/get_uploaded_file_name_jx.php"
               f"?file={file_id}&_={int(time.time()*1000)}")
        r = session.get(api, timeout=20)
        r.raise_for_status()
        name = r.json().get("filename")
        return name or fallback
    except Exception:
        return fallback


def parse_page(url: str, session=None):
    """ページを解析して [(ファイル名, サイズ文字列, file_id), ...] を返す。"""
    m = URL_RE.match(url.strip())
    if not m:
        raise GigaError("ギガファイル便のURLではありません")
    server_id, page_id = m[1], m[2]
    session = session or new_session()
    r = session.get(normalize_url(url), timeout=30)
    r.raise_for_status()
    # 文字コード未指定でも日本語ファイル名が化けないようバイト列から解析
    soup = BeautifulSoup(r.content, "html.parser", from_encoding=_encoding(r))
    files = []
    try:
        if soup.select_one("#contents_matomete"):
            for ele in soup.select(".matomete_file"):
                name = ele.select_one(".matomete_file_info > span:nth-child(2)").text.strip()
                fid = re.search(r"download\(\d+, *'(.+?)'",
                                ele.select_one(".download_panel_btn_dl")["onclick"])[1]
                sm = re.search(r"（(.+?)）",
                               ele.select_one(".matomete_file_info > span:nth-child(3)").text)
                size = sm[1] if sm else ""
                if "*" in name:
                    name = _real_filename(session, server_id, fid, name)
                files.append((name, size, fid))
        else:
            size = soup.select_one(".dl_size").text.strip()
            name = soup.select_one("#dl").text.strip()
            if "*" in name:
                name = _real_filename(session, server_id, page_id, name)
            files.append((name, size, page_id))
    except Exception:
        files = []
    if not files:
        raise GigaError("ページを解析できませんでした（期限切れ・削除済み、またはページ構成の変更）")
    return files


def _encoding(r):
    ct = r.headers.get("Content-Type", "").lower()
    return None if "charset=" in ct and "iso-8859-1" not in ct else "utf-8"


def parse_total(content_range: str):
    m = re.search(r"/(\d+)\s*$", content_range or "")
    return int(m[1]) if m else None


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 1
    while True:
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
        i += 1


# ---------------------------------------------------------------- タスク

class Task:
    FIELDS = ("id", "page_url", "file_id", "name", "size_str", "key",
              "dest_dir", "status", "total", "done_bytes", "message", "final_path")

    def __init__(self, page_url, file_id, name, size_str, key, dest_dir):
        self.id = uuid.uuid4().hex
        self.page_url = normalize_url(page_url)
        self.file_id = file_id
        self.name = sanitize(name)
        self.size_str = size_str
        self.key = key or ""
        self.dest_dir = str(dest_dir)
        self.status = QUEUED
        self.total = 0
        self.done_bytes = 0
        self.message = ""
        self.final_path = ""
        self.speed = 0.0
        self._stop = threading.Event()
        self._thread = None

    # --- 保存/復元
    def to_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    @classmethod
    def from_dict(cls, d):
        t = cls.__new__(cls)
        for k in cls.FIELDS:
            setattr(t, k, d.get(k, "" if k not in ("total", "done_bytes") else 0))
        t.speed = 0.0
        t._stop = threading.Event()
        t._thread = None
        if t.status in (RUNNING, RETRYING, MOVING):
            t.status = PAUSED
        return t

    @property
    def part_path(self) -> Path:
        return Path(self.dest_dir) / (self.name + PART_EXT)

    @property
    def download_url(self):
        base = self.page_url.rsplit("/", 1)[0]
        url = f"{base}/download.php?file={self.file_id}"
        if self.key:
            url += f"&dlkey={self.key}"
        return url


# ---------------------------------------------------------------- マネージャ

class Manager:
    """
    listener(event, task) をワーカースレッドから呼ぶ。
    event: "update" | "done" | "error" | "all_done" | "added" | "resolve_error"
    """

    def __init__(self, state_path: Path, listener=None, max_parallel=2):
        self.state_path = Path(state_path)
        self.listener = listener or (lambda e, t: None)
        self.max_parallel = max_parallel
        self.tasks: list[Task] = []
        self.lock = threading.RLock()
        self.settings = {"dest_dir": str(Path.home() / "Downloads"),
                         "max_parallel": max_parallel, "auto_resume": True}
        self._had_activity = False
        self._load()

    # --- 永続化
    def _load(self):
        try:
            d = json.loads(self.state_path.read_text("utf-8"))
            self.settings.update(d.get("settings", {}))
            self.tasks = [Task.from_dict(x) for x in d.get("tasks", [])]
            self.max_parallel = int(self.settings.get("max_parallel", 2))
        except FileNotFoundError:
            pass
        except Exception:
            pass
        for t in self.tasks:
            if t.status == PAUSED and t.part_path.exists():
                t.done_bytes = t.part_path.stat().st_size

    def save(self):
        with self.lock:
            d = {"settings": self.settings, "tasks": [t.to_dict() for t in self.tasks]}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(f".{self.state_path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
            os.replace(tmp, self.state_path)

    def startup(self):
        """起動時：自動再開が有効なら中断分をキューに戻す"""
        if self.settings.get("auto_resume", True):
            with self.lock:
                for t in self.tasks:
                    if t.status == PAUSED:
                        t.status = QUEUED
            self._schedule()

    # --- 追加
    def add_url(self, url, key, dest_dir):
        """URLを解析してタスクを追加（別スレッド）"""
        def work():
            try:
                files = parse_page(url)
            except Exception as ex:
                self.listener("resolve_error", (url, str(ex)))
                return
            with self.lock:
                for name, size, fid in files:
                    dup = next((t for t in self.tasks if t.file_id == fid
                                and t.status != DONE), None)
                    if dup:
                        continue
                    t = Task(url, fid, name, size, key, dest_dir)
                    self.tasks.append(t)
                    self.listener("added", t)
            self.save()
            self._schedule()
        threading.Thread(target=work, daemon=True).start()

    # --- 操作
    def pause(self, task: Task):
        with self.lock:
            if task.status in (RUNNING, RETRYING):
                task._stop.set()
            elif task.status == QUEUED:
                task.status = PAUSED
                self.listener("update", task)
        self.save()

    def resume(self, task: Task, key=None):
        with self.lock:
            if key is not None:
                task.key = key
            if task.status in (PAUSED, ERROR):
                task.status = QUEUED
                task.message = ""
                self.listener("update", task)
        self.save()
        self._schedule()

    def remove(self, task: Task, delete_part=True):
        if task.status in (RUNNING, RETRYING):
            task._stop.set()
            if task._thread:
                task._thread.join(timeout=10)
        with self.lock:
            if task in self.tasks:
                self.tasks.remove(task)
        if delete_part and task.status != DONE:
            try:
                task.part_path.unlink()
            except FileNotFoundError:
                pass
        self.save()
        self._schedule()

    def change_dest(self, task: Task, new_dir):
        """保存先を変更（途中データがあれば新しいフォルダへ移動して続きから）"""
        new_dir = str(new_dir)

        def work():
            with self.lock:
                if task.status in (DONE, MOVING) or new_dir == task.dest_dir:
                    return
                was_active = task.status in (RUNNING, RETRYING, QUEUED)
                if task.status not in (RUNNING, RETRYING):
                    task.status = MOVING     # 待機中に勝手に始まらないよう先に押さえる
            if task.status in (RUNNING, RETRYING):
                task._stop.set()
                if task._thread:
                    task._thread.join(timeout=60)
                with self.lock:
                    if task.status in (RUNNING, RETRYING):
                        return   # 止まらなかった（通常起きない）
                    task.status = MOVING
            old_part = task.part_path
            new_part = Path(new_dir) / old_part.name
            try:
                Path(new_dir).mkdir(parents=True, exist_ok=True)
                if old_part.exists():
                    task.message = "途中データを移動中…"
                    self.listener("update", task)
                    if new_part.exists():
                        new_part.unlink()
                    shutil.move(str(old_part), str(new_part))
                task.dest_dir = new_dir
                task.message = ""
                task.status = QUEUED if was_active else PAUSED
            except Exception as ex:
                task.status, task.message = ERROR, f"保存先を変更できませんでした: {ex}"
            self.save()
            self.listener("update", task)
            self._schedule()

        threading.Thread(target=work, daemon=True).start()

    def clear_done(self):
        with self.lock:
            self.tasks = [t for t in self.tasks if t.status != DONE]
        self.save()

    def pause_all(self):
        with self.lock:
            for t in self.tasks:
                if t.status in (RUNNING, RETRYING, QUEUED):
                    self.pause(t)

    def resume_all(self):
        with self.lock:
            for t in self.tasks:
                if t.status in (PAUSED, ERROR):
                    t.status = QUEUED
                    t.message = ""
                    self.listener("update", t)
        self.save()
        self._schedule()

    def shutdown(self):
        with self.lock:
            running = [t for t in self.tasks if t.status in (RUNNING, RETRYING)]
            for t in running:
                t._stop.set()
        for t in running:
            if t._thread:
                t._thread.join(timeout=5)
        self.save()

    # --- スケジューラ
    def _schedule(self):
        with self.lock:
            active = sum(1 for t in self.tasks if t.status in (RUNNING, RETRYING))
            for t in self.tasks:
                if active >= self.max_parallel:
                    break
                if t.status == QUEUED:
                    t.status = RUNNING
                    t._stop.clear()
                    t._thread = threading.Thread(target=self._run, args=(t,), daemon=True)
                    t._thread.start()
                    active += 1
                    self._had_activity = True
            busy = any(t.status in (RUNNING, RETRYING, QUEUED) for t in self.tasks)
        if not busy and self._had_activity:
            self._had_activity = False
            self.listener("all_done", None)

    def _run(self, t: Task):
        self.listener("update", t)
        retries = 0
        while True:
            try:
                self._download(t)
                break
            except KeyRequired as ex:
                t.status, t.message = ERROR, str(ex)
                break
            except GigaError as ex:
                t.status, t.message = ERROR, str(ex)
                break
            except (requests.exceptions.RequestException, OSError) as ex:
                if t._stop.is_set():
                    break
                if isinstance(ex, OSError) and not isinstance(
                        ex, requests.exceptions.RequestException) and ex.errno == 28:
                    t.status, t.message = ERROR, "ディスクの空き容量が足りません"
                    break
                retries += 1
                if retries > MAX_RETRIES:
                    t.status, t.message = ERROR, f"接続エラーが続いたため中止: {ex}"
                    break
                wait = min(30, 2 ** min(retries, 5))
                t.status = RETRYING
                t.message = f"{retries}回目の再接続まで{wait}秒"
                t.speed = 0
                self.listener("update", t)
                if t._stop.wait(wait):
                    break
                t.status = RUNNING
                t.message = ""
                continue
        if t._stop.is_set() and t.status in (RUNNING, RETRYING):
            t.status = PAUSED
            t.message = ""
        t.speed = 0
        self.save()
        if t.status == DONE:
            self.listener("done", t)
        elif t.status == ERROR:
            self.listener("error", t)
        else:
            self.listener("update", t)
        self._schedule()

    def _download(self, t: Task):
        Path(t.dest_dir).mkdir(parents=True, exist_ok=True)
        s = new_session()
        # ページを開いてクッキーを取得
        s.get(t.page_url, timeout=30).raise_for_status()

        part = t.part_path
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Referer": t.page_url}
        if offset:
            headers["Range"] = f"bytes={offset}-"

        with s.get(t.download_url, headers=headers, stream=True,
                   timeout=(20, 60)) as r:
            if r.status_code == 416 and offset and t.total and offset >= t.total:
                return self._finish(t)
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "").lower()
            if "text/html" in ctype:
                if t.key:
                    raise KeyRequired("パスワードが違うか、ファイルの保持期限が切れています")
                raise KeyRequired("パスワード（ダウンロードキー）が必要です")

            if r.status_code == 206:
                total = parse_total(r.headers.get("Content-Range"))
                mode = "ab"
            else:  # 200: Range非対応 or 新規
                total = int(r.headers.get("Content-Length") or 0) or None
                offset = 0
                mode = "wb"
            if total:
                t.total = total
            t.done_bytes = offset
            self.save()

            last_emit = last_t = time.monotonic()
            last_b = offset
            with open(part, mode) as f:
                for chunk in r.iter_content(chunk_size=CHUNK):
                    if t._stop.is_set():
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    t.done_bytes += len(chunk)
                    now = time.monotonic()
                    if now - last_emit >= 0.3:
                        inst = (t.done_bytes - last_b) / max(now - last_t, 1e-6)
                        t.speed = inst if not t.speed else t.speed * 0.7 + inst * 0.3
                        last_b, last_t, last_emit = t.done_bytes, now, now
                        self.listener("update", t)

        size = part.stat().st_size
        if t.total and size < t.total:
            # 途中で切れた → 例外扱いにして自動再開させる
            raise requests.exceptions.ChunkedEncodingError(
                f"途中で切断されました ({size}/{t.total})")
        if t.total and size > t.total:
            part.unlink()
            raise GigaError("ファイルサイズが一致しません（最初からやり直してください）")
        if not t.total:
            t.total = size
        return self._finish(t)

    def _finish(self, t: Task):
        final = unique_path(Path(t.dest_dir) / t.name)
        os.replace(t.part_path, final)
        t.final_path = str(final)
        t.done_bytes = t.total
        t.status = DONE
        t.message = ""


def human(n: float) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
