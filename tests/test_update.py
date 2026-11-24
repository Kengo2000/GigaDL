"""新しい版のお知らせ（update_check）の判定を確かめる。通信はしない"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import update_check as uc  # noqa: E402

FAIL = []


def check(cond, label):
    print(("OK  " if cond else "NG  ") + label, flush=True)
    if not cond:
        FAIL.append(label)


check(uc.is_newer("1.3.5", "1.3.4"), "1.3.5 は 1.3.4 より新しい")
check(uc.is_newer("1.3.10", "1.3.9"), "1.3.10 は 1.3.9 より新しい（文字順ではなく数で比べる）")
check(uc.is_newer("1.4", "1.3.9"), "1.4 は 1.3.9 より新しい")
check(not uc.is_newer("1.3.4", "1.3.4"), "同じ版は知らせない")
check(not uc.is_newer("1.3.3", "1.3.4"), "古い版は知らせない")
check(not uc.is_newer("", "1.3.4"), "版番号が空なら知らせない")


class Resp:
    def __init__(self, data, code=200):
        self.data, self.status_code = data, code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Sess:
    def __init__(self, resp):
        self.resp = resp

    def get(self, *a, **k):
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


ok = uc.fetch_latest(session=Sess(Resp({"version": "1.4.0", "url": "https://1drv.ms/x"})))
check(ok and ok["version"] == "1.4.0" and ok["url"] == "https://1drv.ms/x", "version.json を読める")
bad = uc.fetch_latest(session=Sess(Resp({"version": "1.4.0", "url": "javascript:alert(1)"})))
check(bad and bad["url"] == "", "https 以外の場所は開かない")
check(uc.fetch_latest(session=Sess(OSError("offline"))) is None, "通信できなくてもエラーにならない")
check(uc.fetch_latest(session=Sess(Resp(ValueError("html")))) is None, "おかしな内容でもエラーにならない")
check(uc.fetch_latest(session=Sess(Resp({}, 404))) is None, "ファイルが無くてもエラーにならない")

orig = uc.fetch_latest
uc.fetch_latest = lambda **k: {"version": "1.4.0", "url": ""}
check(uc.check("1.3.4") is not None, "新しい版があれば知らせる")
check(uc.check("1.3.4", skip="1.4.0") is None, "「この版は知らせない」にした版は知らせない")
check(uc.check("1.4.0") is None, "最新を使っていれば知らせない")
uc.fetch_latest = orig

print("RESULT:", "PASS" if not FAIL else f"FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
