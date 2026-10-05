"""インストーラから呼ばれる：アイコン画像を生成（png / ico / icns）
使い方: python make_icon.py <出力フォルダ>
"""
import os
import subprocess
import sys
import tempfile

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath

app = QGuiApplication(sys.argv[:1])
out = sys.argv[1]
os.makedirs(out, exist_ok=True)


def draw(n):
    img = QImage(n, n, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, 0, n)
    g.setColorAt(0, QColor("#ff8a3d"))
    g.setColorAt(1, QColor("#e5484d"))
    path = QPainterPath()
    m = n * 0.08
    path.addRoundedRect(QRectF(m, m, n - 2 * m, n - 2 * m), n * 0.2, n * 0.2)
    p.fillPath(path, g)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("white"))
    w, cx = n * 0.14, n / 2
    p.drawRect(QRectF(cx - w / 2, n * 0.24, w, n * 0.30))
    tri = QPainterPath()
    tri.moveTo(cx - n * 0.2, n * 0.50)
    tri.lineTo(cx + n * 0.2, n * 0.50)
    tri.lineTo(cx, n * 0.70)
    tri.closeSubpath()
    p.drawPath(tri)
    p.drawRect(QRectF(n * 0.27, n * 0.74, n * 0.46, n * 0.05))
    p.end()
    return img


draw(256).save(os.path.join(out, "GigaDL.png"))

if sys.platform.startswith("win"):
    draw(256).save(os.path.join(out, "GigaDL.ico"))

if sys.platform == "darwin":
    d = tempfile.mkdtemp()
    iconset = os.path.join(d, "GigaDL.iconset")
    os.mkdir(iconset)
    for s in (16, 32, 128, 256, 512):
        draw(s).save(os.path.join(iconset, f"icon_{s}x{s}.png"))
        draw(s * 2).save(os.path.join(iconset, f"icon_{s}x{s}@2x.png"))
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o",
                    os.path.join(out, "GigaDL.icns")], check=True)
