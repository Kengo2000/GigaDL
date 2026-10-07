"""
GigaDL — ギガファイル便ダウンローダー / アップローダー（Mac / Windows）
パスワード付きリンク / レジューム / 自動再接続 / 完了通知
"""
import sys
from pathlib import Path

import getpass

from PySide6.QtCore import QEvent, QLockFile, QObject, Qt, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QAction, QBrush, QColor, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QSystemTrayIcon, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import engine
import platform_util as pu
import uploader
from upload_ui import UploadTab
from engine import DONE, ERROR, MOVING, PAUSED, QUEUED, RETRYING, RUNNING, human
from platform_util import notify, reveal

APP_NAME = pu.APP_NAME
STATE = pu.data_dir() / "state.json"
UP_STATE = pu.data_dir() / "uploads.json"
HERE = Path(__file__).resolve().parent
COLS = ["ファイル名", "サイズ", "進捗", "速度", "残り", "状態", "保存先"]
MAX_RECENT = 10
OTHER = "__other__"


def app_icon():
    for name in ("GigaDL.png", "GigaDL.ico"):
        for d in (HERE, pu.data_dir()):
            if (d / name).exists():
                return QIcon(str(d / name))
    return QIcon()


# ---------------------------------------------------------------- スレッド→GUI

class Bridge(QObject):
    event = Signal(str, object)


def fmt_eta(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec//3600}時間{sec%3600//60}分"
    if sec >= 60:
        return f"{sec//60}分{sec%60}秒"
    return f"{sec}秒"


# ---------------------------------------------------------------- ウィンドウ

class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {pu.APP_VERSION}")
        self.resize(980, 620)
        self.bridge = Bridge()
        self.bridge.event.connect(self.on_event)
        self.mgr = engine.Manager(STATE, lambda e, t: self.bridge.event.emit(e, t))
        self.caff = pu.KeepAwake()
        self.setWindowIcon(app_icon())
        self.tray = None
        if not pu.IS_MAC and QSystemTrayIcon.isSystemTrayAvailable():
            # Windowsの通知はタスクトレイのアイコン経由で出す
            self.tray = QSystemTrayIcon(app_icon(), self)
            self.tray.setToolTip(APP_NAME)
            self.tray.activated.connect(lambda *_: (self.showNormal(), self.activateWindow()))
            self.tray.messageClicked.connect(lambda: (self.showNormal(), self.activateWindow()))
            self.tray.show()
            pu.set_tray(self.tray)
        self.row_of = {}   # task.id -> row
        self.bars = {}     # task.id -> QProgressBar
        self.batch_done = []

        self.up = uploader.UploadManager(UP_STATE, lambda e, t: self.bridge.event.emit(e, t))
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        root = QWidget()
        self.tabs.addTab(root, "ダウンロード")
        v = QVBoxLayout(root)

        # --- 入力
        h = QHBoxLayout()
        self.urls = QPlainTextEdit()
        self.urls.setPlaceholderText(
            "ギガファイル便のURLを貼り付け（複数行OK）\n例: https://46.gigafile.nu/1005-xxxxxxxx")
        self.urls.setFixedHeight(70)
        h.addWidget(self.urls, 1)
        right = QVBoxLayout()
        self.key = QLineEdit()
        self.key.setPlaceholderText("パスワード（任意）")
        self.key.setFixedWidth(160)
        self.add_btn = QPushButton("追加してダウンロード")
        self.add_btn.setDefault(True)
        self.add_btn.clicked.connect(self.add)
        right.addWidget(self.key)
        right.addWidget(self.add_btn)
        h.addLayout(right)
        v.addLayout(h)
        sc = QAction(self)
        sc.setShortcut(QKeySequence("Ctrl+Return"))   # Mac では ⌘↩
        sc.triggered.connect(self.add)
        self.addAction(sc)

        # --- 保存先（追加するURLごとに選べる。最近使ったフォルダを記憶）
        h = QHBoxLayout()
        h.addWidget(QLabel("保存先:"))
        self.dest = QComboBox()
        self.dest.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.dest.setMinimumContentsLength(30)
        self.dest.activated.connect(self.dest_activated)
        h.addWidget(self.dest, 1)
        self.ask_each = QCheckBox("追加するたびにフォルダを選ぶ")
        self.ask_each.setChecked(bool(self.mgr.settings.get("ask_each", False)))
        self.ask_each.toggled.connect(lambda on: self.set_setting("ask_each", on))
        h.addWidget(self.ask_each)
        b = QPushButton("開く"); b.clicked.connect(
            lambda: pu.open_folder(self.mgr.settings["dest_dir"])); h.addWidget(b)
        v.addLayout(h)
        self.fill_dest_combo()

        # --- 一覧
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.cellDoubleClicked.connect(self.double_clicked)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c, w in ((1, 80), (2, 170), (3, 90), (4, 80), (5, 200), (6, 140)):
            hh.setSectionResizeMode(c, QHeaderView.Interactive)
            self.table.setColumnWidth(c, w)
        v.addWidget(self.table, 1)

        # --- 操作
        h = QHBoxLayout()
        self.b_pause = QPushButton("一時停止"); self.b_pause.clicked.connect(self.act_pause)
        self.b_resume = QPushButton("再開"); self.b_resume.clicked.connect(self.act_resume)
        self.b_key = QPushButton("パスワード入力…"); self.b_key.clicked.connect(self.act_key)
        self.b_remove = QPushButton("削除"); self.b_remove.clicked.connect(self.act_remove)
        self.b_show = QPushButton(f"{pu.FILE_MANAGER}で表示"); self.b_show.clicked.connect(self.act_show)
        self.b_dest = QPushButton("保存先を変更…"); self.b_dest.clicked.connect(self.act_dest)
        for b in (self.b_pause, self.b_resume, self.b_key, self.b_dest, self.b_remove, self.b_show):
            h.addWidget(b)
        h.addStretch(1)
        b = QPushButton("完了を一覧から消す"); b.clicked.connect(self.clear_done); h.addWidget(b)
        v.addLayout(h)

        h = QHBoxLayout()
        b = QPushButton("すべて一時停止"); b.clicked.connect(self.mgr.pause_all); h.addWidget(b)
        b = QPushButton("すべて再開"); b.clicked.connect(self.mgr.resume_all); h.addWidget(b)
        h.addStretch(1)
        h.addWidget(QLabel("同時ダウンロード数:"))
        self.par = QSpinBox(); self.par.setRange(1, 6)
        self.par.setValue(int(self.mgr.settings.get("max_parallel", 2)))
        self.par.valueChanged.connect(self.set_parallel)
        h.addWidget(self.par)
        self.auto = QCheckBox("起動時に中断分を自動再開")
        self.auto.setChecked(bool(self.mgr.settings.get("auto_resume", True)))
        self.auto.toggled.connect(self.set_auto)
        h.addWidget(self.auto)
        v.addLayout(h)

        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.up_tab = UploadTab(self.up, self.status)
        self.tabs.addTab(self.up_tab, "アップロード")
        try:
            self.tabs.setCurrentIndex(int(self.mgr.settings.get("tab", 0)))
        except Exception:
            pass
        self.tabs.currentChanged.connect(self._tab_changed)

        for t in self.mgr.tasks:
            self.add_row(t)
        self.update_buttons()
        self.refresh_summary()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_summary)
        self.timer.start(1000)

        QTimer.singleShot(300, self.mgr.startup)
        QTimer.singleShot(350, self.up.startup)
        QTimer.singleShot(400, self.grab_clipboard)

    # ----------------------------------------------------- 入力系
    def grab_clipboard(self):
        """クリップボードにギガファイル便URLがあれば入力欄に入れておく"""
        if self.urls.toPlainText().strip():
            return
        text = QGuiApplication.clipboard().text() or ""
        known = {t.page_url for t in self.mgr.tasks}
        known |= {engine.normalize_url(t.url) for t in self.up.tasks if t.url}   # 自分が送ったURLは除く
        found = [u for u in text.split() if engine.is_gigafile_url(u)
                 and engine.normalize_url(u) not in known]
        if found:
            self.urls.setPlainText("\n".join(found))

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.grab_clipboard()

    def add(self):
        lines = [u for u in self.urls.toPlainText().split() if u.strip()]
        if not lines:
            return
        bad = [u for u in lines if not engine.is_gigafile_url(u)]
        good = [u for u in lines if engine.is_gigafile_url(u)]
        if bad:
            QMessageBox.warning(self, APP_NAME, "ギガファイル便のURLではないものを除外しました:\n"
                                + "\n".join(bad[:10]))
        if not good:
            return
        dest = self.mgr.settings["dest_dir"]
        if self.ask_each.isChecked():
            d = QFileDialog.getExistingDirectory(self, "このダウンロードの保存先", dest)
            if not d:
                return
            dest = d
            self.remember_dir(d)
        key = self.key.text().strip()
        for u in good:
            self.mgr.add_url(u, key, dest)
        self.urls.clear()
        self.key.clear()
        self.status.setText(f"{len(good)}件のURLを解析中…")

    # ----------------------------------------------------- 保存先
    def recent_dirs(self):
        cur = self.mgr.settings["dest_dir"]
        rec = [d for d in self.mgr.settings.get("recent_dirs", []) if d != cur]
        return [cur] + rec[:MAX_RECENT - 1]

    def remember_dir(self, d, make_current=False):
        rec = [x for x in self.mgr.settings.get("recent_dirs", []) if x != d]
        self.mgr.settings["recent_dirs"] = ([d] + rec)[:MAX_RECENT]
        if make_current:
            self.mgr.settings["dest_dir"] = d
        self.mgr.save()
        self.fill_dest_combo()

    def fill_dest_combo(self):
        self.dest.blockSignals(True)
        self.dest.clear()
        for d in self.recent_dirs():
            p = Path(d)
            label = f"{p.name or d}　—　{p.parent}" if p.name else d
            self.dest.addItem(label, d)
            self.dest.setItemData(self.dest.count() - 1, d, Qt.ToolTipRole)
        self.dest.insertSeparator(self.dest.count())
        self.dest.addItem("別のフォルダを選ぶ…", OTHER)
        self.dest.setCurrentIndex(0)
        self.dest.setToolTip(self.mgr.settings["dest_dir"])
        self.dest.blockSignals(False)

    def dest_activated(self, idx):
        d = self.dest.itemData(idx)
        if d == OTHER:
            d = QFileDialog.getExistingDirectory(self, "保存先フォルダ",
                                                 self.mgr.settings["dest_dir"])
            if not d:
                self.fill_dest_combo()
                return
        if d:
            self.remember_dir(d, make_current=True)

    def act_dest(self):
        s = [t for t in self.selected() if t.status not in (DONE, MOVING)]
        if not s:
            return
        d = QFileDialog.getExistingDirectory(self, f"{len(s)}件の保存先を変更", s[0].dest_dir)
        if not d:
            return
        self.remember_dir(d)
        for t in s:
            self.mgr.change_dest(t, d)

    def set_setting(self, k, v):
        self.mgr.settings[k] = v
        self.mgr.save()

    def set_parallel(self, n):
        self.mgr.max_parallel = n
        self.mgr.settings["max_parallel"] = n
        self.mgr.save()
        self.mgr._schedule()

    def set_auto(self, on):
        self.mgr.settings["auto_resume"] = on
        self.mgr.save()

    # ----------------------------------------------------- 一覧
    def add_row(self, t):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.row_of[t.id] = r
        name = QTableWidgetItem(t.name)
        name.setData(Qt.UserRole, t.id)
        name.setToolTip(f"{t.name}\n{t.page_url}\n保存先: {t.dest_dir}")
        self.table.setItem(r, 0, name)
        for c in (1, 3, 4, 5, 6):
            self.table.setItem(r, c, QTableWidgetItem(""))
        bar = QProgressBar(); bar.setRange(0, 1000); bar.setTextVisible(True)
        self.table.setCellWidget(r, 2, bar)
        self.bars[t.id] = bar
        self.update_row(t)

    def update_row(self, t):
        r = self.row_of.get(t.id)
        if r is None:
            return
        self.table.item(r, 1).setText(human(t.total) if t.total else t.size_str)
        bar = self.bars[t.id]
        if t.total:
            frac = t.done_bytes / t.total
            bar.setValue(int(frac * 1000))
            bar.setFormat(f"{frac*100:.1f}%  {human(t.done_bytes)}")
        else:
            bar.setValue(1000 if t.status == DONE else 0)
            bar.setFormat("100%" if t.status == DONE else "-")
        running = t.status == RUNNING and t.speed > 0
        self.table.item(r, 3).setText(f"{human(t.speed)}/s" if running else "")
        eta = ""
        if running and t.total:
            eta = fmt_eta((t.total - t.done_bytes) / t.speed)
        self.table.item(r, 4).setText(eta)
        dest = self.table.item(r, 6)
        dest.setText(Path(t.dest_dir).name or t.dest_dir)
        dest.setToolTip(t.dest_dir)
        self.table.item(r, 0).setToolTip(f"{t.name}\n{t.page_url}\n保存先: {t.dest_dir}")
        st = t.status + (f"（{t.message}）" if t.message else "")
        it = self.table.item(r, 5)
        it.setText(st)
        it.setToolTip(st)
        color = {DONE: "#2fa84f", ERROR: "#e5484d", RETRYING: "#d39e00"}.get(t.status)
        it.setForeground(QBrush(QColor(color)) if color else self.palette().text())

    def rebuild(self):
        self.table.setRowCount(0)
        self.row_of.clear()
        self.bars.clear()
        for t in self.mgr.tasks:
            self.add_row(t)
        self.update_buttons()

    def task_by_id(self, tid):
        return next((t for t in self.mgr.tasks if t.id == tid), None)

    def selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        out = []
        for r in rows:
            it = self.table.item(r, 0)
            t = it and self.task_by_id(it.data(Qt.UserRole))
            if t:
                out.append(t)
        return out

    def update_buttons(self):
        s = self.selected()
        st = {t.status for t in s}
        self.b_pause.setEnabled(bool(st & {RUNNING, RETRYING, QUEUED}))
        self.b_resume.setEnabled(bool(st & {PAUSED, ERROR}))
        self.b_key.setEnabled(bool(s) and not st & {DONE, MOVING})
        self.b_dest.setEnabled(bool(s) and bool(st - {DONE, MOVING}))
        self.b_remove.setEnabled(bool(s) and MOVING not in st)
        self.b_show.setEnabled(bool(s))

    def _tab_changed(self, i):
        self.mgr.settings["tab"] = i
        self.mgr.save()
        self.refresh_summary()

    def refresh_summary(self):
        ts = self.mgr.tasks
        active = [t for t in ts if t.status in (RUNNING, RETRYING)]
        waiting = sum(1 for t in ts if t.status == QUEUED)
        speed = sum(t.speed for t in active)
        us = self.up.tasks
        u_active = [t for t in us if t.status in (uploader.RUNNING, uploader.RETRYING, uploader.VERIFYING)]
        u_wait = sum(1 for t in us if t.status == uploader.QUEUED)
        self.caff.set(bool(active or waiting or u_active or u_wait))
        parts = []
        if active or waiting:
            parts.append(f"ダウンロード {len(active)}件（待機 {waiting}）{human(speed)}/s")
        if u_active or u_wait:
            parts.append(f"アップロード {len(u_active)}件（待機 {u_wait}）"
                         f"{human(sum(t.speed for t in u_active))}/s")
        if parts:
            self.status.setText(" ／ ".join(parts) + "　— スリープ防止中")
        elif self.tabs.currentIndex() == 1:
            done = sum(1 for t in us if t.status == uploader.DONE)
            self.status.setText(f"アップロード {done}/{len(us)}件 完了" if us else
                                "ファイルをドラッグ＆ドロップしてアップロード")
        elif ts:
            self.status.setText(f"{sum(1 for t in ts if t.status == DONE)}/{len(ts)}件 完了")
        else:
            self.status.setText("URLを貼り付けて「追加してダウンロード」")

    # ----------------------------------------------------- エンジンからのイベント
    def on_event(self, ev, t):
        if ev.startswith("up_"):
            self.up_tab.on_event(ev, t)
            return
        if ev == "added":
            self.add_row(t)
            self.status.setText(f"追加: {t.name}")
        elif ev == "resolve_error":
            url, msg = t
            QMessageBox.warning(self, APP_NAME, f"このURLを読み込めませんでした。\n{url}\n\n{msg}")
        elif ev == "update":
            self.update_row(t)
        elif ev == "done":
            self.update_row(t)
            self.batch_done.append(t.name)
            notify("ダウンロード完了", t.name)
        elif ev == "error":
            self.update_row(t)
            notify("ダウンロードできませんでした", f"{t.name}: {t.message}", error=True)
        elif ev == "all_done":
            if len(self.batch_done) > 1:
                notify("すべて完了", f"{len(self.batch_done)}個のファイルをダウンロードしました")
            self.batch_done.clear()
        self.update_buttons()

    # ----------------------------------------------------- 操作
    def act_pause(self):
        for t in self.selected():
            self.mgr.pause(t)

    def act_resume(self):
        for t in self.selected():
            self.mgr.resume(t)

    def act_key(self):
        s = [t for t in self.selected() if t.status != DONE]
        if not s:
            return
        key, ok = QInputDialog.getText(self, APP_NAME, "ダウンロード用パスワード:",
                                       QLineEdit.Normal, s[0].key)
        if not ok:
            return
        for t in s:
            if t.status in (RUNNING, RETRYING, QUEUED):
                t.key = key.strip()
            else:
                self.mgr.resume(t, key=key.strip())

    def act_remove(self):
        s = self.selected()
        if not s:
            return
        partial = [t for t in s if t.status != DONE]
        if partial:
            r = QMessageBox.question(
                self, APP_NAME,
                f"{len(s)}件を一覧から削除します。\n"
                f"未完了の{len(partial)}件は途中まで落としたデータも削除されます。",
                QMessageBox.Ok | QMessageBox.Cancel)
            if r != QMessageBox.Ok:
                return
        for t in s:
            self.mgr.remove(t)   # 完了済みのファイル本体は消さない
        self.rebuild()

    def act_show(self):
        for t in self.selected()[:1]:
            reveal(t.final_path if t.status == DONE else t.part_path)

    def clear_done(self):
        self.mgr.clear_done()
        self.rebuild()

    def double_clicked(self, row, col):
        t = self.task_by_id(self.table.item(row, 0).data(Qt.UserRole))
        if t and t.status == DONE:
            reveal(t.final_path)

    def context_menu(self, pos):
        if not self.selected():
            return
        m = QMenu(self)
        for label, fn, btn in (("一時停止", self.act_pause, self.b_pause),
                               ("再開", self.act_resume, self.b_resume),
                               ("パスワード入力…", self.act_key, self.b_key),
                               ("保存先を変更…", self.act_dest, self.b_dest),
                               (f"{pu.FILE_MANAGER}で表示", self.act_show, self.b_show),
                               ("削除", self.act_remove, self.b_remove)):
            a = m.addAction(label)
            a.setEnabled(btn.isEnabled())
            a.triggered.connect(fn)
        m.addSeparator()
        a = m.addAction("保存先フォルダを開く")
        a.triggered.connect(lambda: pu.open_folder(self.selected()[0].dest_dir))
        a = m.addAction("ページURLをコピー")
        a.triggered.connect(lambda: QGuiApplication.clipboard().setText(
            "\n".join(sorted({t.page_url for t in self.selected()}))))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def closeEvent(self, e):
        active = [t for t in self.mgr.tasks if t.status in (RUNNING, RETRYING, QUEUED)]
        up_active = [t for t in self.up.tasks if t.status in
                     (uploader.RUNNING, uploader.RETRYING, uploader.QUEUED, uploader.VERIFYING)]
        if active or up_active:
            r = QMessageBox.question(
                self, APP_NAME,
                f"ダウンロード{len(active)}件・アップロード{len(up_active)}件が途中です。終了しますか？\n"
                "（次回起動時に続きから再開できます）",
                QMessageBox.Ok | QMessageBox.Cancel)
            if r != QMessageBox.Ok:
                e.ignore()
                return
        self.mgr.shutdown()
        self.up.shutdown()
        self.caff.set(False)
        if self.tray:
            self.tray.hide()
        e.accept()


SOCK_NAME = "GigaDL-" + "".join(c for c in getpass.getuser() if c.isalnum())


def acquire_single_instance():
    """GigaDLが2つ同時に動くと同じファイルを取り合うので、1つだけにする。
    すでに動いていれば、そのウィンドウを前に出すよう頼んで True を返す。"""
    lock = QLockFile(str(pu.data_dir() / "gigadl.lock"))
    lock.setStaleLockTime(0)          # 期限切れ扱いにはせず、持ち主のプロセスが消えたときだけ解除
    if lock.tryLock(300):
        return lock, False
    sock = QLocalSocket()
    sock.connectToServer(SOCK_NAME)
    if sock.waitForConnected(1500):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        return None, True
    if lock.removeStaleLockFile() and lock.tryLock(300):   # 前回が異常終了していた場合
        return lock, False
    return None, True


def main():
    # エラーをログに残す（Windowsのpythonwは画面出力が無いため）
    if sys.stderr is None or pu.IS_WIN:
        try:
            sys.stderr = open(pu.data_dir() / "gigadl.log", "a", encoding="utf-8", buffering=1)
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    if pu.IS_WIN:
        # タスクバーでPythonではなくGigaDLのアイコンを出す
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("local.gigadl")
        except Exception:
            pass
    app.setWindowIcon(app_icon())
    lock, running = acquire_single_instance()
    if running:
        return            # すでに動いているGigaDLを前に出した
    app._gigadl_lock = lock
    w = Main()
    server = QLocalServer()
    QLocalServer.removeServer(SOCK_NAME)
    server.listen(SOCK_NAME)

    def bring_front():
        c = server.nextPendingConnection()
        if c:
            c.readyRead.connect(lambda: None)
        w.showNormal()
        w.raise_()
        w.activateWindow()
    server.newConnection.connect(bring_front)
    app._gigadl_server = server
    w.show()
    rc = app.exec()
    server.close()
    lock.unlock()
    sys.exit(rc)


if __name__ == "__main__":
    main()
