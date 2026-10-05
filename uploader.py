"""
GigaDL アップロード部
ギガファイル便の公式アップローダー（gfupload / upload.js）と同じ手順で送る:
  1. トップページから送信先サーバーを取得（var server = "NN.gigafile.nu"）
  2. ファイルを100MBずつに分け、同じアップロードIDで 0,1,2… の順に upload_chunk.php へ送る
  3. 最後の分の応答に URL / filename / delkey が入る
  4. パスワードは set_dlkey.php で後から設定（半角英数1〜4文字）
  5. 送ったファイルのサイズをダウンロード側から確認
途中で切れたら同じ分を送り直し、アプリを閉じても続きの分から再開する。
"""
import json
import os
import re
import shutil
import threading
import unicodedata
import zipfile
import time
import uuid
from pathlib import Path

import requests

import engine

TOP_URL = "https://gigafile.nu/"
UP_CHUNK = 100 * 1024 * 1024        # 公式と同じ100MB
LIFETIMES = (3, 5, 7, 14, 30, 60, 100)
MAX_RETRIES = 30
DLKEY_RE = re.compile(r"^[0-9A-Za-z]{1,4}$")

QUEUED, RUNNING, PAUSED, DONE, ERROR, RETRYING = (
    engine.QUEUED, engine.RUNNING, engine.PAUSED, engine.DONE, engine.ERROR, engine.RETRYING)
VERIFYING = "確認中"
ZIP_BLOCK = 4 * 1024 * 1024
_ZIP_THROTTLE = 0.0        # テスト用（ZIP作成をわざと遅くする秒数）


def zip_entries(paths):
    """ZIPに入れる (実ファイル, ZIP内の名前) の一覧。
    フォルダはフォルダ名ごと入れる。名前はWindowsでも崩れないようNFCに揃える。"""
    out = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            base = p.parent
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for f in sorted(files):
                    if f.startswith(".") or f in ("Thumbs.db", "desktop.ini"):
                        continue
                    fp = Path(root) / f
                    out.append((fp, unicodedata.normalize("NFC", fp.relative_to(base).as_posix())))
        elif p.is_file() and not p.name.startswith("."):
            out.append((p, unicodedata.normalize("NFC", p.name)))
    return out


def safe_zip_name(name):
    name = unicodedata.normalize("NFC", (name or "").strip())
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = "まとめ"
    return name if name.lower().endswith(".zip") else name + ".zip"


class UploadError(Exception):
    pass


class Stopped(Exception):
    pass


def valid_dlkey(k: str) -> bool:
    return not k or bool(DLKEY_RE.match(k))


def get_server(session) -> str:
    r = session.get(TOP_URL, timeout=30)
    r.raise_for_status()
    m = re.search(r'var\s+server\s*=\s*"([^"]+)"', r.text)
    if not m:
        raise UploadError("ギガファイル便の送信先を取得できませんでした（ページ構成の変更の可能性）")
    return m[1]


class ChunkBody:
    """multipart/form-data をファイルから少しずつ読みながら送る（メモリを食わない）"""

    def __init__(self, fields, path, start, length, on_read, stop_event):
        self.boundary = "----GigaDL" + uuid.uuid4().hex
        b = self.boundary
        head = b""
        for k, v in fields[:-1]:
            head += (f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        head += (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"blob\"\r\n"
                 f"Content-Type: application/octet-stream\r\n\r\n").encode()
        k, v = fields[-1]
        tail = (f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
                f"--{b}--\r\n").encode()
        self.head, self.tail = head, tail
        self.f = open(path, "rb")
        self.f.seek(start)
        self.remaining = length
        self.length = len(head) + length + len(tail)
        self.on_read = on_read
        self.stop = stop_event
        self.stage = 0

    @property
    def content_type(self):
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self):
        return self.length

    def read(self, n=-1):
        # 一時停止はファイルの中身を送っている途中でだけ受け付ける。
        # 最後まで送り終えた後に止めると、サーバーは受け取ったのにこちらは未送信扱いになるため。
        if self.stop.is_set() and self.stage <= 1 and self.remaining > 0:
            raise Stopped()
        if self.stage == 0:
            self.stage = 1
            return self.head
        if self.stage == 1:
            if self.remaining > 0:
                size = min(1024 * 1024 if n is None or n < 0 else max(n, 64 * 1024), self.remaining)
                data = self.f.read(size)
                if not data:
                    raise UploadError("ファイルを読み込めませんでした（途中で変更・削除された可能性）")
                self.remaining -= len(data)
                self.on_read(len(data))
                return data
            self.stage = 2
            return self.tail
        if self.stage == 2:
            self.stage = 3
            self.f.close()
            return b""
        return b""

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


class UploadTask:
    FIELDS = ("id", "path", "name", "size", "mtime", "lifetime", "dlkey", "status",
              "message", "upload_id", "server", "cookies", "next_chunk", "chunk_size",
              "url", "srv_filename", "delkey", "added_at", "finished_at", "restarted",
              "zip_sources", "zip_ready", "zip_total")

    def __init__(self, path, lifetime=7, dlkey=""):
        p = Path(path)
        st = p.stat()
        self.id = uuid.uuid4().hex
        self.path = str(p)
        self.name = p.name
        self.size = st.st_size
        self.mtime = st.st_mtime
        self.lifetime = int(lifetime)
        self.dlkey = dlkey or ""
        self.status = QUEUED
        self.message = ""
        self.upload_id = ""
        self.server = ""
        self.cookies = {}
        self.next_chunk = 0
        self.chunk_size = UP_CHUNK
        self.url = ""
        self.srv_filename = ""
        self.delkey = ""
        self.added_at = time.time()
        self.finished_at = 0
        self.restarted = False
        self.zip_sources = []      # ZIPにして送るときの元のファイル/フォルダ
        self.zip_ready = True
        self.zip_total = 0
        self._init_runtime()

    @classmethod
    def for_zip(cls, sources, zip_path, total, lifetime=7, dlkey=""):
        """複数のファイル/フォルダを1つのZIP（無圧縮）にしてから送るタスク"""
        t = cls.__new__(cls)
        t.id = uuid.uuid4().hex
        t.path = str(zip_path)
        t.name = Path(zip_path).name
        t.size = total
        t.mtime = 0
        t.lifetime = int(lifetime)
        t.dlkey = dlkey or ""
        t.status = QUEUED
        t.message = ""
        t.upload_id, t.server, t.cookies = "", "", {}
        t.next_chunk, t.chunk_size = 0, UP_CHUNK
        t.url = t.srv_filename = t.delkey = ""
        t.added_at, t.finished_at, t.restarted = time.time(), 0, False
        t.zip_sources = [str(p) for p in sources]
        t.zip_ready = False
        t.zip_total = total
        t._init_runtime()
        return t

    @property
    def is_zip(self):
        return bool(self.zip_sources)

    def _init_runtime(self):
        self.sent = self.next_chunk * self.chunk_size if self.chunk_size else 0
        self.sent = min(self.sent, self.size)
        self.speed = 0.0
        self.zip_done = 0
        self._stop = threading.Event()
        self._thread = None

    @property
    def chunks(self):
        return max(1, -(-self.size // self.chunk_size))

    def to_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    @classmethod
    def from_dict(cls, d):
        t = cls.__new__(cls)
        for k in cls.FIELDS:
            setattr(t, k, d.get(k))
        t.cookies = t.cookies or {}
        t.chunk_size = t.chunk_size or UP_CHUNK
        t.next_chunk = t.next_chunk or 0
        t.dlkey = t.dlkey or ""
        t.message = t.message or ""
        t.zip_sources = t.zip_sources or []
        t.zip_ready = True if t.zip_ready is None else t.zip_ready
        t.zip_total = t.zip_total or 0
        t._init_runtime()
        if t.status in (RUNNING, RETRYING, VERIFYING):
            t.status = PAUSED
        if t.status == DONE:
            t.sent = t.size
        return t


GROUP_WAIT = "送信待ち"
GROUP_MAKING = "まとめ中"
BAD_NAME = re.compile(r'[\\/:,;*?"<>|]')


class Group:
    """複数ファイルを1つのURLにまとめたもの（ギガファイル便の「まとめてダウンロード」）"""
    FIELDS = ("id", "name", "member_ids", "dlkey", "lifetime", "status", "message",
              "url", "delkey", "file_name", "created", "verified")

    def __init__(self, member_ids, name="", dlkey="", lifetime=7):
        self.id = uuid.uuid4().hex
        self.name = name or ""
        self.member_ids = list(member_ids)
        self.dlkey = dlkey or ""
        self.lifetime = int(lifetime)
        self.status = GROUP_WAIT
        self.message = ""
        self.url = ""
        self.delkey = ""
        self.file_name = ""
        self.created = time.time()
        self.verified = False

    def to_dict(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    @classmethod
    def from_dict(cls, d):
        g = cls.__new__(cls)
        for k in cls.FIELDS:
            setattr(g, k, d.get(k))
        g.member_ids = g.member_ids or []
        g.name, g.dlkey, g.message = g.name or "", g.dlkey or "", g.message or ""
        g.url, g.delkey, g.file_name = g.url or "", g.delkey or "", g.file_name or ""
        if g.status == GROUP_MAKING:
            g.status = GROUP_WAIT
        return g

    @property
    def title(self):
        return self.file_name or self.name or "まとめ"


class UploadManager:
    """
    listener(event, task): "up_added" | "up_update" | "up_done" | "up_error" | "up_all_done"
    """

    def __init__(self, state_path, listener=None, max_parallel=1):
        self.state_path = Path(state_path)
        self.listener = listener or (lambda e, t: None)
        self.max_parallel = max_parallel
        self.tasks: list[UploadTask] = []
        self.groups: list[Group] = []
        self.lock = threading.RLock()
        self.settings = {"lifetime": 7, "auto_resume": True}
        self._had_activity = False
        self._load()

    # --- 永続化
    def _load(self):
        try:
            d = json.loads(self.state_path.read_text("utf-8"))
            self.settings.update(d.get("settings", {}))
            self.tasks = [UploadTask.from_dict(x) for x in d.get("tasks", [])]
            self.groups = [Group.from_dict(x) for x in d.get("groups", [])]
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def save(self):
        with self.lock:
            d = {"settings": self.settings, "tasks": [t.to_dict() for t in self.tasks],
                 "groups": [g.to_dict() for g in self.groups]}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(f".{self.state_path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
            os.replace(tmp, self.state_path)

    def startup(self):
        if self.settings.get("auto_resume", True):
            with self.lock:
                for t in self.tasks:
                    if t.status == PAUSED:
                        t.status = QUEUED
            self._schedule()
        self.check_groups()

    # --- 追加
    @staticmethod
    def expand(paths):
        """ファイル/フォルダのリストを、送るファイルの一覧に展開（隠しファイルは除く）"""
        out = []
        for p in paths:
            p = Path(p)
            if p.is_dir():
                for root, dirs, files in os.walk(p):
                    dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                    for f in sorted(files):
                        if not f.startswith(".") and f not in ("Thumbs.db", "desktop.ini"):
                            out.append(Path(root) / f)
            elif p.is_file() and not p.name.startswith("."):
                out.append(p)
        return out

    def add_files(self, paths, lifetime, dlkey="", group=False, group_name=""):
        """group=True なら、全部送り終わったら自動で1つのURLにまとめる"""
        if not valid_dlkey(dlkey):
            raise UploadError("パスワードは半角英数1〜4文字です")
        if group_name and BAD_NAME.search(group_name):
            raise UploadError('まとめの名前に次の文字は使えません: \\ / : , ; * ? " < > |')
        added = []
        with self.lock:
            for p in self.expand(paths):
                try:
                    if p.stat().st_size == 0:
                        continue
                except OSError:
                    continue
                t = UploadTask(p, lifetime, dlkey)
                self.tasks.append(t)
                added.append(t)
                self.listener("up_added", t)
            if group and len(added) >= 2:
                g = Group([t.id for t in added], group_name, dlkey, lifetime)
                self.groups.append(g)
                self.listener("up_group", g)
        self.save()
        self._schedule()
        return added

    # --- ZIPにして送る
    def zip_dir(self):
        d = Path(self.settings.get("zip_dir") or (self.state_path.parent / "ZIP作業"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    def add_zip(self, paths, name, lifetime, dlkey=""):
        """ファイル/フォルダを1つのZIP（無圧縮）にしてから送る"""
        if not valid_dlkey(dlkey):
            raise UploadError("パスワードは半角英数1〜4文字です")
        entries = zip_entries(paths)
        if not entries:
            raise UploadError("ZIPに入れるファイルがありません")
        total = sum(fp.stat().st_size for fp, _ in entries)
        d = self.zip_dir()
        self._check_space(d, total + 1024 * len(entries))
        zp = engine.unique_path(d / safe_zip_name(name))
        zp.touch()          # 名前を確保（同時に同じ名前を使わないように）
        t = UploadTask.for_zip(paths, zp, total, lifetime, dlkey)
        with self.lock:
            self.tasks.append(t)
            self.listener("up_added", t)
        self.save()
        self._schedule()
        return t

    @staticmethod
    def _check_space(d, need):
        free = shutil.disk_usage(d).free
        if free < need + 200 * 1024 * 1024:
            raise UploadError(f"ZIPの作業場所（{d}）の空きが足りません。"
                              f"必要 {engine.human(need)}・空き {engine.human(free)}。"
                              "「ZIPの作業場所」を空きのあるドライブに変えてください")

    def _build_zip(self, t):
        entries = zip_entries(t.zip_sources)
        if not entries:
            raise UploadError("ZIPに入れる元のファイルが見つかりません（移動・削除された可能性）")
        zp = Path(t.path)
        try:
            total = sum(fp.stat().st_size for fp, _ in entries)
            self._check_space(zp.parent, total + 1024 * len(entries))
        except OSError as ex:
            raise UploadError(f"ZIPを作れませんでした: {ex}")
        tmp = zp.with_name(zp.name + ".part")
        t.zip_total, t.zip_done = total, 0
        t.message = "ZIP作成中"
        self.listener("up_update", t)
        last = [time.monotonic()]
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for fp, arc in entries:
                    zi = zipfile.ZipInfo.from_file(fp, arc)
                    zi.compress_type = zipfile.ZIP_STORED
                    with open(fp, "rb") as src, z.open(zi, "w") as dst:
                        while True:
                            if t._stop.is_set():
                                raise Stopped()
                            b = src.read(ZIP_BLOCK)
                            if not b:
                                break
                            dst.write(b)
                            t.zip_done += len(b)
                            if _ZIP_THROTTLE:
                                time.sleep(_ZIP_THROTTLE)
                            now = time.monotonic()
                            if now - last[0] >= 0.3:
                                last[0] = now
                                t.message = f"ZIP作成中 {t.zip_done * 100 // max(total, 1)}%"
                                self.listener("up_update", t)
            os.replace(tmp, zp)
        except Stopped:
            _unlink(tmp)
            raise
        except OSError as ex:
            _unlink(tmp)
            raise UploadError(f"ZIPを作れませんでした: {ex}")
        st = zp.stat()
        t.size, t.mtime = st.st_size, st.st_mtime
        t.zip_ready = True
        t.next_chunk, t.sent, t.upload_id = 0, 0, ""
        t.message = ""
        self.save()
        self.listener("up_update", t)

    # --- まとめ
    def members(self, g):
        by_id = {t.id: t for t in self.tasks}
        return [by_id[i] for i in g.member_ids if i in by_id]

    def group_of(self, t):
        return next((g for g in self.groups if t.id in g.member_ids), None)

    def make_group(self, tasks, name="", dlkey=""):
        """送信済み（または送信中）のファイルを選んで、後からまとめる"""
        tasks = [t for t in tasks if t.status != ERROR]
        if len(tasks) < 2:
            raise UploadError("まとめるには2つ以上のファイルを選んでください")
        if not valid_dlkey(dlkey):
            raise UploadError("パスワードは半角英数1〜4文字です")
        if name and BAD_NAME.search(name):
            raise UploadError('まとめの名前に次の文字は使えません: \\ / : , ; * ? " < > |')
        if any(t.dlkey for t in tasks) and not dlkey:
            raise UploadError("パスワード付きのファイルが含まれているので、まとめにもパスワードが必要です（ギガファイル便の決まり）")
        g = Group([t.id for t in tasks], name, dlkey, min(t.lifetime for t in tasks))
        with self.lock:
            self.groups.append(g)
        self.listener("up_group", g)
        self.save()
        self.check_groups()
        return g

    def remove_group(self, g):
        with self.lock:
            if g in self.groups:
                self.groups.remove(g)
        self.save()

    def retry_group(self, g):
        if g.status == ERROR:
            g.status, g.message = GROUP_WAIT, ""
            self.save()
            self.listener("up_group", g)
            self.check_groups()

    def check_groups(self):
        """メンバーが全部送り終わったグループを、まとめURLにする"""
        ready = []
        with self.lock:
            for g in self.groups:
                if g.status != GROUP_WAIT:
                    continue
                ms = self.members(g)
                if len(ms) < 2:
                    g.status, g.message = ERROR, "まとめるファイルが2つ未満になりました"
                    self.listener("up_group", g)
                    continue
                done = sum(1 for t in ms if t.status == DONE)
                msg = f"{done}/{len(ms)} 送信済み"
                if msg != g.message:
                    g.message = msg
                    self.listener("up_group", g)
                if done == len(ms):
                    g.status, g.message = GROUP_MAKING, ""
                    ready.append(g)
        for g in ready:
            threading.Thread(target=self._make_matomete, args=(g,), daemon=True).start()

    def _make_matomete(self, g):
        self.listener("up_group", g)
        ms = self.members(g)
        try:
            session = requests_session_for(ms[0])
            server = ms[0].server or get_server(session)
            data = {"urls[]": [t.url for t in ms], "dlkeys[]": [t.dlkey for t in ms],
                    "file_name": g.name, "dlkey": g.dlkey, "lifetime": str(g.lifetime)}
            r = session.post(f"https://{server}/matomete_get_url.php", data=data, timeout=60,
                             headers={"Origin": "https://gigafile.nu", "Referer": TOP_URL,
                                      "X-Requested-With": "XMLHttpRequest"})
            r.raise_for_status()
            js = r.json()
            if js.get("status") != 0 or not js.get("url"):
                raise UploadError("まとめURLの発行に失敗しました（ギガファイル便の応答: "
                                  f"{js.get('message') or js.get('status')}）")
            g.url, g.delkey = js["url"], js.get("delkey", "")
            g.file_name = js.get("file_name", "") or g.name
            g.message = ""
            # 確認：まとめページに全ファイルが並んでいるか（確認が終わってから「完了」にする）
            try:
                files = engine.parse_page(g.url, session)
                g.verified = len(files) == len(ms)
                if not g.verified:
                    g.message = f"まとめページのファイル数が {len(files)}/{len(ms)} です"
            except Exception:
                g.verified = False
                g.message = "まとめページを確認できませんでした（URLは発行済み）"
            g.status = DONE
        except Exception as ex:
            g.status, g.message = ERROR, str(ex) if isinstance(ex, UploadError) else f"まとめURLの発行に失敗しました: {ex}"
        self.save()
        self.listener("up_group_done" if g.status == DONE else "up_group_error", g)

    # --- 操作
    def pause(self, t):
        with self.lock:
            if t.status in (RUNNING, RETRYING):
                t._stop.set()
            elif t.status == QUEUED:
                t.status = PAUSED
                self.listener("up_update", t)
        self.save()

    def resume(self, t):
        with self.lock:
            if t.status in (PAUSED, ERROR):
                t.status = QUEUED
                t.message = ""
                self.listener("up_update", t)
        self.save()
        self._schedule()

    def set_dlkey(self, t, key):
        """完了前なら予約、完了後ならその場で設定（別スレッド）"""
        if not valid_dlkey(key):
            raise UploadError("パスワードは半角英数1〜4文字です")
        t.dlkey = key
        self.save()
        if t.status == DONE and key:
            def work():
                try:
                    self._apply_dlkey(t, requests_session_for(t))
                    t.message = "パスワードを設定しました"
                except Exception as ex:
                    t.message = f"パスワードを設定できませんでした: {ex}"
                self.save()
                self.listener("up_update", t)
            threading.Thread(target=work, daemon=True).start()

    def remove(self, t):
        if t.status in (RUNNING, RETRYING, VERIFYING):
            t._stop.set()
            if t._thread:
                t._thread.join(timeout=15)
        if t.is_zip:            # 作業用ZIPは片付ける（元のファイルは消さない）
            _unlink(Path(t.path))
            _unlink(Path(t.path + ".part"))
        with self.lock:
            if t in self.tasks:
                self.tasks.remove(t)
        self.save()
        self._schedule()
        self.check_groups()

    def clear_done(self):
        with self.lock:
            # まだまとめ待ちのグループに入っているファイルは残す
            keep = {i for g in self.groups if g.status in (GROUP_WAIT, GROUP_MAKING) for i in g.member_ids}
            self.tasks = [t for t in self.tasks if t.status != DONE or t.id in keep]
        self.save()

    def pause_all(self):
        for t in list(self.tasks):
            if t.status in (RUNNING, RETRYING, QUEUED):
                self.pause(t)

    def resume_all(self):
        for t in list(self.tasks):
            if t.status in (PAUSED, ERROR):
                self.resume(t)

    def shutdown(self):
        with self.lock:
            running = [t for t in self.tasks if t.status in (RUNNING, RETRYING, VERIFYING)]
            for t in running:
                t._stop.set()
        for t in running:
            if t._thread:
                t._thread.join(timeout=15)
        self.save()

    def busy(self):
        return any(t.status in (RUNNING, RETRYING, QUEUED, VERIFYING) for t in self.tasks)

    # --- スケジューラ
    def _schedule(self):
        with self.lock:
            active = sum(1 for t in self.tasks if t.status in (RUNNING, RETRYING, VERIFYING))
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
            busy = self.busy()
        if not busy and self._had_activity:
            self._had_activity = False
            self.listener("up_all_done", None)

    def _run(self, t: UploadTask):
        self.listener("up_update", t)
        retries = 0
        while True:
            try:
                self._upload(t)
                break
            except Stopped:
                break
            except UploadError as ex:
                t.status, t.message = ERROR, str(ex)
                break
            except (requests.exceptions.RequestException, OSError, ValueError) as ex:
                if t._stop.is_set():
                    break
                retries += 1
                if retries > MAX_RETRIES:
                    t.status, t.message = ERROR, f"接続エラーが続いたため中止: {ex}"
                    break
                wait = min(30, 2 ** min(retries, 5))
                t.status = RETRYING
                t.message = f"{retries}回目の再送まで{wait}秒"
                t.speed = 0
                self.save()
                self.listener("up_update", t)
                if t._stop.wait(wait):
                    break
                t.status, t.message = RUNNING, ""
        if t._stop.is_set() and t.status in (RUNNING, RETRYING, VERIFYING):
            t.status, t.message = PAUSED, ""
        t.speed = 0
        self.save()
        if t.status == DONE:
            self.listener("up_done", t)
            self.check_groups()
        elif t.status == ERROR:
            self.listener("up_error", t)
        else:
            self.listener("up_update", t)
        self._schedule()

    # --- 本体
    def _check_file(self, t):
        p = Path(t.path)
        try:
            st = p.stat()
        except FileNotFoundError:
            raise UploadError("元のファイルが見つかりません（移動・削除された可能性）")
        if st.st_size != t.size or abs(st.st_mtime - t.mtime) > 2:
            raise UploadError("送信中に元のファイルが変更されました。一覧から削除して追加し直してください")

    def _start_fresh(self, t, session):
        t.upload_id = uuid.uuid4().hex
        t.server = get_server(session)
        t.next_chunk = 0
        t.sent = 0
        t.cookies = {}

    def _upload(self, t: UploadTask):
        if t.is_zip and not t.zip_ready:
            self._build_zip(t)
        self._check_file(t)
        session = engine.new_session()
        if t.upload_id and t.next_chunk > 0:
            for k, v in (t.cookies or {}).items():
                session.cookies.set(k, v)
        else:
            self._start_fresh(t, session)
        self.save()

        while t.next_chunk < t.chunks:
            if t._stop.is_set():
                raise Stopped()
            idx = t.next_chunk
            start = idx * t.chunk_size
            length = min(t.chunk_size, t.size - start)
            t.sent = start
            last_emit = [time.monotonic()]
            window = [time.monotonic(), start]

            def on_read(n):
                t.sent += n
                now = time.monotonic()
                if now - last_emit[0] >= 0.3:
                    dt = max(now - window[0], 1e-6)
                    inst = (t.sent - window[1]) / dt
                    t.speed = inst if not t.speed else t.speed * 0.7 + inst * 0.3
                    window[0], window[1] = now, t.sent
                    last_emit[0] = now
                    self.listener("up_update", t)

            fields = [("id", t.upload_id), ("name", t.name), ("chunk", str(idx)),
                      ("chunks", str(t.chunks)), ("lifetime", str(t.lifetime))]
            body = ChunkBody(fields, t.path, start, length, on_read, t._stop)
            try:
                r = session.post(f"https://{t.server}/upload_chunk.php", data=body,
                                 headers={"Content-Type": body.content_type,
                                          "Content-Length": str(len(body)),
                                          "Origin": "https://gigafile.nu",
                                          "Referer": TOP_URL},
                                 timeout=(30, 1800))
            except requests.exceptions.RequestException as ex:
                if t._stop.is_set() or isinstance(ex.__context__, Stopped) or "Stopped" in repr(ex):
                    t.sent = start
                    raise Stopped()
                t.sent = start
                raise
            finally:
                body.close()
            # 送り終わって返事が来た分は、一時停止が押されていても必ず記録する
            # （捨てると再開時に同じ分を送り直し、順番違いで最初からになってしまう）
            r.raise_for_status()
            try:
                data = r.json()
            except ValueError:
                raise requests.exceptions.ContentDecodingError("サーバーの応答を読めませんでした")
            if data.get("status") != 0:
                # 再開した続きが受け付けられない → 一度だけ最初から送り直す
                if idx > 0 and not t.restarted:
                    t.restarted = True
                    t.message = "続きを受け付けてもらえなかったため、最初から送り直します"
                    self.listener("up_update", t)
                    self._start_fresh(t, session)
                    self.save()
                    continue
                raise UploadError(f"ギガファイル便がエラーを返しました: {data.get('message') or data}")
            t.cookies = requests.utils.dict_from_cookiejar(session.cookies)
            t.next_chunk = idx + 1
            t.sent = start + length
            self.save()
            self.listener("up_update", t)
            if t.next_chunk >= t.chunks:
                if not data.get("url"):
                    raise UploadError("送信は終わりましたがURLが返ってきませんでした")
                t.url = data["url"]
                t.srv_filename = data.get("filename", "")
                t.delkey = data.get("delkey", "")
                self.save()

        # --- 仕上げ：パスワード設定 → サイズ確認
        if t._stop.is_set():
            raise Stopped()
        t.status = VERIFYING
        t.message = ""
        self.listener("up_update", t)
        if t.dlkey:
            self._apply_dlkey(t, session)
        self._verify(t, session)
        t.status = DONE
        t.finished_at = time.time()
        t.sent = t.size
        t.message = ""
        if t.is_zip:            # 送り終わったら作業用ZIPを消す
            _unlink(Path(t.path))

    def _apply_dlkey(self, t, session):
        if not (t.srv_filename and t.delkey):
            raise UploadError("パスワード設定に必要な情報がありません")
        server = t.server or get_server(session)
        r = session.get(f"https://{server}/set_dlkey.php",
                        params={"file": t.srv_filename, "delkey": t.delkey, "dlkey": t.dlkey},
                        timeout=30)
        r.raise_for_status()
        if r.json().get("status") != 0:
            raise UploadError("パスワードを設定できませんでした")

    def _verify(self, t, session):
        files = engine.parse_page(t.url, session)
        fid = files[0][2]
        base = engine.normalize_url(t.url).rsplit("/", 1)[0]
        url = f"{base}/download.php?file={fid}"
        if t.dlkey:
            url += f"&dlkey={t.dlkey}"
        with session.get(url, stream=True, timeout=60, headers={"Referer": t.url}) as r:
            r.raise_for_status()
            if "text/html" in r.headers.get("Content-Type", "").lower():
                raise UploadError("確認用のダウンロードができませんでした（パスワード設定を確認してください）")
            n = int(r.headers.get("Content-Length") or 0)
        if n != t.size:
            raise UploadError(f"ギガファイル便側のサイズが一致しません（元 {t.size} / 先 {n}）。もう一度送り直してください")


def _unlink(p):
    try:
        Path(p).unlink()
    except (FileNotFoundError, OSError):
        pass


def requests_session_for(t):
    s = engine.new_session()
    for k, v in (t.cookies or {}).items():
        s.cookies.set(k, v)
    return s


def share_text_group(g, members):
    total = sum(t.size for t in members)
    lines = [f"{g.title}（{len(members)}ファイル・合計 {engine.human(total)}）"]
    lines += [f"・{t.name}" for t in members]
    lines.append(g.url)
    if g.dlkey:
        lines.append(f"パスワード: {g.dlkey}")
    lines.append("")
    lines.append(f"※ダウンロード期限：アップロードから{g.lifetime}日")
    return "\n".join(lines)


def share_text(tasks):
    """相手に送る用の文面"""
    lines = []
    for t in tasks:
        lines.append(f"{t.name}（{engine.human(t.size)}）")
        lines.append(t.url)
        if t.dlkey:
            lines.append(f"パスワード: {t.dlkey}")
        lines.append("")
    if tasks:
        days = max(t.lifetime for t in tasks)
        lines.append(f"※ダウンロード期限：アップロードから{days}日")
    return "\n".join(lines).strip()
