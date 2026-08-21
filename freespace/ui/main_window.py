"""Главное окно FreeSpace."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QSettings, Qt, QThreadPool
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..core.cache import Cache
from ..core.model import FileNode
from ..core.platform_utils import human_size, open_path, reveal_in_explorer
from ..core.search import find_by_name, group_by_exact_name
from .scan_worker import ScanWorker
from .tree_view import FileTreeView
from .treemap_widget import TreemapView


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FreeSpace — анализ дискового пространства")
        self.resize(1100, 720)

        self._settings = QSettings("FreeSpace", "FreeSpace")
        self._cache = Cache()
        self._pool = QThreadPool.globalInstance()
        self._worker: ScanWorker | None = None
        self._root: FileNode | None = None
        self._current_path: str | None = None
        self._scan_started = 0.0

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._refresh_recent()
        self._restore_state()

    # --- построение интерфейса ------------------------------------------

    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setObjectName("mainToolBar")
        tb.setMovable(False)
        self.addToolBar(tb)

        open_act = QAction("Открыть папку…", self)
        open_act.triggered.connect(self._choose_folder)
        tb.addAction(open_act)

        self._refresh_act = QAction("Обновить", self)
        self._refresh_act.setShortcut(QKeySequence.Refresh)
        self._refresh_act.triggered.connect(self._rescan_current)
        self._refresh_act.setEnabled(False)
        tb.addAction(self._refresh_act)

        self._cancel_act = QAction("Отмена", self)
        self._cancel_act.triggered.connect(self._cancel_scan)
        self._cancel_act.setEnabled(False)
        tb.addAction(self._cancel_act)

        tb.addSeparator()
        tb.addWidget(QLabel(" Недавние: "))
        self._recent_combo = QComboBox()
        self._recent_combo.setMinimumWidth(320)
        self._recent_combo.activated.connect(self._open_recent)
        tb.addWidget(self._recent_combo)

        tb.addSeparator()
        tb.addWidget(QLabel(" Поиск: "))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("имя папки, напр. venv  (Enter)")
        self._search_edit.setMaximumWidth(260)
        self._search_edit.returnPressed.connect(self._run_search)
        tb.addWidget(self._search_edit)
        search_btn = QPushButton("Найти")
        search_btn.clicked.connect(self._run_search)
        tb.addWidget(search_btn)

    def _build_central(self) -> None:
        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setObjectName("mainSplitter")
        splitter = self._splitter

        # Левая панель: дерево + результаты поиска (перетаскиваемый разделитель).
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self._breadcrumb = QLabel("")
        self._breadcrumb.setStyleSheet("color: #888; padding: 2px 4px;")
        left_layout.addWidget(self._breadcrumb)

        self._tree = FileTreeView()
        self._tree.clicked.connect(self._on_tree_clicked)
        self._tree.doubleClicked.connect(self._on_tree_double_clicked)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._tree_context_menu)
        left_layout.addWidget(self._tree, 2)

        # Панель результатов поиска (скрыта, пока не выполнен поиск).
        self._search_panel = QWidget()
        sp_layout = QVBoxLayout(self._search_panel)
        sp_layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        self._search_label = QLabel("")
        self._search_label.setStyleSheet("color: #555; padding: 2px 4px;")
        header.addWidget(self._search_label, 1)
        close_btn = QPushButton("Закрыть поиск")
        close_btn.clicked.connect(self._clear_search)
        header.addWidget(close_btn, 0)
        sp_layout.addLayout(header)

        self._results = QTableWidget(0, 3)
        self._results.setHorizontalHeaderLabels(["Имя", "Размер", "Путь"])
        self._results.verticalHeader().setVisible(False)
        self._results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._results.setSelectionMode(QAbstractItemView.SingleSelection)
        self._results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._results.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._results.setColumnWidth(0, 160)
        self._results.setColumnWidth(1, 90)
        self._results.itemClicked.connect(self._on_result_clicked)
        self._results.itemDoubleClicked.connect(self._on_result_double_clicked)
        self._results.setContextMenuPolicy(Qt.CustomContextMenu)
        self._results.customContextMenuRequested.connect(self._results_context_menu)
        sp_layout.addWidget(self._results)

        self._search_panel.hide()
        left_layout.addWidget(self._search_panel, 1)

        splitter.addWidget(left)

        # Правая панель: treemap.
        self._treemap = TreemapView()
        self._treemap.node_clicked.connect(self._on_treemap_clicked)
        self._treemap.node_activated.connect(self._on_treemap_activated)
        splitter.addWidget(self._treemap)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        self.setCentralWidget(splitter)

    def _build_statusbar(self) -> None:
        self._progress = QProgressBar()
        self._progress.setMaximumWidth(180)
        self._progress.setRange(0, 0)  # неопределённый
        self._progress.hide()
        self.statusBar().addPermanentWidget(self._progress)
        self._status_label = QLabel("Откройте папку для анализа.")
        self.statusBar().addWidget(self._status_label)

    # --- сохранение/восстановление состояния окна -----------------------

    def _save_state(self) -> None:
        # Сохраняем только положение главного разделителя — ширину панели treemap.
        self._settings.setValue("mainSplitter", self._splitter.saveState())
        self._settings.sync()

    def _restore_state(self) -> None:
        # Чистим устаревшие ключи от прежних версий (сохраняем только mainSplitter).
        for old_key in ("geometry", "windowState", "leftSplitter", "treeHeader", "resultsHeader"):
            self._settings.remove(old_key)
        main_split = self._settings.value("mainSplitter")
        if main_split is not None:
            self._splitter.restoreState(main_split)

    # --- работа с папками и кэшем ---------------------------------------

    def _refresh_recent(self) -> None:
        self._recent_combo.blockSignals(True)
        self._recent_combo.clear()
        self._recent_combo.addItem("— недавние сканы —", None)
        for snap in self._cache.list_snapshots():
            when = time.strftime("%d.%m %H:%M", time.localtime(snap.created_at))
            label = f"{snap.root_path}  ({human_size(snap.total_size)}, {when})"
            self._recent_combo.addItem(label, snap.root_path)
        self._recent_combo.blockSignals(False)

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Выберите папку или диск")
        if path:
            self.open_folder(path)

    def _open_recent(self, index: int) -> None:
        path = self._recent_combo.itemData(index)
        if path:
            self.open_folder(path)

    def open_folder(self, path: str, force_rescan: bool = False) -> None:
        """Открыть папку: показать из кэша, если есть, иначе сканировать."""
        path = os.path.abspath(path)
        self._current_path = path
        if not force_rescan:
            cached = self._cache.load_snapshot(path)
            if cached is not None:
                info = self._cache.latest_snapshot(path)
                self._set_root(cached)
                when = time.strftime("%d.%m %H:%M", time.localtime(info.created_at)) if info else ""
                self._status_label.setText(
                    f"Из кэша: {path} — {human_size(cached.size)}, "
                    f"{cached.file_count} файлов, снапшот {when}. Нажмите «Обновить» для пересканирования."
                )
                self._refresh_act.setEnabled(True)
                return
        self._start_scan(path)

    def _rescan_current(self) -> None:
        if self._current_path:
            self.open_folder(self._current_path, force_rescan=True)

    def _start_scan(self, path: str) -> None:
        self._scan_started = time.time()
        self._status_label.setText(f"Сканирование: {path} …")
        self._progress.show()
        self._cancel_act.setEnabled(True)
        self._refresh_act.setEnabled(False)

        worker = ScanWorker(path)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_scan_finished)
        worker.signals.error.connect(self._on_scan_error)
        worker.signals.cancelled.connect(self._on_scan_cancelled)
        self._worker = worker
        self._pool.start(worker)

    def _cancel_scan(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._status_label.setText("Отмена сканирования…")

    # --- коллбэки сканирования ------------------------------------------

    def _on_progress(self, count: int, path: str) -> None:
        self._status_label.setText(f"Сканирование… обработано {count:,} объектов")

    def _on_scan_finished(self, root: FileNode, skipped: list) -> None:
        self._progress.hide()
        self._cancel_act.setEnabled(False)
        self._refresh_act.setEnabled(True)
        self._worker = None
        try:
            self._cache.save_snapshot(root)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Кэш", f"Не удалось сохранить кэш: {exc}")
        self._refresh_recent()
        self._set_root(root)
        elapsed = time.time() - self._scan_started
        skipped_txt = f", пропущено {len(skipped)}" if skipped else ""
        self._status_label.setText(
            f"{root.path} — {human_size(root.size)}, {root.file_count} файлов, "
            f"за {elapsed:.1f} с{skipped_txt}."
        )

    def _on_scan_error(self, message: str) -> None:
        self._progress.hide()
        self._cancel_act.setEnabled(False)
        self._worker = None
        QMessageBox.critical(self, "Ошибка сканирования", message)
        self._status_label.setText("Ошибка сканирования.")

    def _on_scan_cancelled(self) -> None:
        self._progress.hide()
        self._cancel_act.setEnabled(False)
        self._worker = None
        self._status_label.setText("Сканирование отменено.")

    # --- отображение -----------------------------------------------------

    def _set_root(self, root: FileNode) -> None:
        self._root = root
        self._tree.set_root_node(root)
        self._treemap.set_node(root)
        self._update_breadcrumb(root)
        self._clear_search()

    def _update_breadcrumb(self, node: FileNode) -> None:
        self._breadcrumb.setText(f"📂 {node.path}  —  {human_size(node.size)}")

    # --- взаимодействие: дерево <-> treemap ------------------------------

    def _on_tree_clicked(self, index) -> None:
        node = self._tree.model().node_from_index(index)
        if node and node.is_dir and node.children:
            self._treemap.set_node(node)
            self._update_breadcrumb(node)

    def _on_tree_double_clicked(self, index) -> None:
        node = self._tree.model().node_from_index(index)
        if node and not node.is_dir:
            reveal_in_explorer(node.path)

    def _on_treemap_clicked(self, node: FileNode) -> None:
        self._update_breadcrumb(node)
        # Подсветить узел в дереве (если виден) — раскрываем родителя.
        self.statusBar().showMessage(f"{node.path} — {human_size(node.size)}", 4000)

    def _on_treemap_activated(self, node: FileNode) -> None:
        # Drill-down: показать содержимое папки в treemap.
        self._treemap.set_node(node)
        self._update_breadcrumb(node)

    # --- контекстное меню ------------------------------------------------

    def _tree_context_menu(self, pos) -> None:
        index = self._tree.indexAt(pos)
        node = self._tree.model().node_from_index(index) if index.isValid() else None
        if node is None:
            return
        menu = QMenu(self)
        reveal = menu.addAction("Показать в проводнике")
        open_act = menu.addAction("Открыть") if node.is_dir else None
        chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if chosen == reveal:
            reveal_in_explorer(node.path)
        elif open_act is not None and chosen == open_act:
            open_path(node.path)

    # --- поиск -----------------------------------------------------------

    def _run_search(self) -> None:
        term = self._search_edit.text().strip()
        if not term or self._root is None:
            return
        use_glob = any(ch in term for ch in "*?")
        matches = find_by_name(self._root, term, dirs_only=not use_glob, use_glob=use_glob)

        self._results.setRowCount(0)
        self._search_panel.show()
        if not matches:
            self._search_label.setText(f"Ничего не найдено по «{term}».")
            return

        total = sum(n.size for n in matches)
        groups = group_by_exact_name(matches)
        summary = f"Найдено {len(matches)} по «{term}», суммарно {human_size(total)}."
        if len(groups) == 1:
            summary = (
                f"«{groups[0].name}»: {groups[0].count} шт., "
                f"суммарно {human_size(groups[0].total_size)}."
            )
        self._search_label.setText(summary)

        # Заполняем таблицу результатов (уже отсортированы по убыванию размера).
        self._results.setRowCount(len(matches))
        for row, node in enumerate(matches):
            name_item = QTableWidgetItem(node.name)
            name_item.setData(Qt.UserRole, node)
            size_item = QTableWidgetItem(human_size(node.size))
            size_item.setTextAlignment(int(Qt.AlignRight | Qt.AlignVCenter))
            # Сортировочное значение по размеру — числовое.
            size_item.setData(Qt.UserRole, node.size)
            path_item = QTableWidgetItem(node.path)
            self._results.setItem(row, 0, name_item)
            self._results.setItem(row, 1, size_item)
            self._results.setItem(row, 2, path_item)

        # Сразу показать крупнейшее совпадение в treemap.
        self._show_match(matches[0])
        self._results.selectRow(0)

    def _result_node(self, row: int) -> FileNode | None:
        item = self._results.item(row, 0)
        return item.data(Qt.UserRole) if item is not None else None

    def _show_match(self, node: FileNode) -> None:
        """Показать найденный узел: treemap + хлебные крошки + статус."""
        if node.is_dir and node.children:
            self._treemap.set_node(node)
        self._update_breadcrumb(node)
        self.statusBar().showMessage(f"{node.path} — {human_size(node.size)}", 6000)

    def _on_result_clicked(self, item: QTableWidgetItem) -> None:
        node = self._result_node(item.row())
        if node is not None:
            self._show_match(node)

    def _on_result_double_clicked(self, item: QTableWidgetItem) -> None:
        node = self._result_node(item.row())
        if node is not None:
            reveal_in_explorer(node.path)

    def _results_context_menu(self, pos) -> None:
        item = self._results.itemAt(pos)
        if item is None:
            return
        node = self._result_node(item.row())
        if node is None:
            return
        menu = QMenu(self)
        reveal = menu.addAction("Показать в проводнике")
        open_act = menu.addAction("Открыть папку") if node.is_dir else None
        chosen = menu.exec(self._results.viewport().mapToGlobal(pos))
        if chosen == reveal:
            reveal_in_explorer(node.path)
        elif open_act is not None and chosen == open_act:
            open_path(node.path)

    def _clear_search(self) -> None:
        self._search_panel.hide()
        self._results.setRowCount(0)
        self._search_label.clear()
        self._search_edit.clear()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_state()
        if self._worker is not None:
            self._worker.cancel()
        self._cache.close()
        super().closeEvent(event)
