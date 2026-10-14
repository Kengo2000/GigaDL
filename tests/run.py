"""CI用: テストを実行し、失敗したら出力の要点をGitHubのエラー注釈として出す"""
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):   # Windowsのコンソール(cp1252)でも日本語を出せるように
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

script = sys.argv[1]
out = Path(__file__).resolve().parent / "out"
out.mkdir(exist_ok=True)
p = subprocess.run([sys.executable, "-X", "utf8", script], capture_output=True,
                   text=True, encoding="utf-8", errors="replace", timeout=900)
log = p.stdout + "\n---- stderr ----\n" + p.stderr
(out / (Path(script).stem + ".log")).write_text(log, encoding="utf-8")
print(p.stdout)
if p.returncode:
    lines = [l for l in p.stdout.splitlines() if l.startswith(("NG", "RESULT"))]
    if not any(l.startswith("RESULT") for l in lines):      # 途中で落ちた：どこまで進んだか
        lines = ["(途中で終了) 最後の出力:"] + p.stdout.strip().splitlines()[-6:]
    err = p.stderr.strip().splitlines()
    tb = []
    if "Traceback" in p.stderr:
        tb = p.stderr[p.stderr.rfind("Traceback"):].splitlines()[-12:]
    msg = "\n".join(lines + tb + (err[-8:] if not tb else []))[:3500]
    msg = msg.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::error title={Path(script).name} failed (exit {p.returncode})::{msg}")
sys.exit(p.returncode)
