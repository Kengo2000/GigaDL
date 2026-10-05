"""
画面の動作確認（実際のウィンドウを開いて操作する）
- 模擬ギガファイル便サーバーを立て、URL入力→追加→完了までを画面経由で行う
- 保存先プルダウン、保存先変更、完了通知の呼び出しを確認
- 途中経過のスクリーンショットを tests/out/ に保存
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))

# 設定ファイルをテスト用の場所に
TMP = Path(tempfile.mkdtemp())
os.environ["APPDATA"] = str(TMP / "appdata")
os.environ["XDG_DATA_HOME"] = str(TMP / "xdg")

DATA = {"9001-alpha": os.urandom(4 * 1024 * 1024), "9001-beta": os.urandom(6 * 1024 * 1024)}
UP = {"next": {}, "buf": {}, "files": {}}
NAMES = {"9001-alpha": "日本語ファイル.mov", "9001-beta": "beta.mp4"}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        import re as _re
        n = int(self.headers["Content-Length"])
        body = self.rfile.read(n)
        if self.path.startswith("/matomete_get_url.php"):
            q = parse_qs(body.decode(), keep_blank_values=True)
            pages = [u.rsplit("/", 1)[1] for u in q.get("urls[]", [])]
            mid = f"9200-{len(UP.setdefault('matome', {}))}"
            UP["matome"][mid] = {"pages": pages, "name": q.get("file_name", [""])[0], "dlkey": q.get("dlkey", [""])[0]}
            out = {"status": 0, "url": f"https://53.gigafile.nu/{mid}", "delkey": "m", "file_name": UP["matome"][mid]["name"] or "gigafile-x"}
            return self._send(200, "application/json", json.dumps(out).encode())
        b = _re.search(r"boundary=(.+)", self.headers["Content-Type"])[1].encode()
        f = {}
        for part in body.split(b"--" + b):
            head, _, data = part.partition(b"\r\n\r\n")
            m = _re.search(rb'name="([^"]+)"', head)
            if m:
                f[m[1].decode()] = data[:-2]
        uid, idx, total = f["id"].decode(), int(f["chunk"]), int(f["chunks"])
        if UP["next"].get(uid, 0) != idx:
            return self._send(200, "application/json", b'{"status":1,"message":"order"}')
        UP["buf"][uid] = UP["buf"].get(uid, b"") + f["file"]
        UP["next"][uid] = idx + 1
        if idx + 1 < total:
            return self._send(200, "application/json", b'{"status":0}')
        page = "9100-" + uid[:8]
        DATA[page] = UP["buf"][uid]
        NAMES[page] = f["name"].decode()
        UP["files"]["srv" + uid[:6]] = {"page": page, "dlkey": None}
        out = {"status": 0, "url": f"https://53.gigafile.nu/{page}", "filename": "srv" + uid[:6], "delkey": "dk"}
        return self._send(200, "application/json", json.dumps(out).encode())

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        pid = u.path.strip("/")
        if pid == "":
            return self._send(200, "text/html", b'<script>var server = "53.gigafile.nu";</script>')
        if pid in UP.get("matome", {}):
            items = "".join(
                f'<div class="matomete_file"><div class="matomete_file_info"><span>x</span><span>{NAMES[pg]}</span>'
                f'<span>（1 MB）</span></div><button class="download_panel_btn_dl" onclick="download({i}, \'{pg}\')"></button></div>'
                for i, pg in enumerate(UP["matome"][pid]["pages"]))
            return self._send(200, "text/html; charset=utf-8", f'<div id="contents_matomete">{items}</div>'.encode())
        if u.path == "/set_dlkey.php":
            UP["files"][q["file"][0]]["dlkey"] = q["dlkey"][0]
            return self._send(200, "application/json", b'{"status":0}')
        if pid in DATA:
            html = f'<div id="dl">{NAMES[pid]}</div><span class="dl_size">1 MB</span>'
            return self._send(200, "text/html; charset=utf-8", html.encode())
        if u.path == "/download.php":
            data = DATA[q["file"][0]]
            start = 0
            rng = self.headers.get("Range")
            extra = {}
            code = 200
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                code = 206
                extra["Content-Range"] = f"bytes {start}-{len(data)-1}/{len(data)}"
            body = data[start:]
            self.send_response(code)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            for i in range(0, len(body), 128 * 1024):   # ゆっくり送る
                self.wfile.write(body[i:i + 128 * 1024])
                time.sleep(0.03)
            return
        self._send(404, "text/plain", b"nf")


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

import requests  # noqa: E402
from requests.adapters import HTTPAdapter  # noqa: E402

import engine  # noqa: E402
import uploader  # noqa: E402
uploader.UP_CHUNK = 1024 * 1024


class Local(HTTPAdapter):
    def send(self, request, **kw):
        u = urlparse(request.url)
        request.url = f"http://127.0.0.1:{port}{u.path}" + (f"?{u.query}" if u.query else "")
        return super().send(request, **kw)


_orig = engine.new_session


def _patched():
    s = _orig()
    s.mount("https://", Local())
    return s


engine.new_session = _patched

import platform_util as pu  # noqa: E402

NOTES = []
pu.notify = lambda title, text, error=False: NOTES.append((title, text, error))

import gigadl  # noqa: E402
gigadl.notify = pu.notify
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

FAIL = []


def check(cond, label):
    print(("OK  " if cond else "NG  ") + label, flush=True)
    if not cond:
        FAIL.append(label)


app = QApplication(sys.argv)
w = gigadl.Main()
dl1 = TMP / "保存先1"
dl2 = TMP / "保存先2"
w.mgr.settings["dest_dir"] = str(dl1)
w.fill_dest_combo()
w.show()


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def wait_for(cond, sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.05)
    return False


def shot(name):
    w.grab().save(str(OUT / f"{name}.png"))


pump(1)
shot("1_起動直後")
check(w.isVisible(), "ウィンドウが表示される")
check(w.dest.currentData() == str(dl1), "保存先プルダウンに初期フォルダ")

# --- URLを入れて追加（1件目は保存先1）
w.urls.setPlainText("https://46.gigafile.nu/9001-alpha")
w.add_btn.click()
check(wait_for(lambda: w.table.rowCount() == 1, 15), "一覧に1件追加")
pump(0.8)
shot("2_ダウンロード中")

# --- 2件目は「追加するたびにフォルダを選ぶ」で保存先2
w.ask_each.setChecked(True)
QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(dl2))
w.urls.setPlainText("https://46.gigafile.nu/9001-beta")
w.add_btn.click()
check(wait_for(lambda: w.table.rowCount() == 2, 15), "2件目を別フォルダで追加")

ok = wait_for(lambda: len(w.mgr.tasks) == 2 and all(t.status == engine.DONE for t in w.mgr.tasks), 90)
pump(0.5)
shot("3_完了")
check(ok, "2件とも完了")
for t in w.mgr.tasks:
    fid = t.file_id
    p = Path(t.final_path)
    check(p.exists() and p.read_bytes() == DATA[fid], f"中身一致 {p.name}")
alpha = next(t for t in w.mgr.tasks if t.file_id == "9001-alpha")
beta = next(t for t in w.mgr.tasks if t.file_id == "9001-beta")
check(Path(alpha.final_path).parent == dl1, "1件目は保存先1に保存")
check(Path(beta.final_path).parent == dl2, "2件目は保存先2に保存")
check(any(n[0] == "ダウンロード完了" for n in NOTES), "完了通知が呼ばれる")
check(w.table.item(0, 6).text() in (dl1.name, dl2.name), "保存先の列が表示される")
check(w.dest.findData(str(dl2)) >= 0, "使ったフォルダがプルダウンに記憶される")

# --- 完了を一覧から消す
w.clear_done()
check(w.table.rowCount() == 0, "完了を一覧から消す")

# --- 状態ファイルが保存されている
st = json.loads(Path(gigadl.STATE).read_text("utf-8"))
check(str(dl2) in st["settings"].get("recent_dirs", []), "最近のフォルダが保存される")

# --- アップロードのタブ
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Ok)
w.tabs.setCurrentIndex(1)
pump(0.5)
shot("4_アップロード画面")
movie = TMP / "送る素材.mov"
movie.write_bytes(os.urandom(5 * 1024 * 1024 + 9))
w.up_tab.lifetime.setCurrentIndex(w.up_tab.lifetime.findData(14))
w.up_tab.key.setText("x1")
w.up_tab.add_paths([str(movie)])
check(wait_for(lambda: w.up_tab.table.rowCount() == 1, 10), "アップロード一覧に追加")
pump(0.6)
shot("5_アップロード中")
ut = w.up.tasks[0]
check(wait_for(lambda: ut.status == uploader.DONE, 60), f"アップロード完了 {ut.message}")
pump(0.4)
shot("6_アップロード完了")
page = ut.url.rsplit("/", 1)[1]
check(DATA.get(page) == movie.read_bytes(), "相手側で元の1ファイルに戻っている")
check(any(v["dlkey"] == "x1" for v in UP["files"].values()), "パスワードが設定される")
check(ut.lifetime == 14, "保持期限14日")
check(w.up_tab.table.item(0, 6).text() == ut.url, "URLの列に表示")
w.up_tab.table.selectRow(0)
w.up_tab.act_share()
clip = QApplication.clipboard().text()
check(ut.url in clip and "パスワード: x1" in clip and "14日" in clip, "送る文面をコピー")
check(any(n[0] == "アップロード完了" for n in NOTES), "アップロード完了の通知")
w.grab_clipboard()
check(ut.url not in w.urls.toPlainText(), "自分のアップロードURLをダウンロード欄に入れない")

# --- まとめて1つのURLにする
import upload_ui  # noqa: E402
from PySide6.QtWidgets import QDialog  # noqa: E402
upload_ui.GroupDialog.exec = lambda self: (self.name.setText("案件A_素材"), QDialog.Accepted)[1]
w.up_tab.matome.setChecked(True)
w.up_tab.key.setText("")
mfiles = []
for nm in ("cam1.mov", "cam2.mov"):
    pth = TMP / nm
    pth.write_bytes(os.urandom(2 * 1024 * 1024 + 1))
    mfiles.append(str(pth))
w.up_tab.add_paths(mfiles)
check(wait_for(lambda: len(w.up.groups) == 1, 5), "まとめを作成")
g = w.up.groups[0]
check(wait_for(lambda: g.status == uploader.DONE, 60), f"全部送ったら1つのURLにまとまる {g.message}")
pump(0.5)
shot("7_まとめURL")
mt = UP["matome"].get(g.url.rsplit("/", 1)[1], {})
check(len(mt.get("pages", [])) == 2 and mt.get("name") == "案件A_素材", "2ファイルを指定の名前でまとめる")
check(w.up_tab.gtable.rowCount() == 1 and w.up_tab.gtable.item(0, 4).text() == g.url, "まとめたURLの一覧に表示")
w.up_tab.gtable.selectRow(0)
w.up_tab.g_share()
clip = QApplication.clipboard().text()
check(g.url in clip and "cam1.mov" in clip and "cam2.mov" in clip, "まとめの送る文面をコピー")
check(any(n[0] == "まとめURLができました" for n in NOTES), "まとめ完了の通知")

# --- 1つのZIPにして送る
import io  # noqa: E402
import zipfile  # noqa: E402
from PySide6.QtWidgets import QInputDialog  # noqa: E402
w.up.settings["zip_dir"] = str(TMP / "zipwork")
w.up_tab.zipbox.setChecked(True)
check(not w.up_tab.matome.isChecked(), "ZIPにするとまとめURLのチェックは外れる")
zdir = TMP / "納品素材"
(zdir / "sub").mkdir(parents=True)
(zdir / "sub" / "clip.mov").write_bytes(os.urandom(3 * 1024 * 1024))
(zdir / "readme.txt").write_bytes(b"memo")
QInputDialog.getText = staticmethod(lambda *a, **k: ("納品素材", True))
w.up_tab.add_paths([str(zdir)])
zt = w.up.tasks[-1]
check(zt.is_zip and zt.name == "納品素材.zip", "ZIPの送信を追加")
check(wait_for(lambda: zt.status == uploader.DONE, 60), f"ZIPを作って送信完了 {zt.message}")
pump(0.5)
shot("8_ZIPで送信")
zdata = DATA.get(zt.url.rsplit("/", 1)[1], b"")
with zipfile.ZipFile(io.BytesIO(zdata)) as z:
    check(sorted(z.namelist()) == ["納品素材/readme.txt", "納品素材/sub/clip.mov"]
          and all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()),
          "相手にはフォルダ構成のまま無圧縮ZIPで届く")
check(not Path(zt.path).exists(), "送信後に作業用ZIPを削除")

# --- 終了（確認ダイアログが出ない状態で閉じられる）
w.close()
pump(0.3)
check(not w.isVisible(), "ウィンドウを閉じられる")

print("platform:", sys.platform, "| Qt:", __import__("PySide6").__version__)
print("RESULT:", "PASS" if not FAIL else f"FAIL {FAIL}")
srv.shutdown()
os._exit(1 if FAIL else 0)
