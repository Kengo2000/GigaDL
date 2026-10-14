import hashlib, os, re, sys, threading, time, json, shutil
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests
from requests.adapters import HTTPAdapter
import engine
FAIL = []
def check(cond, label):
    print(("OK  " if cond else "NG  ") + label, flush=True)
    if not cond: FAIL.append(label)

random_data = {
    "1001-single": os.urandom(5 * 1024 * 1024 + 123),
    "m1": os.urandom(3 * 1024 * 1024),
    "m2": os.urandom(2 * 1024 * 1024 + 7),
    "1001-locked": os.urandom(1024 * 1024),
    "1001-locked2": os.urandom(512 * 1024),
}
REAL_NOKEY = ('<!DOCTYPE html>\r\n<html lang="ja">\r\n\r\n<head>\r\n  <meta charset="UTF-8">\r\n  <script>\r\n'
              '  alert("ダウンロードキーが異なります。");\r\n    history.back();\r\n    </script>\r\n</head>\r\n\r\n</html>')
KEYS = {"1001-locked": "ab12", "1001-locked2": "cd34"}
LOCKED_DL = []        # パスワード付きファイルへの download.php の呼び出し（鍵付きかどうか）
CHECKS = []
NAMES = {"1001-single": "素材A.mov", "m1": "clip1.mp4", "m2": "clip2.wav", "1001-locked": "secret.zip",
         "1001-locked2": "secret2.zip"}
DROP = {"1001-single": 2}  # 最初の2回は途中で切断
HITS = {}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def send_html(self, html, code=200):
        b = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/1001-single":
            self.send_response(200)
            self.send_header("Set-Cookie", "sess=abc; Path=/")
            b = f'<div id="dl">素材A.mov</div><span class="dl_size">5 MB</span>'.encode()
            self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        if u.path in ("/1001-locked", "/1001-locked2"):
            fid = u.path[1:]
            # 本物と同じ：パスワード欄 #dlkey と download('…', true, false)
            return self.send_html(f'<span id="dl" onclick="download(\'{fid}\', true, false);">'
                                  f'{"sec***.zip" if fid == "1001-locked" else NAMES[fid]}</span>'
                                  '<input type="text" id="dlkey" maxlength="4">'
                                  '<span class="dl_size">1 MB</span>')
        if u.path == "/check_dlkey.php":
            fid, k = q["file"][0], q.get("dlkey", [""])[0]
            CHECKS.append((fid, k))
            return self.send_json({"status": 0 if KEYS.get(fid) == k else 1})
        if u.path == "/1001-matome":
            items = ""
            for i, fid in enumerate(["m1", "m2"]):
                items += (f'<div class="matomete_file"><div class="matomete_file_info"><span>x</span>'
                          f'<span>{NAMES[fid]}</span><span>（{len(random_data[fid])//1024} KB）</span></div>'
                          f'<button class="download_panel_btn_dl" onclick="download({i}, \'{fid}\')"></button></div>')
            return self.send_html(f'<div id="contents_matomete">{items}</div>')
        if u.path == "/get_uploaded_file_name_jx.php":
            b = json.dumps({"filename": NAMES[q["file"][0]]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
        if u.path == "/download.php":
            fid = q["file"][0]
            if fid in KEYS:
                LOCKED_DL.append((fid, q.get("dlkey", [""])[0]))
                if q.get("dlkey", [""])[0] != KEYS[fid]:
                    return self.send_html(REAL_NOKEY)      # 本物と同じ返事（200・text/html）
            data = random_data[fid]
            start = 0
            rng = self.headers.get("Range")
            HITS.setdefault(fid, []).append(rng)
            if rng:
                start = int(re.search(r"bytes=(\d+)-", rng)[1])
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(data)-1}/{len(data)}")
            else:
                self.send_response(200)
            body = data[start:]
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if DROP.get(fid, 0) > 0:
                DROP[fid] -= 1
                self.wfile.write(body[: len(body) // 3])
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(2)
                return
            # ゆっくり送る（一時停止テスト用に locked 以外）
            for i in range(0, len(body), 256 * 1024):
                self.wfile.write(body[i:i + 256 * 1024])
                time.sleep(0.02)
            return
        self.send_html("nf", 404)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


class Local(HTTPAdapter):
    def send(self, request, **kw):
        u = urlparse(request.url)
        assert u.hostname.endswith("gigafile.jp"), request.url
        request.url = f"http://127.0.0.1:{port}{u.path}" + (f"?{u.query}" if u.query else "")
        return super().send(request, **kw)


orig = engine.new_session
def patched():
    s = orig(); s.mount("https://", Local()); return s
engine.new_session = patched

import tempfile
base = Path(tempfile.mkdtemp()) / "dl"
shutil.rmtree(base, ignore_errors=True); base.mkdir()
events = []
def listener(e, t):
    if e != "update":
        events.append((e, getattr(t, "name", t)))

engine.MAX_RETRIES = 5
real_wait = threading.Event.wait
m = engine.Manager(base / "state.json", listener)

m.add_url("https://46.gigafile.nu/1001-single", "", base)
m.add_url("https://46.gigafile.nu/1001-matome", "", base)
m.add_url("https://46.gigafile.nu/1001-locked", "", base)
m.add_url("https://46.gigafile.nu/1001-nope", "", base)

# 再接続待ちを短縮
engine.Task._orig = None
def wait_until(cond, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond(): return True
        time.sleep(0.1)
    return False

# 再接続待機を短くするため min を潰す
import builtins
time.sleep(1)
for t in m.tasks:
    pass
ok = wait_until(lambda: len(m.tasks) == 4 and all(t.status in (engine.DONE, engine.ERROR, engine.KEY_WAIT) for t in m.tasks), 120)
print("finished:", ok)
for t in m.tasks:
    print(f"  {t.name:12} {t.status} {t.message}")
print("events:", events)
print("single Range headers:", HITS.get("1001-single"))

# パスワードを入れ忘れて追加 → 「パスワード待ち」、ダウンロードは試さない
lk = next(t for t in m.tasks if t.name == "secret.zip")
kw_status, kw_hits = lk.status, list(LOCKED_DL)
kw_event = any(e == "need_key" for e, _ in events)
# 間違ったパスワード → 確認で弾く（ダウンロードを試さない）
m.set_key([lk], "zz99")
wait_until(lambda: lk.status == engine.KEY_WAIT and "違います" in lk.message, 15)
wrong_msg, wrong_hits = lk.message, list(LOCKED_DL)
# 同じURLを正しいパスワード付きで追加し直す → 同じ行が再開して完了（行は増えない）
n_before = len(m.tasks)
m.add_url("https://46.gigafile.nu/1001-locked", "ab12", base)
wait_until(lambda: lk.status == engine.DONE, 30)
readd_ok, n_after = lk.status == engine.DONE, len(m.tasks)
# 古い版で追加した行（パスワード要否を知らない）でも、失敗後にパスワードを入れれば完了
old = engine.Task("https://46.gigafile.nu/1001-locked2", "1001-locked2", "secret2.zip", "", "", str(base))
m.tasks.append(old); m._schedule()
wait_until(lambda: old.status == engine.KEY_WAIT, 15)
old_wait = old.status == engine.KEY_WAIT and old.needs_key
m.set_key([old], "cd34")
wait_until(lambda: old.status == engine.DONE, 30)
print("locked after key:", lk.status, lk.message)

# 一時停止→再開
DROP["m1"] = 0
random_data["big"] = os.urandom(20 * 1024 * 1024); NAMES["big"] = "big.bin"
t = engine.Task("https://46.gigafile.nu/1001-single", "big", "big.bin", "20MB", "", str(base))
m.tasks.append(t); m._schedule()
wait_until(lambda: t.done_bytes > 3 * 1024 * 1024, 20)
m.pause(t); wait_until(lambda: t.status == engine.PAUSED, 10)
paused_at = t.part_path.stat().st_size if t.part_path.exists() else -1
print("paused at", t.done_bytes, "part exists", t.part_path.exists())
# 再起動をシミュレート：新しいManagerで読み込み
m.save()
m2 = engine.Manager(base / "state.json", listener)
t2 = next(x for x in m2.tasks if x.file_id == "big")
print("reloaded:", t2.status, t2.done_bytes)
HITS["big"] = []
m2.startup()
wait_until(lambda: t2.status == engine.DONE, 30)
print("big:", t2.status, "Range:", HITS["big"])

for fid, name in NAMES.items():
    p = base / name
    if p.exists():
        same = hashlib.md5(p.read_bytes()).digest() == hashlib.md5(random_data[fid]).digest()
        print(f"verify {name}: {same}")
print("leftover parts:", list(base.glob("*.gdlpart")))

# 保存先変更テスト：ダウンロード中に別フォルダへ
random_data["mv"] = os.urandom(20 * 1024 * 1024); NAMES["mv"] = "move.bin"
t3 = engine.Task("https://46.gigafile.nu/1001-single", "mv", "move.bin", "20MB", "", str(base))
m2.tasks.append(t3); m2._schedule()
wait_until(lambda: t3.done_bytes > 3 * 1024 * 1024, 20)
other = base / "案件B"
HITS["mv"] = []
m2.change_dest(t3, other)
wait_until(lambda: t3.status == engine.DONE, 60)
p = other / "move.bin"
print("moved:", t3.status, t3.dest_dir.endswith("案件B"), p.exists(),
      p.exists() and p.read_bytes() == random_data["mv"], "Range:", HITS["mv"][-1])
print("old part gone:", not (base / "move.bin.gdlpart").exists())

# ---- 保存先の変更（ダウンロード中に別フォルダへ）
random_data["mv"] = os.urandom(20 * 1024 * 1024); NAMES["mv"] = "move.bin"
other = base / "別フォルダ"
t3 = engine.Task("https://46.gigafile.nu/1001-single", "mv", "move.bin", "20MB", "", str(base))
m2.tasks.append(t3); m2._schedule()
wait_until(lambda: t3.done_bytes > 3 * 1024 * 1024, 20)
before = t3.done_bytes
HITS["mv"] = []
m2.change_dest(t3, other)
wait_until(lambda: t3.status == engine.DONE, 40)
print("move:", t3.status, Path(t3.final_path) == other / "move.bin", "resumed from", HITS["mv"][-1], "(had", before, ")")
print("verify moved:", hashlib.md5(Path(t3.final_path).read_bytes()).digest() == hashlib.md5(random_data["mv"]).digest())
# 待機中のタスクの保存先変更（勝手に始まらないこと）
m2.max_parallel = 0
t4 = engine.Task("https://46.gigafile.nu/1001-single", "m1", "q.bin", "", "", str(base))
m2.tasks.append(t4)
m2.change_dest(t4, other); time.sleep(0.5)
print("queued->moved:", t4.status, t4.dest_dir.endswith("別フォルダ"))
print("old part leftovers:", list(base.glob("*.gdlpart")))

# ---- 判定
check(ok, "全タスクが終了")
for nm in ("素材A.mov", "clip1.mp4", "clip2.wav", "secret.zip", "secret2.zip", "big.bin"):
    p = base / nm
    fid = next(k for k, v in NAMES.items() if v == nm)
    check(p.exists() and hashlib.md5(p.read_bytes()).digest() == hashlib.md5(random_data[fid]).digest(), f"中身一致 {nm}")
check(HITS.get("1001-single", [None])[1:] and all(h and h.startswith("bytes=") for h in HITS["1001-single"][1:]), "切断後にRangeで続きから")
check(kw_status == engine.KEY_WAIT and kw_event, "入れ忘れは「パスワード待ち」になり入力を促す")
check(kw_hits == [], "入れ忘れのときはダウンロードを試さない（失敗回数を増やさない）")
check("違います" in wrong_msg and wrong_hits == [], "違うパスワードは先に確認して弾く")
check(readd_ok and n_after == n_before, "同じURLをパスワード付きで追加し直すと、その行が再開して完了")
check(old_wait and old.status == engine.DONE, "古い行でも失敗後にパスワードを入れれば完了")
check(all(k == KEYS[f] for f, k in LOCKED_DL if f == "1001-locked"),
      "新しく追加した行は、正しいパスワードのときだけダウンロードする")
check(t2.status == engine.DONE and HITS["big"] == [f"bytes={paused_at}-"] and paused_at > 0, f"再起動後に続きから再開 (停止位置 {paused_at})")
check(t3.status == engine.DONE and Path(t3.final_path) == other / "move.bin", "ダウンロード中の保存先変更")
check(t4.status == engine.QUEUED and Path(t4.dest_dir) == other, "待機中の保存先変更")
check(not list(base.glob("*.gdlpart")), "途中ファイルが残らない")
check(engine.sanitize("CON.txt") == "_CON.txt", "Windows予約名の回避")
print("RESULT:", "PASS" if not FAIL else f"FAIL {FAIL}")
srv.shutdown()
os._exit(1 if FAIL else 0)
