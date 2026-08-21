"""Модель и виджет дерева папок поверх FileNode."""

from __future__ import annotations

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QTreeView

from ..core.model import FileNode
from ..core.platform_utils import human_size

COL_NAME, COL_SIZE, COL_PERCENT, COL_COUNT = range(4)
_HEADERS = ("Имя", "Размер", "% от родителя", "Файлов")


class FileTreeModel(QAbstractItemModel):
    """Модель дерева: оборачивает корневой FileNode для QTreeView."""

    def __init__(self, root: FileNode | None = None) -> None:
        super().__init__()
        self._root = root
        # Кэш родителей: id ребёнка -> родительский FileNode.
        self._parents: dict[int, FileNode] = {}
        if root is not None:
            self._index_parents(root)

    def set_root(self, root: FileNode | None) -> None:
        self.beginResetModel()
        self._root = root
        self._parents = {}
        if root is not None:
            self._index_parents(root)
        self.endResetModel()

    def _index_parents(self, node: FileNode) -> None:
        if node.children:
            for child in node.children:
                self._parents[id(child)] = node
                self._index_parents(child)

    def node_from_index(self, index: QModelIndex) -> FileNode | None:
        if not index.isValid():
            return self._root
        return index.internalPointer()

    # --- обязательные методы QAbstractItemModel -------------------------

    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        parent_node = self.node_from_index(parent)
        if parent_node is None or not parent_node.children:
            return QModelIndex()
        children = parent_node.sorted_children()
        if row >= len(children):
            return QModelIndex()
        return self.createIndex(row, column, children[row])

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        node = index.internalPointer()
        parent_node = self._parents.get(id(node))
        if parent_node is None or parent_node is self._root:
            return QModelIndex()
        grand = self._parents.get(id(parent_node))
        if grand is None:
            row = 0
        else:
            row = grand.sorted_children().index(parent_node)
        return self.createIndex(row, 0, parent_node)

    def rowCount(self, parent=QModelIndex()):
        node = self.node_from_index(parent)
        if node is None or not node.children:
            return 0
        return len(node.children)

    def columnCount(self, parent=QModelIndex()):
        return len(_HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return _HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        node: FileNode = index.internalPointer()
        col = index.column()

        if role == Qt.DisplayRole:
            if col == COL_NAME:
                return node.name
            if col == COL_SIZE:
                return human_size(node.size)
            if col == COL_PERCENT:
                parent = self._parents.get(id(node))
                if parent and parent.size > 0:
                    return f"{node.size / parent.size * 100:.0f}%"
                return ""
            if col == COL_COUNT:
                return str(node.file_count) if node.is_dir else ""

        if role == Qt.TextAlignmentRole and col in (COL_SIZE, COL_PERCENT, COL_COUNT):
            return int(Qt.AlignRight | Qt.AlignVCenter)

        if role == Qt.ForegroundRole and not node.is_dir:
            return QColor(120, 120, 120)

        return None


class FileTreeView(QTreeView):
    """QTreeView с разумными настройками для отображения дерева размеров."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(False)
        self.setExpandsOnDoubleClick(True)
        self.setAllColumnsShowFocus(True)

    def set_root_node(self, root: FileNode | None) -> None:
        model = FileTreeModel(root)
        self.setModel(model)
        self.setColumnWidth(COL_NAME, 360)
        for col in (COL_SIZE, COL_PERCENT, COL_COUNT):
            self.setColumnWidth(col, 120)
