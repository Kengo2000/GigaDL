"""アップロードのタブ"""
from pathlib import Path

from PySide6.QtCore import QRegularExpression, Qt, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QGuiApplication, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import platform_util as pu
import uploader
from engine import human
from uploader import DONE, ERROR, PAUSED, QUEUED, RETRYING, RUNNING, VERIFYING

COLS = ["ファイル名", "サイズ", "進捗", "速度", "残り", "状態", "URL"]
GCOLS = ["まとめの名前", "ファイル数", "合計", "状態", "まとめURL"]


class GroupDialog(QDialog):
    """まとめの名前とパスワードを聞く"""

    def __init__(self, parent, count, total, name="", key="", key_required=False):
        super().__init__(parent)
        self.setWindowTitle("1つのURLにまとめる")
        f = QFormLayout(self)
        f.addRow(QLabel(f"{count}個のファイル（合計 {human(total)}）を、1つのダウンロードURLにまとめます。"))
        self.name = QLineEdit(name)
        self.name.setPlaceholderText("空欄ならギガファイル便が自動で付けます")
        self.key = QLineEdit(key)
        self.key.setMaxLength(4)
        self.key.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Za-z]{0,4}")))
        self.key.setPlaceholderText("必須（英数1〜4文字）" if key_required else "なし（英数1〜4文字）")
        f.addRow("まとめの名前:", self.name)
        f.addRow("パスワード:", self.key)
        if key_required:
            f.addRow(QLabel("パスワード付きのファイルを含むので、まとめにもパスワードが必要です。"))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


def fmt_eta(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec//3600}時間{sec%3600//60}分"
    if sec >= 60:
        return f"{sec//60}分{sec%60}秒"
    return f"{sec}秒"


class DropArea(QFrame):
    def __init__(self, on_paths, parent=None):
        super().__init__(parent)
        self.on_paths = on_paths
        self.setAcceptDrops(True)
        self.setMinimumHeight(86)
        self.setObjectName("drop")
        self._style(False)
        lay = QVBoxLayout(self)
        self.label = QLabel("ここにファイルやフォルダをドラッグ＆ドロップ")
        self.label.setAlignment(Qt.AlignCenter)
        f = self.label.font()
        f.setPointSize(f.pointSize() + 2)
        self.label.setFont(f)
        sub = QLabel("フォルダは中のファイルを1つずつ送ります（大きなムービーもそのまま1ファイルで届きます）")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet("color: gray;")
        lay.addWidget(self.label)
        lay.addWidget(sub)

    def _style(self, hover):
        c = "#e5533d" if hover else "#888"
        self.setStyleSheet(f"#drop {{ border: 2px dashed {c}; border-radius: 10px; }}")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._style(True)

    def dragLeaveEvent(self, e):
        self._style(False)

    def dropEvent(self, e):
        self._style(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.on_paths(paths)


class UploadTab(QWidget):
    def __init__(self, mgr: uploader.UploadManager, status_label, parent=None):
        super().__init__(parent)
        self.mgr = mgr
        self.status = status_label
        self.row_of, self.bars = {}, {}
        self.batch_done = []
        v = QVBoxLayout(self)

        # --- 追加
        self.drop = DropArea(self.add_paths)
        v.addWidget(self.drop)

        h = QHBoxLayout()
        b = QPushButton("ファイルを選ぶ…"); b.clicked.connect(self.pick_files); h.addWidget(b)
        b = QPushButton("フォルダを選ぶ…"); b.clicked.connect(self.pick_folder); h.addWidget(b)
        h.addSpacing(16)
        h.addWidget(QLabel("保持期限:"))
        self.lifetime = QComboBox()
        for d in uploader.LIFETIMES:
            self.lifetime.addItem(f"{d}日", d)
        cur = int(self.mgr.settings.get("lifetime", 7))
        self.lifetime.setCurrentIndex(max(0, self.lifetime.findData(cur)))
        self.lifetime.currentIndexChanged.connect(self._save_lifetime)
        h.addWidget(self.lifetime)
        h.addSpacing(16)
        h.addWidget(QLabel("パスワード:"))
        self.key = QLineEdit()
        self.key.setPlaceholderText("なし（英数1〜4文字）")
        self.key.setMaxLength(4)
        self.key.setFixedWidth(150)
        self.key.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Za-z]{0,4}")))
        h.addWidget(self.key)
        h.addSpacing(16)
        self.matome = QCheckBox("まとめて1つのURLにする")
        self.matome.setToolTip("複数のファイルを送るとき、全部送り終わったら1つのダウンロードURLにまとめます")
        self.matome.setChecked(bool(self.mgr.settings.get("matome", False)))
        self.matome.toggled.connect(lambda on: (self.mgr.settings.__setitem__("matome", on), self.mgr.save()))
        h.addWidget(self.matome)
        h.addStretch(1)
        v.addLayout(h)

        # --- 一覧
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.cellDoubleClicked.connect(lambda r, c: self.act_copy())
        self.table.itemSelectionChanged.connect(self.update_buttons)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c, w in ((1, 80), (2, 170), (3, 90), (4, 80), (5, 200), (6, 230)):
            hh.setSectionResizeMode(c, QHeaderView.Interactive)
            self.table.setColumnWidth(c, w)
        v.addWidget(self.table, 1)

        # --- 操作
        h = QHBoxLayout()
        self.b_copy = QPushButton("URLをコピー"); self.b_copy.clicked.connect(self.act_copy)
        self.b_share = QPushButton("送る文面をコピー"); self.b_share.clicked.connect(self.act_share)
        self.b_share.setToolTip("ファイル名・URL・パスワード・期限をまとめてコピー（メールやLINEに貼れます）")
        self.b_open = QPushButton("ブラウザで開く"); self.b_open.clicked.connect(self.act_open)
        self.b_pause = QPushButton("一時停止"); self.b_pause.clicked.connect(self.act_pause)
        self.b_resume = QPushButton("再開"); self.b_resume.clicked.connect(self.act_resume)
        self.b_key = QPushButton("パスワード設定…"); self.b_key.clicked.connect(self.act_key)
        self.b_group = QPushButton("選んだファイルをまとめる…"); self.b_group.clicked.connect(self.act_group)
        self.b_remove = QPushButton("一覧から削除"); self.b_remove.clicked.connect(self.act_remove)
        for b in (self.b_copy, self.b_share, self.b_open, self.b_group, self.b_pause, self.b_resume, self.b_key, self.b_remove):
            h.addWidget(b)
        h.addStretch(1)
        b = QPushButton("完了を一覧から消す"); b.clicked.connect(self.clear_done); h.addWidget(b)
        v.addLayout(h)

        # --- まとめたURL
        gl = QLabel("まとめたURL（複数ファイルを1つのURLで渡せます）")
        f = gl.font(); f.setBold(True); gl.setFont(f)
        v.addWidget(gl)
        self.gtable = QTableWidget(0, len(GCOLS))
        self.gtable.setHorizontalHeaderLabels(GCOLS)
        self.gtable.verticalHeader().hide()
        self.gtable.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.gtable.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.gtable.setMaximumHeight(130)
        self.gtable.cellDoubleClicked.connect(lambda r, c: self.g_copy())
        self.gtable.itemSelectionChanged.connect(self.update_buttons)
        gh = self.gtable.horizontalHeader()
        gh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c, w_ in ((1, 70), (2, 90), (3, 220), (4, 300)):
            gh.setSectionResizeMode(c, QHeaderView.Interactive)
            self.gtable.setColumnWidth(c, w_)
        v.addWidget(self.gtable)
        h = QHBoxLayout()
        self.g_b_copy = QPushButton("まとめURLをコピー"); self.g_b_copy.clicked.connect(self.g_copy)
        self.g_b_share = QPushButton("まとめの送る文面をコピー"); self.g_b_share.clicked.connect(self.g_share)
        self.g_b_open = QPushButton("ブラウザで開く"); self.g_b_open.clicked.connect(self.g_open)
        self.g_b_retry = QPushButton("もう一度まとめる"); self.g_b_retry.clicked.connect(self.g_retry)
        self.g_b_remove = QPushButton("一覧から削除"); self.g_b_remove.clicked.connect(self.g_remove)
        for b in (self.g_b_copy, self.g_b_share, self.g_b_open, self.g_b_retry, self.g_b_remove):
            h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)

        note = QLabel("送信は公式サイトと同じく100MBずつ順番に行います。途中で切れても切れた分だけ送り直し、"
                      "アプリを閉じても次回は続きから再開します。")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        v.addWidget(note)

        for t in self.mgr.tasks:
            self.add_row(t)
        self.rebuild_groups()
        self.update_buttons()

    # ------------------------------------------------- 追加
    def _save_lifetime(self):
        self.mgr.settings["lifetime"] = self.lifetime.currentData()
        self.mgr.save()

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "アップロードするファイル")
        if paths:
            self.add_paths(paths)

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "アップロードするフォルダ")
        if d:
            self.add_paths([d])

    def add_paths(self, paths):
        key = self.key.text().strip()
        if not uploader.valid_dlkey(key):
            QMessageBox.warning(self, pu.APP_NAME, "パスワードは半角英数1〜4文字です。")
            return
        files = self.mgr.expand(paths)
        if not files:
            QMessageBox.information(self, pu.APP_NAME, "送れるファイルがありませんでした。")
            return
        total = sum(p.stat().st_size for p in files)
        group, gname = False, ""
        if self.matome.isChecked() and len(files) >= 2:
            default = Path(paths[0]).name if len(paths) == 1 and Path(paths[0]).is_dir() else ""
            dlg = GroupDialog(self, len(files), total, default, key)
            dlg.key.setReadOnly(True)
            dlg.key.setToolTip("上の「パスワード」欄と同じになります")
            if dlg.exec() != QDialog.Accepted:
                return
            group, gname = True, dlg.name.text().strip()
        elif len(files) > 1 or total > 20 * 1024 ** 3:
            msg = (f"{len(files)}個のファイル（合計 {human(total)}）をアップロードします。\n"
                   f"保持期限: {self.lifetime.currentData()}日　パスワード: {key or 'なし'}")
            if QMessageBox.question(self, pu.APP_NAME, msg,
                                    QMessageBox.Ok | QMessageBox.Cancel) != QMessageBox.Ok:
                return
        try:
            added = self.mgr.add_files(files, self.lifetime.currentData(), key, group, gname)
        except uploader.UploadError as ex:
            QMessageBox.warning(self, pu.APP_NAME, str(ex))
            return
        self.status.setText(f"アップロードに{len(added)}件追加しました")

    # ------------------------------------------------- 一覧
    def add_row(self, t):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.row_of[t.id] = r
        it = QTableWidgetItem(t.name)
        it.setData(Qt.UserRole, t.id)
        it.setToolTip(t.path)
        self.table.setItem(r, 0, it)
        for c in (1, 3, 4, 5, 6):
            self.table.setItem(r, c, QTableWidgetItem(""))
        bar = QProgressBar(); bar.setRange(0, 1000)
        self.table.setCellWidget(r, 2, bar)
        self.bars[t.id] = bar
        self.update_row(t)

    def update_row(self, t):
        r = self.row_of.get(t.id)
        if r is None:
            return
        self.table.item(r, 1).setText(human(t.size))
        bar = self.bars[t.id]
        frac = (t.sent / t.size) if t.size else 0
        bar.setValue(int(frac * 1000))
        bar.setFormat(f"{frac*100:.1f}%  {human(t.sent)}")
        running = t.status == RUNNING and t.speed > 0
        self.table.item(r, 3).setText(f"{human(t.speed)}/s" if running else "")
        self.table.item(r, 4).setText(fmt_eta((t.size - t.sent) / t.speed) if running else "")
        extra = []
        if t.status == RUNNING:
            extra.append(f"{min(t.next_chunk + 1, t.chunks)}/{t.chunks}")
        if t.message:
            extra.append(t.message)
        if t.dlkey:
            extra.append(f"PW {t.dlkey}")
        g = self.mgr.group_of(t)
        if g:
            extra.append(f"まとめ: {g.title}")
        st = t.status + (f"（{' / '.join(extra)}）" if extra else "")
        it = self.table.item(r, 5)
        it.setText(st)
        it.setToolTip(st)
        color = {DONE: "#2fa84f", ERROR: "#e5484d", RETRYING: "#d39e00"}.get(t.status)
        it.setForeground(QBrush(QColor(color)) if color else self.palette().text())
        u = self.table.item(r, 6)
        u.setText(t.url if t.status == DONE else "")
        u.setToolTip(t.url)

    def rebuild(self):
        self.table.setRowCount(0)
        self.row_of.clear()
        self.bars.clear()
        for t in self.mgr.tasks:
            self.add_row(t)
        self.rebuild_groups()
        self.update_buttons()

    # ------------------------------------------------- まとめ一覧
    def rebuild_groups(self):
        self.gtable.setRowCount(0)
        for g in self.mgr.groups:
            self.update_group(g)

    def _grow(self, g):
        for r in range(self.gtable.rowCount()):
            it = self.gtable.item(r, 0)
            if it and it.data(Qt.UserRole) == g.id:
                return r
        return None

    def update_group(self, g):
        if g not in self.mgr.groups:
            return
        r = self._grow(g)
        if r is None:
            r = self.gtable.rowCount()
            self.gtable.insertRow(r)
            for c in range(len(GCOLS)):
                self.gtable.setItem(r, c, QTableWidgetItem(""))
            self.gtable.item(r, 0).setData(Qt.UserRole, g.id)
        ms = self.mgr.members(g)
        self.gtable.item(r, 0).setText(g.title)
        self.gtable.item(r, 0).setToolTip("\n".join(t.name for t in ms))
        self.gtable.item(r, 1).setText(str(len(g.member_ids)))
        self.gtable.item(r, 2).setText(human(sum(t.size for t in ms)))
        st = g.status + (f"（{g.message}）" if g.message else "") + (f"  PW {g.dlkey}" if g.dlkey else "")
        it = self.gtable.item(r, 3)
        it.setText(st)
        it.setToolTip(st)
        color = {DONE: "#2fa84f", ERROR: "#e5484d"}.get(g.status)
        it.setForeground(QBrush(QColor(color)) if color else self.palette().text())
        self.gtable.item(r, 4).setText(g.url)
        self.gtable.item(r, 4).setToolTip(g.url)
        for t in ms:
            self.update_row(t)

    def g_selected(self):
        rows = sorted({i.row() for i in self.gtable.selectedIndexes()})
        ids = [self.gtable.item(r, 0).data(Qt.UserRole) for r in rows if self.gtable.item(r, 0)]
        return [g for g in self.mgr.groups if g.id in ids]

    def g_copy(self):
        gs = [g for g in self.g_selected() if g.status == DONE]
        if gs:
            QGuiApplication.clipboard().setText("\n".join(g.url for g in gs))
            self.status.setText("まとめURLをコピーしました")

    def g_share(self):
        gs = [g for g in self.g_selected() if g.status == DONE]
        if gs:
            QGuiApplication.clipboard().setText(
                "\n\n".join(uploader.share_text_group(g, self.mgr.members(g)) for g in gs))
            self.status.setText("まとめの送る文面をコピーしました（ファイル一覧・URL・パスワード・期限入り）")

    def g_open(self):
        for g in [g for g in self.g_selected() if g.status == DONE][:3]:
            QDesktopServices.openUrl(QUrl(g.url))

    def g_retry(self):
        for g in self.g_selected():
            self.mgr.retry_group(g)

    def g_remove(self):
        gs = self.g_selected()
        if gs and QMessageBox.question(
                self, pu.APP_NAME, f"{len(gs)}件のまとめを一覧から削除します。\n"
                "（ギガファイル便上のまとめURLと、各ファイルのURLはそのまま使えます）",
                QMessageBox.Ok | QMessageBox.Cancel) == QMessageBox.Ok:
            for g in gs:
                self.mgr.remove_group(g)
            self.rebuild()

    def act_group(self):
        s = [t for t in self.selected() if t.status != ERROR]
        if len(s) < 2:
            return
        keyed = [t.dlkey for t in s if t.dlkey]
        dlg = GroupDialog(self, len(s), sum(t.size for t in s), "", keyed[0] if keyed else "",
                          key_required=bool(keyed))
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            self.mgr.make_group(s, dlg.name.text().strip(), dlg.key.text().strip())
        except uploader.UploadError as ex:
            QMessageBox.warning(self, pu.APP_NAME, str(ex))
            return
        self.status.setText(f"{len(s)}個のファイルをまとめます（送信中のものは送り終わってから）")

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
        done = [t for t in s if t.status == DONE]
        self.b_copy.setEnabled(bool(done))
        self.b_share.setEnabled(bool(done) or any(t.status == DONE for t in self.mgr.tasks))
        self.b_open.setEnabled(bool(done))
        self.b_pause.setEnabled(bool(st & {RUNNING, RETRYING, QUEUED}))
        self.b_resume.setEnabled(bool(st & {PAUSED, ERROR}))
        self.b_key.setEnabled(bool(s) and VERIFYING not in st)
        self.b_group.setEnabled(len([t for t in s if t.status != ERROR]) >= 2)
        gs = self.g_selected()
        gdone = [g for g in gs if g.status == DONE]
        self.g_b_copy.setEnabled(bool(gdone))
        self.g_b_share.setEnabled(bool(gdone))
        self.g_b_open.setEnabled(bool(gdone))
        self.g_b_retry.setEnabled(any(g.status == ERROR for g in gs))
        self.g_b_remove.setEnabled(bool(gs))
        self.b_remove.setEnabled(bool(s))

    # ------------------------------------------------- イベント
    def on_event(self, ev, t):
        if ev == "up_added":
            self.add_row(t)
        elif ev == "up_update":
            self.update_row(t)
        elif ev == "up_done":
            self.update_row(t)
            self.batch_done.append(t)
            pu.notify("アップロード完了", f"{t.name}\n{t.url}")
        elif ev == "up_error":
            self.update_row(t)
            pu.notify("アップロードできませんでした", f"{t.name}: {t.message}", error=True)
        elif ev == "up_group":
            self.update_group(t)
        elif ev == "up_group_done":
            self.update_group(t)
            pu.notify("まとめURLができました", f"{t.title}（{len(t.member_ids)}ファイル）\n{t.url}")
        elif ev == "up_group_error":
            self.update_group(t)
            pu.notify("まとめURLを作れませんでした", t.message, error=True)
        elif ev == "up_all_done":
            if len(self.batch_done) > 1:
                pu.notify("アップロードがすべて完了",
                          f"{len(self.batch_done)}個のファイル。「送る文面をコピー」で相手に送れます")
            self.batch_done.clear()
        self.update_buttons()

    # ------------------------------------------------- 操作
    def _done_targets(self):
        s = [t for t in self.selected() if t.status == DONE]
        return s or [t for t in self.mgr.tasks if t.status == DONE]

    def act_copy(self):
        s = [t for t in self.selected() if t.status == DONE]
        if s:
            QGuiApplication.clipboard().setText("\n".join(t.url for t in s))
            self.status.setText(f"URLを{len(s)}件コピーしました")

    def act_share(self):
        s = self._done_targets()
        if s:
            QGuiApplication.clipboard().setText(uploader.share_text(s))
            self.status.setText(f"{len(s)}件分の送る文面をコピーしました（URL・パスワード・期限入り）")

    def act_open(self):
        for t in [t for t in self.selected() if t.status == DONE][:3]:
            QDesktopServices.openUrl(QUrl(t.url))

    def act_pause(self):
        for t in self.selected():
            self.mgr.pause(t)

    def act_resume(self):
        for t in self.selected():
            self.mgr.resume(t)

    def act_key(self):
        s = self.selected()
        if not s:
            return
        key, ok = QInputDialog.getText(self, pu.APP_NAME,
                                       "ダウンロード用パスワード（半角英数1〜4文字）:",
                                       QLineEdit.Normal, s[0].dlkey)
        if not ok:
            return
        key = key.strip()
        if not key or not uploader.valid_dlkey(key):
            QMessageBox.warning(self, pu.APP_NAME, "パスワードは半角英数1〜4文字です。")
            return
        for t in s:
            self.mgr.set_dlkey(t, key)
            self.update_row(t)

    def act_remove(self):
        s = self.selected()
        if not s:
            return
        partial = [t for t in s if t.status != DONE]
        if partial and QMessageBox.question(
                self, pu.APP_NAME,
                f"{len(s)}件を一覧から削除します。\n送信途中の{len(partial)}件は中止されます。"
                "（パソコンの元ファイルは消えません）",
                QMessageBox.Ok | QMessageBox.Cancel) != QMessageBox.Ok:
            return
        for t in s:
            self.mgr.remove(t)
        self.rebuild()

    def clear_done(self):
        self.mgr.clear_done()
        self.rebuild()

    def context_menu(self, pos):
        if not self.selected():
            return
        m = QMenu(self)
        for label, fn, btn in (("URLをコピー", self.act_copy, self.b_copy),
                               ("送る文面をコピー", self.act_share, self.b_share),
                               ("ブラウザで開く", self.act_open, self.b_open),
                               ("一時停止", self.act_pause, self.b_pause),
                               ("再開", self.act_resume, self.b_resume),
                               ("パスワード設定…", self.act_key, self.b_key),
                               ("選んだファイルをまとめる…", self.act_group, self.b_group),
                               ("一覧から削除", self.act_remove, self.b_remove)):
            a = m.addAction(label)
            a.setEnabled(btn.isEnabled())
            a.triggered.connect(fn)
        m.addSeparator()
        a = m.addAction(f"元ファイルを{pu.FILE_MANAGER}で表示")
        a.triggered.connect(lambda: pu.reveal(self.selected()[0].path))
        m.exec(self.table.viewport().mapToGlobal(pos))
