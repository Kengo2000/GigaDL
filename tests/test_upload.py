"""
アップロードのテスト（模擬ギガファイル便サーバー）
本物と同じく: 分割は id ごとに 0,1,2… の順でしか受け付けない / 最初の分でクッキー発行 /
最後の分で url・filename・delkey を返す / set_dlkey.php / パスワード付きダウンロード
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from requests.adapters import HTTPAdapter  # noqa: E402

import engine  # noqa: E402
import uploader  # noqa: E402

FAIL = []


def check(cond, label):
    print(("OK  " if cond else "NG  ") + label, flush=True)
    if not cond:
        FAIL.append(label)


STATE = {"sessions": {}, "files": {}, "drop_on": set(), "reject_once": set(), "hits": [], "dropped": []}
LOCK = threading.Lock()


def parse_multipart(body, ctype):
    b = re.search(r"boundary=(.+)", ctype)[1].encode()
    parts = body.split(b"--" + b)
    out = {}
    for p in parts:
        if not p or p in (b"--\r\n", b"--"):
            continue
        head, _, data = p.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)[1].decode()
        out[name] = data[:-2] if data.endswith(b"\r\n") else data
    return out


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def js(self, obj, extra=None):
        self.send(200, "application/json", json.dumps(obj).encode(), extra)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/" and self.headers.get("X-Host", "").startswith("gigafile.nu"):
            return self.send(200, "text/html", b'<script>var server = "53.gigafile.nu";</script>')
        if u.path == "/set_dlkey.php":
            f = STATE["files"].get(q.get("file"))
            if f and f["delkey"] == q.get("delkey") and re.match(r"^[0-9A-Za-z]{1,4}$", q.get("dlkey", "")):
                f["dlkey"] = q["dlkey"]
                return self.js({"status": 0})
            return self.js({"status": 1})
        if u.path == "/download.php":
            f = next((x for x in STATE["files"].values() if x["page"] == q.get("file")), None)
            if not f:
                return self.send(404, "text/plain", b"nf")
            if f.get("dlkey") and q.get("dlkey") != f["dlkey"]:
                return self.send(200, "text/html", b"<html>key</html>")
            return self.send(200, "application/octet-stream", f["data"])
        page = u.path.strip("/")
        f = next((x for x in STATE["files"].values() if x["page"] == page), None)
        if f:
            html = f'<div id="dl">{f["name"]}</div><span class="dl_size">1 MB</span>'
            return self.send(200, "text/html; charset=utf-8", html.encode())
        self.send(404, "text/plain", b"nf")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/upload_chunk.php":
            return self.send(404, "text/plain", b"nf")
        n = int(self.headers["Content-Length"])
        ctype = self.headers["Content-Type"]
        # 指定された分は途中で接続を切る（回線断の再現）
        peek_key = None
        body = b""
        while len(body) < n:
            got = self.rfile.read(min(256 * 1024, n - len(body)))
            if not got:
                break
            body += got
            if peek_key is None and b'name="chunk"' in body:
                m = re.search(rb'name="id"\r\n\r\n([0-9a-f]+)\r\n.*?name="chunk"\r\n\r\n(\d+)\r\n', body, re.S)
                if m:
                    peek_key = (m[1].decode(), int(m[2]))
                    with LOCK:
                        drop = peek_key[1] in STATE["drop_on"]
                        if drop:
                            STATE["drop_on"].discard(peek_key[1])
                            STATE["dropped"].append(peek_key)
                    if drop:
                        self.rfile.read(min(512 * 1024, n - len(body)))
                        self.close_connection = True
                        self.connection.shutdown(2)
                        return
            time.sleep(0.002)
        f = parse_multipart(body, ctype)
        uid, idx, total = f["id"].decode(), int(f["chunk"]), int(f["chunks"])
        with LOCK:
            STATE["hits"].append((uid, idx))
            s = STATE["sessions"].setdefault(uid, {"next": 0, "data": b"", "cookie": None})
            cookie = self.headers.get("Cookie", "")
            extra = {}
            if idx == 0:
                s.update(next=0, data=b"", cookie=f"gfup={uid[:8]}")
                extra["Set-Cookie"] = s["cookie"] + "; Path=/"
            elif s["cookie"] not in cookie:
                return self.js({"status": 1, "message": "session"})
            if idx in STATE["reject_once"]:
                STATE["reject_once"].discard(idx)
                return self.js({"status": 1, "message": "expired"})
            if idx != s["next"]:
                return self.js({"status": 1, "message": f"order {idx}!={s['next']}"})
            s["data"] += f["file"]
            s["next"] += 1
            if s["next"] < total:
                return self.js({"status": 0}, extra)
            srv_name = "srv_" + uid[:10]
            page = f"1012-{uid[:12]}"
            STATE["files"][srv_name] = {"name": f["name"].decode(), "data": s["data"], "page": page,
                                        "delkey": "dk" + uid[:6], "lifetime": f["lifetime"].decode()}
            return self.js({"status": 0, "url": f"https://53.gigafile.nu/{page}", "filename": srv_name,
                            "delkey": "dk" + uid[:6]}, extra)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


class Local(HTTPAdapter):
    def send(self, request, **kw):
        # 元のリクエストはそのまま（クッキーが本来のホストに紐づくように）、コピーを手元のサーバーへ
        u = urlparse(request.url)
        req = request.copy()
        req.headers["X-Host"] = u.hostname
        req.url = f"http://127.0.0.1:{port}{u.path}" + (f"?{u.query}" if u.query else "")
        r = super().send(req, **kw)
        r.url = request.url
        return r


_orig = engine.new_session


def patched():
    s = _orig()
    s.mount("https://", Local())
    return s


engine.new_session = patched
uploader.UP_CHUNK = 1024 * 1024          # テストは1MBずつ
uploader.MAX_RETRIES = 6

tmp = Path(tempfile.mkdtemp())
src = tmp / "素材"
(src / "sub").mkdir(parents=True)
files = {
    "A_日本語ムービー.mov": os.urandom(5 * 1024 * 1024 + 321),
    "B.wav": os.urandom(3 * 1024 * 1024),
    "sub/C.mp4": os.urandom(1024 * 1024 + 5),
    ".DS_Store": b"x",
}
for k, v in files.items():
    (src / k).write_bytes(v)
big = tmp / "big.mov"
big.write_bytes(os.urandom(12 * 1024 * 1024 + 77))

events = []
m = uploader.UploadManager(tmp / "uploads.json", lambda e, t: e != "up_update" and events.append(e))


def wait(cond, sec=60):
    end = time.time() + sec
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def md5(b):
    return hashlib.md5(b).hexdigest()


def stored(t):
    return STATE["files"].get(t.srv_filename, {})


# 1) フォルダごと追加（隠しファイル除外）＋ パスワード ＋ 途中切断
STATE["drop_on"] = {2}
added = m.add_files([src], lifetime=30, dlkey="ab12")
check(len(added) == 3, "フォルダを展開して3ファイル（隠しファイル除外）")
check(wait(lambda: all(t.status in (uploader.DONE, uploader.ERROR) for t in added), 90), "3ファイルとも終了")
for t in added:
    rel = Path(t.path).relative_to(src).as_posix()
    check(t.status == uploader.DONE, f"完了 {rel} {t.message}")
    check(md5(stored(t).get("data", b"")) == md5(files[rel]), f"相手側で1ファイルに戻っている {rel}")
    check(stored(t).get("dlkey") == "ab12", f"パスワード設定 {rel}")
    check(stored(t).get("lifetime") == "30", f"保持期限30日 {rel}")
    check(t.url.startswith("https://") and t.delkey, f"URLと削除キー取得 {rel}")
check("up_done" in events and "up_all_done" in events, "完了の通知イベント")
ids = [h for h in STATE["hits"] if h[0] == added[0].upload_id]
check(STATE["dropped"] == [(added[0].upload_id, 2)] and [h[1] for h in ids] == [0, 1, 2, 3, 4, 5],
      "途中で切断された分（2番目）だけ送り直し、最初からにならない")

# 2) 一時停止 → アプリ再起動 → 続きの分から再開
STATE["hits"].clear()
t = m.add_files([big], lifetime=7)[0]
check(wait(lambda: t.next_chunk >= 4, 30), "大きいファイル送信中")
m.pause(t)
check(wait(lambda: t.status == uploader.PAUSED, 15), "一時停止")
stopped_at = t.next_chunk
m.save()
m2 = uploader.UploadManager(tmp / "uploads.json", lambda e, x: None)
t2 = next(x for x in m2.tasks if x.id == t.id)
check(t2.status == uploader.PAUSED and t2.next_chunk == stopped_at, f"再起動後も途中の位置を記憶（{stopped_at}/{t2.chunks}）")
STATE["hits"].clear()
m2.startup()
check(wait(lambda: t2.status == uploader.DONE, 60), "再起動後に完了")
resumed = [h[1] for h in STATE["hits"] if h[0] == t2.upload_id]
check(resumed and resumed[0] == stopped_at and 0 not in resumed, f"続きの分（{stopped_at}番目）から再開、最初からではない")
check(md5(stored(t2)["data"]) == md5(big.read_bytes()), "再開後も中身が一致")
check(t2.dlkey == "" and "dlkey" not in stored(t2), "パスワード無しでも完了")

# 3) 続きを拒否されたら、最初から送り直す
m3 = uploader.UploadManager(tmp / "u3.json", lambda e, x: None)
t3 = m3.add_files([big], lifetime=7)[0]
wait(lambda: t3.next_chunk >= 3, 30)
m3.pause(t3)
wait(lambda: t3.status == uploader.PAUSED, 15)
STATE["reject_once"] = {t3.next_chunk}
m3.resume(t3)
check(wait(lambda: t3.status == uploader.DONE, 60), "続きを拒否されても最初から送り直して完了")
check(md5(stored(t3)["data"]) == md5(big.read_bytes()), "送り直し後も中身が一致")

# 4) 送信中に元ファイルが変わったらエラー
chg = tmp / "chg.mov"
chg.write_bytes(os.urandom(6 * 1024 * 1024))
t4 = m3.add_files([chg], lifetime=7)[0]
wait(lambda: t4.next_chunk >= 1, 30)
m3.pause(t4)
wait(lambda: t4.status == uploader.PAUSED, 15)
with open(chg, "ab") as fh:
    fh.write(b"more")
m3.resume(t4)
check(wait(lambda: t4.status == uploader.ERROR, 20) and "変更" in t4.message, "元ファイルの変更を検出してエラー")

# 5) 完了後にパスワードを後から設定
t5 = added[1]
m.set_dlkey(t5, "Z9")
check(wait(lambda: stored(t5).get("dlkey") == "Z9", 10), "完了後にパスワードを後から設定")
try:
    m.set_dlkey(t5, "toolong")
    check(False, "5文字以上のパスワードを拒否")
except uploader.UploadError:
    check(True, "5文字以上のパスワードを拒否")

# 5b) 中身を送り終えた直後に一時停止しても、その分は取り消さない
ev = threading.Event()
bf = tmp / "body.bin"
bf.write_bytes(b"x" * 1000)
cb = uploader.ChunkBody([("id", "a"), ("lifetime", "7")], bf, 0, 1000, lambda n: None, ev)
got = cb.read(); got += cb.read(); got += cb.read()
ev.set()
try:
    end = cb.read()
    check(end == b"" and got.endswith(b"--\r\n"), "送り終えた後の一時停止で送信を壊さない")
except uploader.Stopped:
    check(False, "送り終えた後の一時停止で送信を壊さない")
cb2 = uploader.ChunkBody([("id", "a"), ("lifetime", "7")], bf, 0, 1000, lambda n: None, ev)
cb2.read()
try:
    cb2.read()
    check(False, "送っている途中なら一時停止できる")
except uploader.Stopped:
    check(True, "送っている途中なら一時停止できる")
cb2.close()

# 6) 相手に送る文面
txt = uploader.share_text(added)
check(all(t.url in txt for t in added) and "パスワード: ab12" in txt, "相手に送る文面を作れる")

print("RESULT:", "PASS" if not FAIL else f"FAIL {FAIL}")
srv.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
os._exit(1 if FAIL else 0)
