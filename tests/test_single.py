"""GigaDLを2回起動しても、2つ目はすぐ終わり、1つ目だけが動き続けることを確認"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tmp = tempfile.mkdtemp()
env = dict(os.environ, GIGADL_NO_UPDATE_CHECK="1", HOME=tmp, USERPROFILE=tmp, APPDATA=str(Path(tmp) / "appdata"),
           LOCALAPPDATA=str(Path(tmp) / "local"), XDG_DATA_HOME=str(Path(tmp) / "xdg"))
FAIL = []


def check(cond, label):
    print(("OK  " if cond else "NG  ") + label, flush=True)
    if not cond:
        FAIL.append(label)


cmd = [sys.executable, str(ROOT / "gigadl.py")]
first = subprocess.Popen(cmd, env=env, cwd=ROOT)
time.sleep(8)
check(first.poll() is None, "1つ目が起動して動いている")

t0 = time.time()
second = subprocess.run(cmd, env=env, cwd=ROOT, timeout=60)
check(second.returncode == 0 and time.time() - t0 < 30, "2つ目はすぐ終わる（1つ目を前に出すだけ）")
time.sleep(1)
check(first.poll() is None, "1つ目は動き続けている")

first.kill()
first.wait(timeout=20)
time.sleep(1)
third = subprocess.Popen(cmd, env=env, cwd=ROOT)
time.sleep(8)
check(third.poll() is None, "1つ目が異常終了した後でも、また起動できる")
third.kill()
third.wait(timeout=20)

print("RESULT:", "PASS" if not FAIL else f"FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
