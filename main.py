import sys
import os
import shutil
import struct
import threading
import bisect
import numpy as np
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QThread, Signal, QObject, QSettings, QUrl, QByteArray, QMimeData
from PySide6.QtGui import QPixmap, QImage, QAction, QKeySequence, QShortcut, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QProgressBar, QFileDialog, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QComboBox, QSlider, QGroupBox,
    QHeaderView, QMessageBox, QSplitter, QScrollArea, QCheckBox,
    QStatusBar, QStyle, QStyleFactory, QSizePolicy, QSpinBox, QMenu, QDialog,
    QProgressDialog
)

from db import Database, ImageRecord, VideoRecord
from indexer import (
    index_directory, IndexProgress, hamming_distance, quality_score, hex_to_bool_array,
    batch_hamming_distances, compute_color_histogram, histogram_similarity,
    video_similarity_distance,
)


def open_path(path):
    """Open a file or folder with the OS default application (cross-platform)."""
    if not path or not os.path.exists(path):
        return False
    return QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def set_cut_mime_effect(mime):
    """Flag clipboard content as a 'cut' (move) operation.

    Only Windows Explorer understands the 'Preferred DropEffect' format; other
    file managers rely solely on the URL list, which is already set.
    """
    if sys.platform != "win32":
        return
    # DROPEFFECT_MOVE = 2
    effect = QByteArray(struct.pack("<I", 2))
    mime.setData('application/x-qt-windows-mime;value="Preferred DropEffect"', effect)


def _parse_taken_at(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


class CaseInsensitiveTreeWidgetItem(QTreeWidgetItem):
    def __lt__(self, other):
        tree = self.treeWidget()
        col = tree.sortColumn() if tree else 0
        left = self.text(col)
        right = other.text(col)
        try:
            return float(left.replace(",", "")) < float(right.replace(",", ""))
        except ValueError:
            return left.lower() < right.lower()


class PannableScrollArea(QScrollArea):
    zoomRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._panning = False
        self._pan_start = None
        self._h_start = 0
        self._v_start = 0
        self.viewport().setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta != 0:
            steps = delta / 120.0
            self.zoomRequested.emit(1 if steps > 0 else -1)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._panning = True
            self._pan_start = event.pos()
            self._h_start = self.horizontalScrollBar().value()
            self._v_start = self.verticalScrollBar().value()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.pos() - self._pan_start
            self.horizontalScrollBar().setValue(self._h_start - delta.x())
            self.verticalScrollBar().setValue(self._v_start - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._panning:
            self._panning = False
            self.viewport().setCursor(Qt.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ImageComparePanel(QWidget):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.pixmap = QPixmap(path) if path and os.path.exists(path) else QPixmap()
        self.zoom = 1.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if path:
            info_text = os.path.dirname(path) + "\n" + os.path.basename(path)
        else:
            info_text = "(aucune image)"
        if not self.pixmap.isNull():
            info_text += f"  —  {self.pixmap.width()}x{self.pixmap.height()}"
            try:
                size_bytes = os.path.getsize(path)
                info_text += f"  —  {_fmt_size(size_bytes)}"
            except Exception:
                pass
        self.lbl_info = QLabel(info_text)
        self.lbl_info.setStyleSheet("font-weight: bold;")
        self.lbl_info.setWordWrap(True)
        layout.addWidget(self.lbl_info)

        self.scroll = PannableScrollArea()
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.setWidgetResizable(False)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.scroll.setWidget(self.image_label)
        layout.addWidget(self.scroll, 1)

        self.set_zoom(1.0, fit_first=True)

    def set_zoom(self, zoom, fit_first=False):
        if self.pixmap.isNull():
            self.image_label.setText("Apercu non disponible")
            return
        if fit_first:
            avail = self.scroll.viewport().size()
            if avail.width() > 0 and avail.height() > 0:
                w_ratio = avail.width() / self.pixmap.width()
                h_ratio = avail.height() / self.pixmap.height()
                zoom = min(w_ratio, h_ratio, 1.0)
        self.zoom = max(0.05, zoom)
        scaled = self.pixmap.scaled(
            int(self.pixmap.width() * self.zoom),
            int(self.pixmap.height() * self.zoom),
            Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.image_label.setPixmap(scaled)
        self.image_label.resize(scaled.size())


class ImageCompareDialog(QDialog):
    def __init__(self, left_path, right_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Comparaison visuelle")
        self.resize(1300, 800)

        layout = QVBoxLayout(self)

        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("Zoom:"))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(5, 400)
        self.slider.setValue(100)
        self.slider.valueChanged.connect(self._on_zoom_changed)
        ctrl_row.addWidget(self.slider, 1)
        self.lbl_zoom_pct = QLabel("100%")
        self.lbl_zoom_pct.setMinimumWidth(50)
        ctrl_row.addWidget(self.lbl_zoom_pct)
        btn_fit = QPushButton("Ajuster")
        btn_fit.clicked.connect(self._fit_both)
        ctrl_row.addWidget(btn_fit)
        btn_100 = QPushButton("100%")
        btn_100.clicked.connect(lambda: self.slider.setValue(100))
        ctrl_row.addWidget(btn_100)
        self.chk_sync_scroll = QCheckBox("Synchroniser le defilement")
        self.chk_sync_scroll.setChecked(True)
        ctrl_row.addWidget(self.chk_sync_scroll)
        layout.addLayout(ctrl_row)

        splitter = QSplitter(Qt.Horizontal)
        self.left_panel = ImageComparePanel(left_path)
        self.right_panel = ImageComparePanel(right_path)
        splitter.addWidget(self.left_panel)
        splitter.addWidget(self.right_panel)
        splitter.setSizes([650, 650])
        layout.addWidget(splitter, 1)

        btn_close = QPushButton("Fermer")
        btn_close.clicked.connect(self.close)
        layout.addWidget(btn_close, 0, Qt.AlignRight)

        self.left_panel.scroll.horizontalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll(self.left_panel, self.right_panel, "h", v))
        self.left_panel.scroll.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll(self.left_panel, self.right_panel, "v", v))
        self.right_panel.scroll.horizontalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll(self.right_panel, self.left_panel, "h", v))
        self.right_panel.scroll.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll(self.right_panel, self.left_panel, "v", v))
        self._syncing = False

        self.left_panel.scroll.zoomRequested.connect(self._on_wheel_zoom)
        self.right_panel.scroll.zoomRequested.connect(self._on_wheel_zoom)

    def _on_wheel_zoom(self, steps):
        new_val = self.slider.value() + steps * 10
        new_val = max(self.slider.minimum(), min(self.slider.maximum(), new_val))
        self.slider.setValue(new_val)

    def _sync_scroll(self, src_panel, dst_panel, axis, value):
        if not self.chk_sync_scroll.isChecked() or self._syncing:
            return
        self._syncing = True
        try:
            src_bar = src_panel.scroll.horizontalScrollBar() if axis == "h" else src_panel.scroll.verticalScrollBar()
            dst_bar = dst_panel.scroll.horizontalScrollBar() if axis == "h" else dst_panel.scroll.verticalScrollBar()
            if src_bar.maximum() > 0:
                ratio = value / src_bar.maximum()
            else:
                ratio = 0
            dst_bar.setValue(int(ratio * dst_bar.maximum()))
        finally:
            self._syncing = False

    def _on_zoom_changed(self, value):
        self.lbl_zoom_pct.setText(f"{value}%")
        self.left_panel.set_zoom(value / 100.0)
        self.right_panel.set_zoom(value / 100.0)

    def _fit_both(self):
        self.left_panel.set_zoom(1.0, fit_first=True)
        self.right_panel.set_zoom(1.0, fit_first=True)
        zoom_pct = int(round(min(self.left_panel.zoom, self.right_panel.zoom) * 100))
        self.slider.blockSignals(True)
        self.slider.setValue(zoom_pct)
        self.slider.blockSignals(False)
        self.lbl_zoom_pct.setText(f"{zoom_pct}%")
        self.left_panel.set_zoom(zoom_pct / 100.0)
        self.right_panel.set_zoom(zoom_pct / 100.0)


class IndexWorker(QObject):
    finished = Signal(object)
    progress = Signal(object)

    def __init__(self, db_path, directories, max_workers=None, force=False):
        super().__init__()
        self.db_path = db_path
        self.directories = directories  # list of (dir_path, recursive) tuples
        self.max_workers = max_workers
        self.force = force
        self.pause_event = threading.Event()
        self.pause_event.set()  # not paused

    def pause(self):
        self.pause_event.clear()

    def resume(self):
        self.pause_event.set()

    def run(self):
        db = Database(self.db_path)
        total_progress = IndexProgress()
        for d, recursive in self.directories:
            def cb(p):
                total_progress.skipped = p.skipped
                total_progress.current = p.current
                total_progress.total = p.total
                self.progress.emit(p)
            index_directory(db, d, cb, recursive=recursive, pause_event=self.pause_event, max_workers=self.max_workers, force=self.force)
        db.close()
        self.finished.emit(total_progress)


class QueryWorker(QObject):
    finished = Signal(list)
    progress = Signal(int, int)  # current, total

    NEAR_DUP_TIME_WINDOW = timedelta(seconds=120)
    NEAR_DUP_SIMILARITY_THRESHOLD = 0.92

    def __init__(self, db_path, target_dir, threshold, recursive=False, detect_rotation=False, detect_near_dup=False):
        super().__init__()
        self.db_path = db_path
        self.target_dir = target_dir
        self.threshold = threshold
        self.recursive = recursive
        self.detect_rotation = detect_rotation
        self.detect_near_dup = detect_near_dup

    def run(self):
        db = Database(self.db_path)
        disabled_dirs = db.get_disabled_dirs()

        def is_dir_disabled(d):
            for dd in disabled_dirs:
                if d == dd or d.startswith(dd.rstrip(os.sep) + os.sep):
                    return True
            return False

        target_images = db.get_images_in_directory(self.target_dir, recursive=self.recursive)
        other_images = db.get_images_not_in_directory(self.target_dir, recursive=self.recursive)
        other_images = [o for o in other_images if not is_dir_disabled(o.directory)]

        exact_dups = db.get_exact_duplicates_outside(self.target_dir, recursive=self.recursive)
        exact_dups = {k: [m for m in v if not is_dir_disabled(m.directory)] for k, v in exact_dups.items()}
        exact_dups = {k: v for k, v in exact_dups.items() if v}

        total = len(target_images)
        print(f"[query] target_dir={self.target_dir!r} target_images={total} other_images={len(other_images)}")

        # Pre-convert all other hashes to numpy matrix
        other_hashes = None
        if other_images:
            other_hashes = np.array([hex_to_bool_array(o.phash) for o in other_images])

        # For near-duplicate detection: sorted (taken_at, index) list for fast
        # time-window lookups among other_images.
        other_times = []
        if self.detect_near_dup:
            for idx, o in enumerate(other_images):
                t = _parse_taken_at(o.taken_at)
                if t is not None:
                    other_times.append((t, idx))
            other_times.sort(key=lambda x: x[0])
        other_time_keys = [t for t, _ in other_times]

        results = []
        batch_size = 500
        for batch_start in range(0, total, batch_size):
            batch_end = min(batch_start + batch_size, total)
            self.progress.emit(batch_start, total)

            batch = target_images[batch_start:batch_end]
            target_hashes = np.array([hex_to_bool_array(img.phash) for img in batch])

            if other_hashes is not None:
                dist_matrix = batch_hamming_distances(target_hashes, other_hashes)

                if self.detect_rotation:
                    nbits = target_hashes.shape[1]
                    zero_bits = np.zeros(nbits, dtype=np.uint8)
                    for attr in ("phash_90", "phash_180", "phash_270"):
                        rot_arr = np.array([
                            hex_to_bool_array(getattr(img, attr)) if getattr(img, attr) else zero_bits
                            for img in batch
                        ])
                        rot_dist = batch_hamming_distances(rot_arr, other_hashes)
                        dist_matrix = np.minimum(dist_matrix, rot_dist)
            else:
                dist_matrix = None

            for local_i, img in enumerate(batch):
                matches = []
                seen_paths = set()

                exact = exact_dups.get(img.sha256, [])
                for m in exact:
                    matches.append(("exact", 0, m))
                    seen_paths.add(m.path)

                if dist_matrix is not None:
                    distances = dist_matrix[local_i]
                    mask = distances <= self.threshold
                    for idx in np.where(mask)[0]:
                        other = other_images[idx]
                        if other.path not in seen_paths:
                            matches.append(("similar", int(distances[idx]), other))
                            seen_paths.add(other.path)

                if self.detect_near_dup and other_time_keys:
                    t = _parse_taken_at(img.taken_at)
                    if t is not None:
                        lo = bisect.bisect_left(other_time_keys, t - self.NEAR_DUP_TIME_WINDOW)
                        hi = bisect.bisect_right(other_time_keys, t + self.NEAR_DUP_TIME_WINDOW)
                        candidates = other_times[lo:hi]
                        if candidates:
                            h1 = compute_color_histogram(img.path)
                            for _, idx in candidates:
                                other = other_images[idx]
                                if other.path in seen_paths:
                                    continue
                                h2 = compute_color_histogram(other.path)
                                sim = histogram_similarity(h1, h2)
                                if sim >= self.NEAR_DUP_SIMILARITY_THRESHOLD:
                                    dist_equiv = int(round((1 - sim) * 256))
                                    matches.append(("near", dist_equiv, other))
                                    seen_paths.add(other.path)

                matches.sort(key=lambda x: x[1])
                results.append((img, matches))

        self.progress.emit(total, total)
        db.close()
        self.finished.emit(results)


class VideoQueryWorker(QObject):
    finished = Signal(list)
    progress = Signal(int, int)  # current, total

    NEAR_DUP_DURATION_TOLERANCE = 0.15  # allowed relative duration difference

    def __init__(self, db_path, target_dir, threshold, recursive=False):
        super().__init__()
        self.db_path = db_path
        self.target_dir = target_dir
        self.threshold = threshold
        self.recursive = recursive

    def run(self):
        db = Database(self.db_path)
        disabled_dirs = db.get_disabled_dirs()

        def is_dir_disabled(d):
            for dd in disabled_dirs:
                if d == dd or d.startswith(dd.rstrip(os.sep) + os.sep):
                    return True
            return False

        target_videos = db.get_videos_in_directory(self.target_dir, recursive=self.recursive)
        other_videos = db.get_videos_not_in_directory(self.target_dir, recursive=self.recursive)
        other_videos = [o for o in other_videos if not is_dir_disabled(o.directory)]

        exact_dups = db.get_exact_video_duplicates_outside(self.target_dir, recursive=self.recursive)
        exact_dups = {k: [m for m in v if not is_dir_disabled(m.directory)] for k, v in exact_dups.items()}
        exact_dups = {k: v for k, v in exact_dups.items() if v}

        total = len(target_videos)
        print(f"[video query] target_dir={self.target_dir!r} target_videos={total} other_videos={len(other_videos)}")

        results = []
        for i, vid in enumerate(target_videos):
            if i % 5 == 0:
                self.progress.emit(i, total)

            matches = []
            seen_paths = set()

            for m in exact_dups.get(vid.sha256, []):
                matches.append(("exact", 0.0, m))
                seen_paths.add(m.path)

            hashes1 = vid.frame_hash_list()
            if hashes1:
                for other in other_videos:
                    if other.path in seen_paths:
                        continue
                    if vid.duration > 0 and other.duration > 0:
                        rel_diff = abs(vid.duration - other.duration) / max(vid.duration, other.duration)
                        if rel_diff > self.NEAR_DUP_DURATION_TOLERANCE:
                            continue
                    hashes2 = other.frame_hash_list()
                    if not hashes2:
                        continue
                    dist = video_similarity_distance(hashes1, hashes2)
                    if dist <= self.threshold:
                        matches.append(("similar", dist, other))
                        seen_paths.add(other.path)

            matches.sort(key=lambda x: x[1])
            results.append((vid, matches))

        self.progress.emit(total, total)
        db.close()
        self.finished.emit(results)


class IndexTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.worker = None
        self.thread = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        grp = QGroupBox("Repertoires a indexer")
        gl = QVBoxLayout(grp)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("Ajouter un repertoire")
        self.btn_add.clicked.connect(self._add_dir)
        self.btn_remove = QPushButton("Retirer selectionne")
        self.btn_remove.clicked.connect(self._remove_dir)
        self.btn_clear_db = QPushButton("Vider la base")
        self.btn_clear_db.clicked.connect(self._clear_db)
        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_remove)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_clear_db)
        gl.addLayout(btn_row)

        self.dir_list = QTreeWidget()
        self.dir_list.setHeaderLabels(["Recursif", "Actif", "Repertoire indexe", "Date d'indexation"])
        self.dir_list.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.dir_list.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.dir_list.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.dir_list.header().setSectionResizeMode(3, QHeaderView.Interactive)
        self.dir_list.itemChanged.connect(self._on_dir_check_changed)
        gl.addWidget(self.dir_list)

        index_row = QHBoxLayout()
        self.btn_index = QPushButton("Lancer l'indexation")
        self.btn_index.setMinimumHeight(40)
        self.btn_index.clicked.connect(self._start_index)
        index_row.addWidget(self.btn_index, 1)
        index_row.addWidget(QLabel("Workers:"))
        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 128)
        self.spin_workers.setValue(16)
        self.spin_workers.setToolTip("Nombre de processus paralleles pour l'indexation")
        index_row.addWidget(self.spin_workers)
        self.chk_force = QCheckBox("Forcer le recalcul")
        self.chk_force.setToolTip(
            "Recalcule le hash meme pour les fichiers deja indexes (taille inchangee).\n"
            "A utiliser apres une mise a jour de l'algorithme de hachage."
        )
        index_row.addWidget(self.chk_force)
        gl.addLayout(index_row)

        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setMinimumHeight(36)
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_pause.setVisible(False)
        gl.addWidget(self.btn_pause)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        gl.addWidget(self.progress)

        self.lbl_progress = QLabel("")
        gl.addWidget(self.lbl_progress)

        layout.addWidget(grp)

        info_grp = QGroupBox("Informations")
        il = QVBoxLayout(info_grp)
        self.lbl_count = QLabel("0 images dans la base")
        il.addWidget(self.lbl_count)
        layout.addWidget(info_grp)

        self._refresh_dirs()

    def _refresh_dirs(self):
        self.dir_list.blockSignals(True)
        self.dir_list.clear()
        for d, recursive, enabled in self.db.get_indexed_dirs():
            item = QTreeWidgetItem(["", "", d, ""])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked if recursive else Qt.Unchecked)
            item.setCheckState(1, Qt.Checked if enabled else Qt.Unchecked)
            self.dir_list.addTopLevelItem(item)
        self.dir_list.blockSignals(False)
        self.lbl_count.setText(f"{self.db.get_image_count()} images dans la base")

    def _on_dir_check_changed(self, item, col):
        dir_path = item.text(2)
        if col == 1:
            enabled = item.checkState(1) == Qt.Checked
            self.db.set_dir_enabled(dir_path, enabled)
        elif col == 0:
            recursive = item.checkState(0) == Qt.Checked
            self.db.set_dir_recursive(dir_path, recursive)

    def _add_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Selectionner un repertoire")
        if d:
            d = os.path.normpath(d)
            existing = [e[0] for e in self.db.get_indexed_dirs()]
            if d not in existing:
                self.db.mark_dir_indexed(d, recursive=True)
            self._refresh_dirs()

    def _remove_dir(self):
        item = self.dir_list.currentItem()
        if item:
            d = item.text(2)
            self.db.remove_images_in_dir(d)
            self._refresh_dirs()

    def _clear_db(self):
        reply = QMessageBox.question(
            self, "Confirmer", "Vider toute la base de donnees ?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.db.clear_all()
            self._refresh_dirs()

    def _start_index(self):
        dirs = []
        for i in range(self.dir_list.topLevelItemCount()):
            item = self.dir_list.topLevelItem(i)
            d = item.text(2)
            recursive = item.checkState(0) == Qt.Checked
            dirs.append((d, recursive))
        if not dirs:
            return
        self.btn_index.setEnabled(False)
        self.btn_pause.setVisible(True)
        self.btn_pause.setText("Pause")
        self._paused = False
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.lbl_progress.setText("Indexation en cours...")

        self.thread = QThread()
        self.worker = IndexWorker(self.db.db_path, dirs, max_workers=self.spin_workers.value(), force=self.chk_force.isChecked())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.thread.start()

    def _toggle_pause(self):
        if self._paused:
            self.worker.resume()
            self._paused = False
            self.btn_pause.setText("Pause")
            self.lbl_progress.setText("Indexation en cours...")
        else:
            self.worker.pause()
            self._paused = True
            self.btn_pause.setText("Reprendre")
            self.lbl_progress.setText("Indexation en pause...")

    def _on_progress(self, p: IndexProgress):
        if p.total > 0:
            self.progress.setRange(0, p.total)
            self.progress.setValue(p.current)
        skip_info = f" ({p.skipped} skipes)" if p.skipped else ""
        self.lbl_progress.setText(f"[{p.current}/{p.total}]{skip_info} {p.current_file}")

    def _on_finished(self, progress):
        self._last_progress = progress
        self.thread.quit()
        self._paused = False
        self.btn_index.setEnabled(True)
        self.btn_pause.setVisible(False)
        self.progress.setVisible(False)
        self.lbl_progress.setText("Indexation terminee.")
        if progress and progress.skipped:
            self.lbl_progress.setText(f"Indexation terminee. {progress.skipped} fichier(s) deja indexes ignores.")
        if progress and progress.errors:
            for msg in progress.errors:
                if "supprime" in msg:
                    self.lbl_progress.setText(self.lbl_progress.text() + f" {msg}.")
        self._refresh_dirs()
        sb = self.window().statusBar()
        if sb:
            sb.showMessage("Indexation terminee", 5000)

    def refresh(self):
        self._refresh_dirs()


class QueryTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.worker = None
        self.thread = None
        self.results = []
        self._build_ui()

    def _build_ui(self):
        self.settings = QSettings("DoublonPhoto", "DoublonPhoto")
        layout = QVBoxLayout(self)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Repertoire cible:"))
        self.combo_dirs = QComboBox()
        self.combo_dirs.setMinimumWidth(400)
        ctrl.addWidget(self.combo_dirs, 1)
        self.chk_recursive = QCheckBox("Recursif")
        self.chk_recursive.setToolTip("Inclure les sous-repertoires dans la recherche")
        ctrl.addWidget(self.chk_recursive)
        self.chk_rotation = QCheckBox("Detecter rotations")
        self.chk_rotation.setToolTip(
            "Detecte aussi les doublons tournes de 90/180/270 degres.\n"
            "Necessite d'avoir reindexe avec 'Forcer le recalcul' apres cette mise a jour\n"
            "pour que les hash de rotation soient disponibles en base."
        )
        ctrl.addWidget(self.chk_rotation)
        self.chk_near_dup = QCheckBox("Detecter quasi-doublons")
        self.chk_near_dup.setToolTip(
            "Detecte les photos visuellement tres proches prises a quelques minutes\n"
            "d'intervalle (meme scene, angle/cadrage legerement different), en se basant\n"
            "sur la date EXIF de prise de vue et un histogramme de couleurs.\n"
            "Necessite que les photos aient une date EXIF valide."
        )
        ctrl.addWidget(self.chk_near_dup)
        self.btn_refresh_dirs = QPushButton("Rafraichir")
        self.btn_refresh_dirs.clicked.connect(self._refresh_dirs)
        ctrl.addWidget(self.btn_refresh_dirs)
        layout.addLayout(ctrl)

        thr_row = QHBoxLayout()
        thr_row.addWidget(QLabel("Seuil de similarite (hamming):"))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 64)
        self.slider.setValue(10)
        self.slider.setToolTip(
            "Distance de Hamming entre les hashes perceptuels.\n\n"
            "0 = images identiques (meme pixels)\n"
            "1-5 = recompression leger, crop mineur, metadata change\n"
            "6-15 = recompression modere, petit redimensionnement\n"
            "16-30 = variantes tres alterees, faux positifs probables\n"
            "30+ = images tres differentes\n\n"
            "Plus le seuil est eleve, plus on tolere de differences\n"
            "mais plus on risque de detecter des photos differentes comme doublons."
        )
        self.lbl_threshold = QLabel("10")
        self.slider.valueChanged.connect(lambda v: self.lbl_threshold.setText(str(v)))
        thr_row.addWidget(self.slider, 1)
        thr_row.addWidget(self.lbl_threshold)
        layout.addLayout(thr_row)

        self.lbl_hamming_help = QLabel(
            "<small><i>0 = identique · 1-5 = recompression leger · 6-15 = modere · 16+ = altere/risque de faux positifs</i></small>"
        )
        layout.addWidget(self.lbl_hamming_help)

        self.btn_query = QPushButton("Rechercher les doublons")
        self.btn_query.setMinimumHeight(36)
        self.btn_query.clicked.connect(self._run_query)
        layout.addWidget(self.btn_query)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        review_row = QHBoxLayout()
        review_row.addWidget(QLabel("Dossier de revue:"))
        self.lbl_review_folder = QLabel(self.settings.value("review_folder", "(non configure)") or "(non configure)")
        self.lbl_review_folder.setStyleSheet("font-style: italic; color: #555;")
        review_row.addWidget(self.lbl_review_folder, 1)
        self.btn_set_review = QPushButton("Configurer...")
        self.btn_set_review.clicked.connect(self._set_review_folder)
        review_row.addWidget(self.btn_set_review)
        self.btn_move = QPushButton("Deplacer la selection vers dossier de revue")
        self.btn_move.clicked.connect(self._move_selected)
        review_row.addWidget(self.btn_move)
        self.btn_move_custom = QPushButton("Deplacer la selection vers...")
        self.btn_move_custom.clicked.connect(self._move_selected_to_custom)
        review_row.addWidget(self.btn_move_custom)
        layout.addLayout(review_row)

        splitter = QSplitter(Qt.Horizontal)

        left_splitter = QSplitter(Qt.Vertical)
        left_header = QLabel("<b>Photos du repertoire cible avec doublons</b>")
        left_header.setMaximumHeight(20)
        left_splitter.addWidget(left_header)
        self.tree = QTreeWidget()
        self.tree.setSortingEnabled(True)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.setHeaderLabels([
            "Fichier", "Dimensions", "Taille", "Format",
            "Doublons", "Type", "Meilleure qualite", "Repertoire du doublon"
        ])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(2, QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(3, QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(4, QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(5, QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(6, QHeaderView.Interactive)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 250)
        self.tree.setColumnWidth(1, 100)
        self.tree.setColumnWidth(2, 80)
        self.tree.setColumnWidth(3, 60)
        self.tree.setColumnWidth(4, 80)
        self.tree.setColumnWidth(5, 80)
        self.tree.setColumnWidth(6, 120)
        self.tree.itemClicked.connect(self._on_left_item_clicked)
        self.tree.itemDoubleClicked.connect(self._on_tree_item_double_clicked)
        self.tree.itemSelectionChanged.connect(self._on_left_selection_changed)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        QShortcut(QKeySequence("Ctrl+X"), self.tree, activated=lambda: self._cut_to_clipboard(self.tree))
        left_splitter.addWidget(self.tree)
        self.lbl_left_preview = QLabel()
        self.lbl_left_preview.setAlignment(Qt.AlignCenter)
        self.lbl_left_preview.setMinimumHeight(50)
        self.lbl_left_preview.setStyleSheet("border: 1px solid #ccc; background: #222;")
        self.lbl_left_preview.mouseDoubleClickEvent = lambda e: self._open_preview_path(self.lbl_left_preview)
        left_splitter.addWidget(self.lbl_left_preview)
        left_splitter.setSizes([20, 370, 200])
        left_splitter.setStretchFactor(0, 0)
        left_splitter.setStretchFactor(1, 4)
        left_splitter.setStretchFactor(2, 1)
        left_splitter.splitterMoved.connect(lambda: self._rescale_preview(self.lbl_left_preview))
        splitter.addWidget(left_splitter)

        right_splitter = QSplitter(Qt.Vertical)
        right_top = QWidget()
        right_top_layout = QVBoxLayout(right_top)
        right_top_layout.setContentsMargins(0, 0, 0, 0)
        self.lbl_detail_title = QLabel("Cliquez sur une photo a gauche pour voir ses doublons")
        self.lbl_detail_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        right_top_layout.addWidget(self.lbl_detail_title)
        self.detail_tree = QTreeWidget()
        self.detail_tree.setSortingEnabled(True)
        self.detail_tree.setHeaderLabels([
            "Chemin", "Dimensions", "Taille", "Format", "Distance", "Qualite"
        ])
        self.detail_tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.detail_tree.header().setSectionResizeMode(1, QHeaderView.Interactive)
        self.detail_tree.header().setSectionResizeMode(2, QHeaderView.Interactive)
        self.detail_tree.header().setSectionResizeMode(3, QHeaderView.Interactive)
        self.detail_tree.header().setSectionResizeMode(4, QHeaderView.Interactive)
        self.detail_tree.header().setSectionResizeMode(5, QHeaderView.Interactive)
        self.detail_tree.header().setStretchLastSection(True)
        self.detail_tree.setColumnWidth(0, 300)
        self.detail_tree.setColumnWidth(1, 100)
        self.detail_tree.setColumnWidth(2, 80)
        self.detail_tree.setColumnWidth(3, 60)
        self.detail_tree.setColumnWidth(4, 70)
        self.detail_tree.setColumnWidth(5, 80)
        self.detail_tree.itemClicked.connect(self._on_right_item_clicked)
        self.detail_tree.itemDoubleClicked.connect(self._on_tree_item_double_clicked)
        self.detail_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.detail_tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        QShortcut(QKeySequence("Ctrl+X"), self.detail_tree, activated=lambda: self._cut_to_clipboard(self.detail_tree))
        right_top_layout.addWidget(self.detail_tree, 1)

        action_row = QHBoxLayout()
        self.btn_open = QPushButton("Ouvrir le dossier")
        self.btn_open.clicked.connect(self._open_folder)
        action_row.addWidget(self.btn_open)
        self.btn_compare = QPushButton("Comparer visuellement")
        self.btn_compare.clicked.connect(self._compare_selected)
        action_row.addWidget(self.btn_compare)
        action_row.addStretch()
        right_top_layout.addLayout(action_row)
        right_splitter.addWidget(right_top)

        self.lbl_right_preview = QLabel()
        self.lbl_right_preview.setAlignment(Qt.AlignCenter)
        self.lbl_right_preview.setMinimumHeight(50)
        self.lbl_right_preview.setStyleSheet("border: 1px solid #ccc; background: #222;")
        self.lbl_right_preview.mouseDoubleClickEvent = lambda e: self._open_preview_path(self.lbl_right_preview)
        right_splitter.addWidget(self.lbl_right_preview)
        right_splitter.setSizes([400, 200])
        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 1)
        right_splitter.splitterMoved.connect(lambda: self._rescale_preview(self.lbl_right_preview))
        splitter.addWidget(right_splitter)
        splitter.setSizes([550, 550])
        layout.addWidget(splitter, 1)

        self._refresh_dirs()

    def _refresh_dirs(self):
        previous = self.combo_dirs.currentData(Qt.UserRole)
        self.combo_dirs.clear()
        enabled_dirs = self.db.get_enabled_dirs()

        def is_enabled(d):
            for ed in enabled_dirs:
                if d == ed or d.startswith(ed.rstrip(os.sep) + os.sep):
                    return True
            return False

        counts = self.db.get_directory_counts()

        entries = {}
        for d, count in counts.items():
            if is_enabled(d):
                entries[d] = entries.get(d, 0) + count

        # Ensure indexed root directories appear even if they contain no
        # direct images (e.g. only subfolders when indexed recursively).
        for root in enabled_dirs:
            if root not in entries:
                prefix = root.rstrip(os.sep) + os.sep
                total = sum(c for d, c in counts.items() if d == root or d.startswith(prefix))
                entries[root] = total

        restore_index = -1
        for d, count in sorted(entries.items()):
            self.combo_dirs.addItem(f"{d} ({count})")
            self.combo_dirs.setItemData(self.combo_dirs.count() - 1, d, Qt.UserRole)
            if d == previous:
                restore_index = self.combo_dirs.count() - 1
        if restore_index >= 0:
            self.combo_dirs.setCurrentIndex(restore_index)

    def _run_query(self):
        target = self.combo_dirs.currentData(Qt.UserRole)
        self.tree.clear()
        self.detail_tree.clear()
        self.lbl_left_preview.clear()
        self.lbl_right_preview.clear()
        if not target:
            self.lbl_detail_title.setText("Aucun repertoire selectionne.")
            return
        print(f"[query] target_dir={target!r}")
        threshold = self.slider.value()
        self.btn_query.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.lbl_detail_title.setText("Recherche en cours...")

        recursive = self.chk_recursive.isChecked()
        detect_rotation = self.chk_rotation.isChecked()
        detect_near_dup = self.chk_near_dup.isChecked()
        self.thread = QThread()
        self.worker = QueryWorker(
            self.db.db_path, target, threshold, recursive=recursive,
            detect_rotation=detect_rotation, detect_near_dup=detect_near_dup
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_query_progress)
        self.worker.finished.connect(self._on_query_done)
        self.thread.start()

    def _on_query_progress(self, current, total):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
            self.lbl_detail_title.setText(f"Recherche en cours... {current}/{total}")

    def _on_query_done(self, results):
        self.thread.quit()
        self.results = results
        self.btn_query.setEnabled(True)
        self.progress.setVisible(False)

        has_dup_count = 0
        for img, matches in results:
            if not matches:
                continue
            has_dup_count += 1
            best_match = max(matches, key=lambda m: quality_score(m[2]))[2]
            has_exact = any(m[0] == "exact" for m in matches)
            if quality_score(img) >= quality_score(best_match):
                if best_match.file_size > 0:
                    pct = (img.file_size - best_match.file_size) / best_match.file_size * 100
                    pct_str = f" {'+' if pct >= 0 else ''}{pct:.1f}%"
                else:
                    pct_str = ""
                best_quality = ("Identique" if has_exact else "Oui") + pct_str
            else:
                best_quality = "Non (autre copie meilleure)"

            dup_types = set(m[0] for m in matches)
            type_str = ", ".join(sorted(dup_types))
            dup_dir = os.path.dirname(matches[0][2].path) if len(matches) == 1 else ""

            item = CaseInsensitiveTreeWidgetItem([
                img.filename,
                f"{img.width}x{img.height}",
                _fmt_size(img.file_size),
                img.format,
                str(len(matches)),
                type_str,
                best_quality,
                dup_dir
            ])
            item.setData(0, Qt.UserRole, (img, matches))
            if "exact" in dup_types:
                for c in range(8):
                    item.setBackground(c, Qt.green if False else Qt.white)
                item.setForeground(6, Qt.darkGreen)
            else:
                item.setForeground(6, Qt.darkYellow)
            self.tree.addTopLevelItem(item)

        self.lbl_detail_title.setText(
            f"{has_dup_count} image(s) avec doublons sur {len(results)} analysees"
        )
        sb = self.window().statusBar()
        if sb:
            sb.showMessage(f"Recherche terminee: {has_dup_count} doublons trouves", 5000)

    def _on_left_item_clicked(self, item, col):
        pass

    def _on_tree_item_double_clicked(self, item, col):
        path = item.data(0, Qt.UserRole)
        if isinstance(path, tuple):
            path = path[0].path
        if isinstance(path, str):
            open_path(path)

    def _on_tree_context_menu(self, pos):
        tree = self.sender()
        item = tree.itemAt(pos)
        if not item:
            return
        path = item.data(0, Qt.UserRole)
        if not path:
            return
        if isinstance(path, tuple):
            path = path[0].path
        if not isinstance(path, str):
            return

        menu = QMenu(self)
        act_cut = menu.addAction("Couper")
        act_open = menu.addAction("Ouvrir le repertoire de la photo")
        act_copy = menu.addAction("Copier le chemin du repertoire")
        chosen = menu.exec(tree.viewport().mapToGlobal(pos))

        if chosen == act_cut:
            self._cut_to_clipboard(tree)
        elif chosen == act_open:
            open_path(os.path.dirname(path))
        elif chosen == act_copy:
            folder = os.path.dirname(path)
            QApplication.clipboard().setText(folder)

    def _get_selected_paths(self, tree):
        paths = []
        for it in tree.selectedItems():
            data = it.data(0, Qt.UserRole)
            if isinstance(data, tuple):
                paths.append(data[0].path)
            elif isinstance(data, str):
                paths.append(data)
        return [p for p in paths if p]

    def _cut_to_clipboard(self, tree):
        paths = self._get_selected_paths(tree)
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return
        urls = [QUrl.fromLocalFile(p) for p in paths]
        mime = QMimeData()
        mime.setUrls(urls)
        set_cut_mime_effect(mime)
        QApplication.clipboard().setMimeData(mime)
        sb = self.window().statusBar()
        if sb:
            sb.showMessage(f"{len(paths)} fichier(s) coupe(s). Collez avec Ctrl+V dans l'Explorateur.", 5000)

    def _on_left_selection_changed(self):
        items = self.tree.selectedItems()
        if len(items) == 0:
            return
        if len(items) > 1:
            self.lbl_left_preview.clear()
            self.lbl_left_preview.setText(f"{len(items)} photos selectionnees")
            self.detail_tree.clear()
            self.lbl_detail_title.setText(f"{len(items)} photos selectionnees")
            self.lbl_right_preview.clear()
            return

        item = items[0]
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        img, matches = data
        self.detail_tree.clear()
        self.lbl_detail_title.setText(f"{img.filename} — {len(matches)} doublon(s)")

        for match_type, dist, m in matches:
            q_score = quality_score(m)
            child = CaseInsensitiveTreeWidgetItem([
                m.path,
                f"{m.width}x{m.height}",
                _fmt_size(m.file_size),
                m.format,
                str(dist) if match_type == "similar" else "0 (exact)",
                f"{q_score:.0f}"
            ])
            child.setData(0, Qt.UserRole, m.path)
            if match_type == "exact":
                child.setForeground(0, Qt.darkGreen)
            self.detail_tree.addTopLevelItem(child)

        self._load_preview_left(img.path)
        if self.detail_tree.topLevelItemCount() > 0:
            first = self.detail_tree.topLevelItem(0)
            self.detail_tree.setCurrentItem(first)
            path = first.data(0, Qt.UserRole)
            if path:
                self._load_preview_right(path)
        else:
            self.lbl_right_preview.clear()
            self.lbl_right_preview.setText("Aucun doublon")

    def _on_right_item_clicked(self, item, col):
        path = item.data(0, Qt.UserRole)
        if path:
            self._load_preview_right(path)

    def _load_preview_left(self, path):
        self._load_preview(path, self.lbl_left_preview)

    def _load_preview_right(self, path):
        self._load_preview(path, self.lbl_right_preview)

    def _load_preview(self, path, label):
        label.setProperty("preview_path", path)
        try:
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                label.setProperty("full_pixmap", pixmap)
                scaled = pixmap.scaled(
                    label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                label.setPixmap(scaled)
            else:
                label.setProperty("full_pixmap", None)
                label.setText("Apercu non disponible")
        except Exception:
            label.setProperty("full_pixmap", None)
            label.setText("Apercu non disponible")

    def _open_preview_path(self, label):
        open_path(label.property("preview_path"))

    def _rescale_preview(self, label):
        pixmap = label.property("full_pixmap")
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(
                label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            label.setPixmap(scaled)

    def _set_review_folder(self):
        current = self.settings.value("review_folder", "")
        d = QFileDialog.getExistingDirectory(self, "Configurer le dossier de revue", current)
        if d:
            self.settings.setValue("review_folder", d)
            self.lbl_review_folder.setText(d)

    def _move_selected(self):
        items = self.tree.selectedItems()
        if not items:
            QMessageBox.information(self, "Info", "Selectionnez des photos dans le panneau de gauche.")
            return
        dest = self.settings.value("review_folder", "")
        if not dest or not os.path.isdir(dest):
            QMessageBox.warning(self, "Dossier de revue", "Aucun dossier de revue configure. Cliquez sur 'Configurer...'")
            return
        names = ", ".join(items[i].data(0, Qt.UserRole)[0].filename for i in range(min(len(items), 5)))
        if len(items) > 5:
            names += f" ... ({len(items)} total)"
        reply = QMessageBox.question(
            self, "Confirmer le deplacement",
            f"Deplacer {len(items)} fichier(s) vers:\n{dest}\n\n{names}",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        moved = 0
        moved_items = []
        progress = QProgressDialog("Deplacement en cours...", "Annuler", 0, len(items), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        for i, item in enumerate(items):
            if progress.wasCanceled():
                break
            data = item.data(0, Qt.UserRole)
            if not data:
                continue
            path = data[0].path
            progress.setLabelText(f"Deplacement de {os.path.basename(path) if path else ''}...")
            progress.setValue(i)
            QApplication.processEvents()
            if path and os.path.exists(path):
                try:
                    dest_path = os.path.join(dest, os.path.basename(path))
                    if os.path.exists(dest_path):
                        dest_path = os.path.join(dest, f"dup_{moved}_{os.path.basename(path)}")
                    shutil.move(path, dest_path)
                    self.db.remove_image(path)
                    moved += 1
                    moved_items.append(item)
                except Exception as e:
                    QMessageBox.warning(self, "Erreur", f"Impossible de deplacer {path}:\n{e}")
                    break
        progress.setValue(len(items))
        progress.close()
        if moved:
            for it in moved_items:
                idx = self.tree.indexOfTopLevelItem(it)
                if idx >= 0:
                    self.tree.takeTopLevelItem(idx)
            self.detail_tree.clear()
            self.lbl_left_preview.clear()
            self.lbl_right_preview.clear()
            sb = self.window().statusBar()
            if sb:
                sb.showMessage(f"{moved} fichier(s) deplace(s) vers {dest}", 5000)
            self._refresh_dirs()

    def _move_selected_to_custom(self):
        items = self.tree.selectedItems()
        from_left = True
        if not items:
            items = self.detail_tree.selectedItems()
            from_left = False
        if not items:
            QMessageBox.information(self, "Info", "Selectionnez des photos dans un des deux panneaux.")
            return

        path_items = []
        if from_left:
            for it in items:
                data = it.data(0, Qt.UserRole)
                if data:
                    path_items.append((data[0].path, it))
        else:
            for it in items:
                p = it.data(0, Qt.UserRole)
                if p:
                    path_items.append((p, it))
        path_items = [(p, it) for p, it in path_items if p]
        if not path_items:
            return
        paths = [p for p, it in path_items]
        item_by_path = {p: it for p, it in path_items}

        last_dir = self.settings.value("last_move_dir", "")
        dest = QFileDialog.getExistingDirectory(self, "Choisir le repertoire de destination", last_dir)
        if not dest:
            return
        dest = os.path.normpath(dest)
        self.settings.setValue("last_move_dir", dest)

        names = ", ".join(os.path.basename(p) for p in paths[:5])
        if len(paths) > 5:
            names += f" ... ({len(paths)} total)"
        reply = QMessageBox.question(
            self, "Confirmer le deplacement",
            f"Deplacer {len(paths)} fichier(s) vers:\n{dest}\n\n{names}",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        apply_all_choice = None
        moved = 0
        skipped = 0
        moved_paths = []
        progress = QProgressDialog("Deplacement en cours...", "Annuler", 0, len(paths), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        for idx, path in enumerate(paths):
            if progress.wasCanceled():
                break
            progress.setLabelText(f"Deplacement de {os.path.basename(path)}...")
            progress.setValue(idx)
            QApplication.processEvents()
            if not os.path.exists(path):
                continue
            dest_path = os.path.join(dest, os.path.basename(path))
            if os.path.abspath(os.path.dirname(dest_path)) == os.path.abspath(os.path.dirname(path)):
                continue
            if os.path.exists(dest_path):
                choice = apply_all_choice
                if choice is None:
                    choice, apply_all = self._ask_conflict_choice(os.path.basename(path))
                    if apply_all:
                        apply_all_choice = choice
                if choice == "skip":
                    skipped += 1
                    continue
                elif choice == "rename":
                    base, ext = os.path.splitext(os.path.basename(path))
                    i = 1
                    new_dest = dest_path
                    while os.path.exists(new_dest):
                        new_dest = os.path.join(dest, f"{base}_{i}{ext}")
                        i += 1
                    dest_path = new_dest
                # "replace" falls through and overwrites
            try:
                shutil.move(path, dest_path)
                self.db.remove_image(path)
                moved += 1
                moved_paths.append(path)
            except Exception as e:
                QMessageBox.warning(self, "Erreur", f"Impossible de deplacer {path}:\n{e}")
                break
        progress.setValue(len(paths))
        progress.close()

        if moved or skipped:
            if from_left:
                for p in moved_paths:
                    it = item_by_path.get(p)
                    if it is None:
                        continue
                    idx = self.tree.indexOfTopLevelItem(it)
                    if idx >= 0:
                        self.tree.takeTopLevelItem(idx)
                self.detail_tree.clear()
                self.lbl_left_preview.clear()
                self.lbl_right_preview.clear()
            else:
                for p in moved_paths:
                    it = item_by_path.get(p)
                    if it is None:
                        continue
                    idx = self.detail_tree.indexOfTopLevelItem(it)
                    if idx >= 0:
                        self.detail_tree.takeTopLevelItem(idx)
                    else:
                        parent = it.parent()
                        if parent is not None:
                            parent.removeChild(it)
                sel = self.tree.selectedItems()
                if sel:
                    data = sel[0].data(0, Qt.UserRole)
                    if data:
                        img, matches = data
                        new_matches = [m for m in matches if m[2].path not in moved_paths]
                        sel[0].setData(0, Qt.UserRole, (img, new_matches))
                        sel[0].setText(4, str(len(new_matches)))
            msg = f"{moved} fichier(s) deplace(s) vers {dest}"
            if skipped:
                msg += f"\n{skipped} fichier(s) ignore(s) (conflit)"
            sb = self.window().statusBar()
            if sb:
                sb.showMessage(msg.replace("\n", " "), 5000)
            self._refresh_dirs()

    def _ask_conflict_choice(self, filename):
        box = QMessageBox(self)
        box.setWindowTitle("Conflit de fichier")
        box.setText(f"Le fichier '{filename}' existe deja dans le repertoire de destination.\nQue voulez-vous faire ?")
        btn_replace = box.addButton("Remplacer", QMessageBox.AcceptRole)
        btn_rename = box.addButton("Renommer", QMessageBox.ActionRole)
        btn_skip = box.addButton("Ignorer", QMessageBox.RejectRole)
        chk = QCheckBox("Appliquer a tous les conflits suivants")
        box.setCheckBox(chk)
        box.exec()
        clicked = box.clickedButton()
        if clicked == btn_replace:
            choice = "replace"
        elif clicked == btn_rename:
            choice = "rename"
        else:
            choice = "skip"
        return choice, chk.isChecked()

    def _open_folder(self):
        items = self.detail_tree.selectedItems()
        if not items:
            return
        path = items[0].data(0, Qt.UserRole)
        if path:
            open_path(os.path.dirname(path))

    def _compare_selected(self):
        left_items = self.tree.selectedItems()
        right_items = self.detail_tree.selectedItems()
        if not left_items or not right_items:
            QMessageBox.information(
                self, "Comparaison",
                "Selectionnez une image dans la liste de gauche et une image dans la liste de droite."
            )
            return
        left_data = left_items[0].data(0, Qt.UserRole)
        left_path = left_data[0].path if left_data else None
        right_path = right_items[0].data(0, Qt.UserRole)
        if not left_path or not right_path:
            QMessageBox.information(self, "Comparaison", "Impossible de recuperer les chemins des images.")
            return
        dlg = ImageCompareDialog(left_path, right_path, self)
        dlg.exec()

    def refresh(self):
        self._refresh_dirs()


class VideoTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.worker = None
        self.thread = None
        self.results = []
        self._build_ui()

    def _build_ui(self):
        self.settings = QSettings("DoublonPhoto", "DoublonPhoto")
        layout = QVBoxLayout(self)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Repertoire cible:"))
        self.combo_dirs = QComboBox()
        self.combo_dirs.setMinimumWidth(400)
        ctrl.addWidget(self.combo_dirs, 1)
        self.chk_recursive = QCheckBox("Recursif")
        self.chk_recursive.setToolTip("Inclure les sous-repertoires dans la recherche")
        ctrl.addWidget(self.chk_recursive)
        self.btn_refresh_dirs = QPushButton("Rafraichir")
        self.btn_refresh_dirs.clicked.connect(self._refresh_dirs)
        ctrl.addWidget(self.btn_refresh_dirs)
        layout.addLayout(ctrl)

        thr_row = QHBoxLayout()
        thr_row.addWidget(QLabel("Seuil de similarite (distance moyenne par frame):"))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 64)
        self.slider.setValue(10)
        self.slider.setToolTip(
            "Distance de Hamming moyenne entre les frames echantillonnees des deux videos.\n\n"
            "0 = frames identiques\n"
            "1-10 = reencodage/compression differente, tres probable doublon\n"
            "11-25 = variantes plus alterees, faux positifs possibles\n"
            "25+ = videos tres differentes\n\n"
            "La duree des deux videos doit aussi etre proche (tolerance 15%)."
        )
        self.lbl_threshold = QLabel("10")
        self.slider.valueChanged.connect(lambda v: self.lbl_threshold.setText(str(v)))
        thr_row.addWidget(self.slider, 1)
        thr_row.addWidget(self.lbl_threshold)
        layout.addLayout(thr_row)

        self.btn_query = QPushButton("Rechercher les doublons video")
        self.btn_query.setMinimumHeight(36)
        self.btn_query.clicked.connect(self._run_query)
        layout.addWidget(self.btn_query)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        review_row = QHBoxLayout()
        review_row.addWidget(QLabel("Dossier de revue:"))
        self.lbl_review_folder = QLabel(self.settings.value("review_folder", "(non configure)") or "(non configure)")
        self.lbl_review_folder.setStyleSheet("font-style: italic; color: #555;")
        review_row.addWidget(self.lbl_review_folder, 1)
        self.btn_set_review = QPushButton("Configurer...")
        self.btn_set_review.clicked.connect(self._set_review_folder)
        review_row.addWidget(self.btn_set_review)
        self.btn_move = QPushButton("Deplacer la selection vers dossier de revue")
        self.btn_move.clicked.connect(self._move_selected)
        review_row.addWidget(self.btn_move)
        self.btn_move_custom = QPushButton("Deplacer la selection vers...")
        self.btn_move_custom.clicked.connect(self._move_selected_to_custom)
        review_row.addWidget(self.btn_move_custom)
        layout.addLayout(review_row)

        splitter = QSplitter(Qt.Horizontal)

        left_splitter = QSplitter(Qt.Vertical)
        left_header = QLabel("<b>Videos du repertoire cible avec doublons</b>")
        left_header.setMaximumHeight(20)
        left_splitter.addWidget(left_header)
        self.tree = QTreeWidget()
        self.tree.setSortingEnabled(True)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.setHeaderLabels([
            "Fichier", "Duree", "Dimensions", "Taille",
            "Doublons", "Type", "Repertoire du doublon"
        ])
        for c in range(6):
            self.tree.header().setSectionResizeMode(c, QHeaderView.Interactive)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 250)
        self.tree.setColumnWidth(1, 80)
        self.tree.setColumnWidth(2, 100)
        self.tree.setColumnWidth(3, 80)
        self.tree.setColumnWidth(4, 80)
        self.tree.setColumnWidth(5, 80)
        self.tree.itemDoubleClicked.connect(self._on_tree_item_double_clicked)
        self.tree.itemSelectionChanged.connect(self._on_left_selection_changed)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        QShortcut(QKeySequence("Ctrl+X"), self.tree, activated=lambda: self._cut_to_clipboard(self.tree))
        left_splitter.addWidget(self.tree)
        self.lbl_left_preview = QLabel()
        self.lbl_left_preview.setAlignment(Qt.AlignCenter)
        self.lbl_left_preview.setMinimumHeight(50)
        self.lbl_left_preview.setStyleSheet("border: 1px solid #ccc; background: #222;")
        self.lbl_left_preview.mouseDoubleClickEvent = lambda e: self._open_preview_path(self.lbl_left_preview)
        left_splitter.addWidget(self.lbl_left_preview)
        left_splitter.setSizes([20, 370, 200])
        left_splitter.setStretchFactor(0, 0)
        left_splitter.setStretchFactor(1, 4)
        left_splitter.setStretchFactor(2, 2)
        left_splitter.splitterMoved.connect(lambda: self._rescale_preview(self.lbl_left_preview))
        splitter.addWidget(left_splitter)

        right_splitter = QSplitter(Qt.Vertical)
        right_top = QWidget()
        right_top_layout = QVBoxLayout(right_top)
        right_top_layout.setContentsMargins(0, 0, 0, 0)
        self.lbl_detail_title = QLabel("Selectionnez une video a gauche")
        right_top_layout.addWidget(self.lbl_detail_title)
        self.detail_tree = QTreeWidget()
        self.detail_tree.setSortingEnabled(True)
        self.detail_tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.detail_tree.setHeaderLabels([
            "Chemin du doublon", "Duree", "Dimensions", "Taille", "Format", "Distance"
        ])
        for c in range(5):
            self.detail_tree.header().setSectionResizeMode(c, QHeaderView.Interactive)
        self.detail_tree.setColumnWidth(0, 300)
        self.detail_tree.setColumnWidth(1, 80)
        self.detail_tree.setColumnWidth(2, 100)
        self.detail_tree.setColumnWidth(3, 80)
        self.detail_tree.setColumnWidth(4, 70)
        self.detail_tree.itemClicked.connect(self._on_right_item_clicked)
        self.detail_tree.itemDoubleClicked.connect(self._on_tree_item_double_clicked)
        self.detail_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.detail_tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        QShortcut(QKeySequence("Ctrl+X"), self.detail_tree, activated=lambda: self._cut_to_clipboard(self.detail_tree))
        right_top_layout.addWidget(self.detail_tree, 1)

        action_row = QHBoxLayout()
        self.btn_open = QPushButton("Ouvrir le dossier")
        self.btn_open.clicked.connect(self._open_folder)
        action_row.addWidget(self.btn_open)
        action_row.addStretch()
        right_top_layout.addLayout(action_row)
        right_splitter.addWidget(right_top)

        self.lbl_right_preview = QLabel()
        self.lbl_right_preview.setAlignment(Qt.AlignCenter)
        self.lbl_right_preview.setMinimumHeight(50)
        self.lbl_right_preview.setStyleSheet("border: 1px solid #ccc; background: #222;")
        self.lbl_right_preview.mouseDoubleClickEvent = lambda e: self._open_preview_path(self.lbl_right_preview)
        right_splitter.addWidget(self.lbl_right_preview)
        right_splitter.setSizes([400, 200])
        right_splitter.setStretchFactor(0, 3)
        right_splitter.setStretchFactor(1, 1)
        right_splitter.splitterMoved.connect(lambda: self._rescale_preview(self.lbl_right_preview))
        splitter.addWidget(right_splitter)

        splitter.setSizes([500, 500])
        layout.addWidget(splitter, 1)

        self._refresh_dirs()

    def _refresh_dirs(self):
        previous = self.combo_dirs.currentData(Qt.UserRole)
        self.combo_dirs.clear()
        enabled_dirs = self.db.get_enabled_dirs()

        def is_enabled(d):
            for ed in enabled_dirs:
                if d == ed or d.startswith(ed.rstrip(os.sep) + os.sep):
                    return True
            return False

        counts = self.db.get_video_directory_counts()
        entries = {}
        for d, count in counts.items():
            if is_enabled(d):
                entries[d] = entries.get(d, 0) + count

        for root in enabled_dirs:
            if root not in entries:
                prefix = root.rstrip(os.sep) + os.sep
                total = sum(c for d, c in counts.items() if d == root or d.startswith(prefix))
                entries[root] = total

        restore_index = -1
        for d, count in sorted(entries.items()):
            self.combo_dirs.addItem(f"{d} ({count})")
            self.combo_dirs.setItemData(self.combo_dirs.count() - 1, d, Qt.UserRole)
            if d == previous:
                restore_index = self.combo_dirs.count() - 1
        if restore_index >= 0:
            self.combo_dirs.setCurrentIndex(restore_index)

    def _run_query(self):
        target = self.combo_dirs.currentData(Qt.UserRole)
        self.tree.clear()
        self.detail_tree.clear()
        self.lbl_left_preview.clear()
        self.lbl_right_preview.clear()
        if not target:
            self.lbl_detail_title.setText("Aucun repertoire selectionne.")
            return
        threshold = self.slider.value()
        self.btn_query.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.lbl_detail_title.setText("Recherche en cours...")

        recursive = self.chk_recursive.isChecked()
        self.thread = QThread()
        self.worker = VideoQueryWorker(self.db.db_path, target, threshold, recursive=recursive)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_query_progress)
        self.worker.finished.connect(self._on_query_done)
        self.thread.start()

    def _on_query_progress(self, current, total):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
            self.lbl_detail_title.setText(f"Recherche en cours... {current}/{total}")

    def _on_query_done(self, results):
        self.thread.quit()
        self.results = results
        self.btn_query.setEnabled(True)
        self.progress.setVisible(False)

        has_dup_count = 0
        for vid, matches in results:
            if not matches:
                continue
            has_dup_count += 1
            dup_types = set(m[0] for m in matches)
            type_str = ", ".join(sorted(dup_types))
            dup_dir = os.path.dirname(matches[0][2].path) if len(matches) == 1 else ""

            item = CaseInsensitiveTreeWidgetItem([
                vid.filename,
                _fmt_duration(vid.duration),
                f"{vid.width}x{vid.height}",
                _fmt_size(vid.file_size),
                str(len(matches)),
                type_str,
                dup_dir
            ])
            item.setData(0, Qt.UserRole, (vid, matches))
            if "exact" in dup_types:
                item.setForeground(5, Qt.darkGreen)
            else:
                item.setForeground(5, Qt.darkYellow)
            self.tree.addTopLevelItem(item)

        self.lbl_detail_title.setText(
            f"{has_dup_count} video(s) avec doublons sur {len(results)} analysees"
        )
        sb = self.window().statusBar()
        if sb:
            sb.showMessage(f"Recherche video terminee: {has_dup_count} doublons trouves", 5000)

    def _on_tree_item_double_clicked(self, item, col):
        path = item.data(0, Qt.UserRole)
        if isinstance(path, tuple):
            path = path[0].path
        if isinstance(path, str):
            open_path(path)

    def _on_tree_context_menu(self, pos):
        tree = self.sender()
        item = tree.itemAt(pos)
        if not item:
            return
        path = item.data(0, Qt.UserRole)
        if not path:
            return
        if isinstance(path, tuple):
            path = path[0].path
        if not isinstance(path, str):
            return

        menu = QMenu(self)
        act_cut = menu.addAction("Couper")
        act_open = menu.addAction("Ouvrir le repertoire de la video")
        act_copy = menu.addAction("Copier le chemin du repertoire")
        chosen = menu.exec(tree.viewport().mapToGlobal(pos))

        if chosen == act_cut:
            self._cut_to_clipboard(tree)
        elif chosen == act_open:
            open_path(os.path.dirname(path))
        elif chosen == act_copy:
            folder = os.path.dirname(path)
            QApplication.clipboard().setText(folder)

    def _get_selected_paths(self, tree):
        paths = []
        for it in tree.selectedItems():
            data = it.data(0, Qt.UserRole)
            if isinstance(data, tuple):
                paths.append(data[0].path)
            elif isinstance(data, str):
                paths.append(data)
        return [p for p in paths if p]

    def _cut_to_clipboard(self, tree):
        paths = self._get_selected_paths(tree)
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return
        urls = [QUrl.fromLocalFile(p) for p in paths]
        mime = QMimeData()
        mime.setUrls(urls)
        set_cut_mime_effect(mime)
        QApplication.clipboard().setMimeData(mime)
        sb = self.window().statusBar()
        if sb:
            sb.showMessage(f"{len(paths)} fichier(s) coupe(s). Collez avec Ctrl+V dans l'Explorateur.", 5000)

    def _on_left_selection_changed(self):
        items = self.tree.selectedItems()
        if len(items) == 0:
            return
        if len(items) > 1:
            self.lbl_left_preview.clear()
            self.lbl_left_preview.setText(f"{len(items)} videos selectionnees")
            self.detail_tree.clear()
            self.lbl_detail_title.setText(f"{len(items)} videos selectionnees")
            self.lbl_right_preview.clear()
            return

        item = items[0]
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        vid, matches = data
        self.detail_tree.clear()
        self.lbl_detail_title.setText(f"{vid.filename} — {len(matches)} doublon(s)")

        for match_type, dist, m in matches:
            child = CaseInsensitiveTreeWidgetItem([
                m.path,
                _fmt_duration(m.duration),
                f"{m.width}x{m.height}",
                _fmt_size(m.file_size),
                m.format,
                "0 (exact)" if match_type == "exact" else f"{dist:.1f}"
            ])
            child.setData(0, Qt.UserRole, m.path)
            if match_type == "exact":
                child.setForeground(0, Qt.darkGreen)
            self.detail_tree.addTopLevelItem(child)

        self._load_preview_left(vid.path)
        if self.detail_tree.topLevelItemCount() > 0:
            first = self.detail_tree.topLevelItem(0)
            self.detail_tree.setCurrentItem(first)
            path = first.data(0, Qt.UserRole)
            if path:
                self._load_preview_right(path)
        else:
            self.lbl_right_preview.clear()
            self.lbl_right_preview.setText("Aucun doublon")

    def _on_right_item_clicked(self, item, col):
        path = item.data(0, Qt.UserRole)
        if path:
            self._load_preview_right(path)

    def _load_preview_left(self, path):
        self._load_video_thumbnail(path, self.lbl_left_preview)

    def _load_preview_right(self, path):
        self._load_video_thumbnail(path, self.lbl_right_preview)

    def _load_video_thumbnail(self, path, label):
        label.setProperty("preview_path", path)
        try:
            import cv2
            cap = cv2.VideoCapture(path)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(frame_count // 2, 0))
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                label.setProperty("full_pixmap", None)
                label.setText("Apercu non disponible")
                return
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(qimg.copy())
            label.setProperty("full_pixmap", pixmap)
            scaled = pixmap.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            label.setPixmap(scaled)
        except Exception:
            label.setProperty("full_pixmap", None)
            label.setText("Apercu non disponible")

    def _open_preview_path(self, label):
        open_path(label.property("preview_path"))

    def _rescale_preview(self, label):
        pixmap = label.property("full_pixmap")
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(
                label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            label.setPixmap(scaled)

    def _set_review_folder(self):
        current = self.settings.value("review_folder", "")
        d = QFileDialog.getExistingDirectory(self, "Configurer le dossier de revue", current)
        if d:
            self.settings.setValue("review_folder", d)
            self.lbl_review_folder.setText(d)

    def _move_selected(self):
        items = self.tree.selectedItems()
        if not items:
            QMessageBox.information(self, "Info", "Selectionnez des videos dans le panneau de gauche.")
            return
        dest = self.settings.value("review_folder", "")
        if not dest or not os.path.isdir(dest):
            QMessageBox.warning(self, "Dossier de revue", "Aucun dossier de revue configure. Cliquez sur 'Configurer...'")
            return
        names = ", ".join(items[i].data(0, Qt.UserRole)[0].filename for i in range(min(len(items), 5)))
        if len(items) > 5:
            names += f" ... ({len(items)} total)"
        reply = QMessageBox.question(
            self, "Confirmer le deplacement",
            f"Deplacer {len(items)} fichier(s) vers:\n{dest}\n\n{names}",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        moved = 0
        moved_items = []
        progress = QProgressDialog("Deplacement en cours...", "Annuler", 0, len(items), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        for i, item in enumerate(items):
            if progress.wasCanceled():
                break
            data = item.data(0, Qt.UserRole)
            if not data:
                continue
            path = data[0].path
            progress.setLabelText(f"Deplacement de {os.path.basename(path) if path else ''}...")
            progress.setValue(i)
            QApplication.processEvents()
            if path and os.path.exists(path):
                try:
                    dest_path = os.path.join(dest, os.path.basename(path))
                    if os.path.exists(dest_path):
                        dest_path = os.path.join(dest, f"dup_{moved}_{os.path.basename(path)}")
                    shutil.move(path, dest_path)
                    self.db.remove_video(path)
                    moved += 1
                    moved_items.append(item)
                except Exception as e:
                    QMessageBox.warning(self, "Erreur", f"Impossible de deplacer {path}:\n{e}")
                    break
        progress.setValue(len(items))
        progress.close()
        if moved:
            for it in moved_items:
                idx = self.tree.indexOfTopLevelItem(it)
                if idx >= 0:
                    self.tree.takeTopLevelItem(idx)
            self.detail_tree.clear()
            self.lbl_left_preview.clear()
            self.lbl_right_preview.clear()
            sb = self.window().statusBar()
            if sb:
                sb.showMessage(f"{moved} fichier(s) deplace(s) vers {dest}", 5000)
            self._refresh_dirs()

    def _move_selected_to_custom(self):
        items = self.tree.selectedItems()
        from_left = True
        if not items:
            items = self.detail_tree.selectedItems()
            from_left = False
        if not items:
            QMessageBox.information(self, "Info", "Selectionnez des videos dans un des deux panneaux.")
            return

        path_items = []
        if from_left:
            for it in items:
                data = it.data(0, Qt.UserRole)
                if data:
                    path_items.append((data[0].path, it))
        else:
            for it in items:
                p = it.data(0, Qt.UserRole)
                if p:
                    path_items.append((p, it))
        path_items = [(p, it) for p, it in path_items if p]
        if not path_items:
            return
        paths = [p for p, it in path_items]
        item_by_path = {p: it for p, it in path_items}

        last_dir = self.settings.value("last_move_dir", "")
        dest = QFileDialog.getExistingDirectory(self, "Choisir le repertoire de destination", last_dir)
        if not dest:
            return
        dest = os.path.normpath(dest)
        self.settings.setValue("last_move_dir", dest)

        names = ", ".join(os.path.basename(p) for p in paths[:5])
        if len(paths) > 5:
            names += f" ... ({len(paths)} total)"
        reply = QMessageBox.question(
            self, "Confirmer le deplacement",
            f"Deplacer {len(paths)} fichier(s) vers:\n{dest}\n\n{names}",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        apply_all_choice = None
        moved = 0
        skipped = 0
        moved_paths = []
        progress = QProgressDialog("Deplacement en cours...", "Annuler", 0, len(paths), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        for idx, path in enumerate(paths):
            if progress.wasCanceled():
                break
            progress.setLabelText(f"Deplacement de {os.path.basename(path)}...")
            progress.setValue(idx)
            QApplication.processEvents()
            if not os.path.exists(path):
                continue
            dest_path = os.path.join(dest, os.path.basename(path))
            if os.path.abspath(os.path.dirname(dest_path)) == os.path.abspath(os.path.dirname(path)):
                continue
            if os.path.exists(dest_path):
                choice = apply_all_choice
                if choice is None:
                    choice, apply_all = self._ask_conflict_choice(os.path.basename(path))
                    if apply_all:
                        apply_all_choice = choice
                if choice == "skip":
                    skipped += 1
                    continue
                elif choice == "rename":
                    base, ext = os.path.splitext(os.path.basename(path))
                    i = 1
                    new_dest = dest_path
                    while os.path.exists(new_dest):
                        new_dest = os.path.join(dest, f"{base}_{i}{ext}")
                        i += 1
                    dest_path = new_dest
            try:
                shutil.move(path, dest_path)
                self.db.remove_video(path)
                moved += 1
                moved_paths.append(path)
            except Exception as e:
                QMessageBox.warning(self, "Erreur", f"Impossible de deplacer {path}:\n{e}")
                break
        progress.setValue(len(paths))
        progress.close()

        if moved or skipped:
            if from_left:
                for p in moved_paths:
                    it = item_by_path.get(p)
                    if it is None:
                        continue
                    idx = self.tree.indexOfTopLevelItem(it)
                    if idx >= 0:
                        self.tree.takeTopLevelItem(idx)
                self.detail_tree.clear()
                self.lbl_left_preview.clear()
                self.lbl_right_preview.clear()
            else:
                for p in moved_paths:
                    it = item_by_path.get(p)
                    if it is None:
                        continue
                    idx = self.detail_tree.indexOfTopLevelItem(it)
                    if idx >= 0:
                        self.detail_tree.takeTopLevelItem(idx)
                    else:
                        parent = it.parent()
                        if parent is not None:
                            parent.removeChild(it)
                sel = self.tree.selectedItems()
                if sel:
                    data = sel[0].data(0, Qt.UserRole)
                    if data:
                        vid, matches = data
                        new_matches = [m for m in matches if m[2].path not in moved_paths]
                        sel[0].setData(0, Qt.UserRole, (vid, new_matches))
                        sel[0].setText(4, str(len(new_matches)))
            msg = f"{moved} fichier(s) deplace(s) vers {dest}"
            if skipped:
                msg += f"\n{skipped} fichier(s) ignore(s) (conflit)"
            sb = self.window().statusBar()
            if sb:
                sb.showMessage(msg.replace("\n", " "), 5000)
            self._refresh_dirs()

    def _ask_conflict_choice(self, filename):
        box = QMessageBox(self)
        box.setWindowTitle("Conflit de fichier")
        box.setText(f"Le fichier '{filename}' existe deja dans le repertoire de destination.\nQue voulez-vous faire ?")
        btn_replace = box.addButton("Remplacer", QMessageBox.AcceptRole)
        btn_rename = box.addButton("Renommer", QMessageBox.ActionRole)
        btn_skip = box.addButton("Ignorer", QMessageBox.RejectRole)
        chk = QCheckBox("Appliquer a tous les conflits suivants")
        box.setCheckBox(chk)
        box.exec()
        clicked = box.clickedButton()
        if clicked == btn_replace:
            choice = "replace"
        elif clicked == btn_rename:
            choice = "rename"
        else:
            choice = "skip"
        return choice, chk.isChecked()

    def _open_folder(self):
        items = self.detail_tree.selectedItems()
        if not items:
            return
        path = items[0].data(0, Qt.UserRole)
        if path:
            open_path(os.path.dirname(path))

    def refresh(self):
        self._refresh_dirs()


def _fmt_size(n):
    if n < 1024:
        return f"{n} o"
    elif n < 1024 * 1024:
        return f"{n/1024:.1f} Ko"
    else:
        return f"{n/(1024*1024):.1f} Mo"


def _fmt_duration(seconds):
    seconds = int(seconds or 0)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DoublonPhoto — Detecteur de doublons par repertoire")
        self.resize(1100, 750)
        self.db = Database("doublons.db")

        tabs = QTabWidget()
        self.index_tab = IndexTab(self.db)
        self.query_tab = QueryTab(self.db)
        self.video_tab = VideoTab(self.db)
        tabs.addTab(self.index_tab, "1. Indexation")
        tabs.addTab(self.query_tab, "2. Recherche par repertoire")
        tabs.addTab(self.video_tab, "3. Recherche video")
        tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(tabs)
        self._tabs = tabs

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Pret")

    def _on_tab_changed(self, idx):
        if idx == 1:
            self.query_tab.refresh()
        elif idx == 2:
            self.video_tab.refresh()
        elif idx == 0:
            self.index_tab.refresh()

    def closeEvent(self, event):
        self.db.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
