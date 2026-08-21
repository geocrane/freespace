"""Treemap-визуализация (squarified) на QGraphicsView."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from ..core.model import FileNode
from ..core.platform_utils import human_size


@dataclass
class _Rect:
    x: float
    y: float
    w: float
    h: float


def squarify(values: list[float], rect: _Rect) -> list[_Rect]:
    """Squarified treemap: разложить значения в прямоугольники внутри rect.

    Значения должны быть отсортированы по убыванию. Возвращает список
    прямоугольников в исходном порядке значений.
    """
    if not values:
        return []
    total = sum(values)
    if total <= 0:
        return [_Rect(rect.x, rect.y, 0, 0) for _ in values]

    # Нормируем значения к площади прямоугольника.
    scale = (rect.w * rect.h) / total
    scaled = [v * scale for v in values]

    result: list[_Rect] = [None] * len(values)  # type: ignore[list-item]
    x, y, w, h = rect.x, rect.y, rect.w, rect.h
    i = 0
    n = len(scaled)
    while i < n:
        short_side = min(w, h)
        row: list[float] = [scaled[i]]
        row_idx = [i]
        j = i + 1
        while j < n and _worst(row + [scaled[j]], short_side) <= _worst(row, short_side):
            row.append(scaled[j])
            row_idx.append(j)
            j += 1

        row_sum = sum(row)
        if w >= h:
            # выкладываем столбец слева, шириной row_sum / h
            col_w = row_sum / h if h > 0 else 0
            oy = y
            for idx, area in zip(row_idx, row):
                rh = area / col_w if col_w > 0 else 0
                result[idx] = _Rect(x, oy, col_w, rh)
                oy += rh
            x += col_w
            w -= col_w
        else:
            # выкладываем ряд сверху, высотой row_sum / w
            row_h = row_sum / w if w > 0 else 0
            ox = x
            for idx, area in zip(row_idx, row):
                rw = area / row_h if row_h > 0 else 0
                result[idx] = _Rect(ox, y, rw, row_h)
                ox += rw
            y += row_h
            h -= row_h
        i = j
    return result


def _worst(row: list[float], short_side: float) -> float:
    """Худшее (макс) соотношение сторон для ряда — критерий squarified."""
    if not row or short_side <= 0:
        return float("inf")
    s = sum(row)
    if s <= 0:
        return float("inf")
    rmax = max(row)
    rmin = min(row)
    side_sq = short_side * short_side
    s_sq = s * s
    return max(side_sq * rmax / s_sq, s_sq / (side_sq * rmin))


# Палитра плиток по индексу.
_PALETTE = [
    QColor(0x4F, 0x86, 0xC6), QColor(0x6F, 0xB3, 0x6F), QColor(0xD9, 0xA1, 0x4E),
    QColor(0xC0, 0x6C, 0x84), QColor(0x7E, 0x6B, 0xC4), QColor(0x55, 0xA8, 0xA8),
    QColor(0xB5, 0x7E, 0x4A), QColor(0x8F, 0x9B, 0x4E),
]


class TreemapView(QGraphicsView):
    """Виджет treemap: показывает детей узла прямоугольниками по размеру."""

    node_clicked = Signal(object)        # FileNode
    node_activated = Signal(object)      # FileNode (двойной клик / drill-down)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.TextAntialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._node: FileNode | None = None
        self._item_map: dict[QGraphicsRectItem, FileNode] = {}
        self._min_label_px = 38

    def set_node(self, node: FileNode | None) -> None:
        """Показать детей ``node`` в виде treemap."""
        self._node = node
        self._render()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._render()

    def _render(self) -> None:
        self._scene.clear()
        self._item_map.clear()
        node = self._node
        if node is None or not node.children:
            return

        children = [c for c in node.sorted_children() if c.size > 0]
        if not children:
            return

        vw = max(1, self.viewport().width() - 2)
        vh = max(1, self.viewport().height() - 2)
        self._scene.setSceneRect(0, 0, vw, vh)

        rects = squarify([float(c.size) for c in children], _Rect(0, 0, vw, vh))
        for i, (child, r) in enumerate(zip(children, rects)):
            if r.w <= 0 or r.h <= 0:
                continue
            color = _PALETTE[i % len(_PALETTE)]
            if not child.is_dir:
                color = color.lighter(120)
            item = QGraphicsRectItem(QRectF(r.x, r.y, r.w, r.h))
            item.setBrush(QBrush(color))
            item.setPen(QPen(QColor(255, 255, 255), 1))
            item.setToolTip(f"{child.name}\n{human_size(child.size)}")
            self._scene.addItem(item)
            self._item_map[item] = child

            if r.w >= self._min_label_px and r.h >= 18:
                self._add_label(child, r)

    def _add_label(self, child: FileNode, r: _Rect) -> None:
        text = f"{child.name}\n{human_size(child.size)}"
        label = QGraphicsSimpleTextItem(text)
        font = QFont()
        font.setPointSize(8)
        label.setFont(font)
        label.setBrush(QBrush(QColor(255, 255, 255)))
        # Обрезаем подпись, если не влезает по ширине.
        br = label.boundingRect()
        if br.width() > r.w - 4:
            label.setText(child.name[:18])
            br = label.boundingRect()
        label.setPos(r.x + 4, r.y + 3)
        label.setFlag(label.GraphicsItemFlag.ItemClipsToShape, False)
        if br.width() <= r.w - 4 and br.height() <= r.h - 4:
            self._scene.addItem(label)

    def _node_at(self, view_pos) -> FileNode | None:
        scene_pos = self.mapToScene(view_pos)
        for item in self._scene.items(scene_pos):
            if isinstance(item, QGraphicsRectItem) and item in self._item_map:
                return self._item_map[item]
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        node = self._node_at(event.position().toPoint())
        if node is not None:
            self.node_clicked.emit(node)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        node = self._node_at(event.position().toPoint())
        if node is not None and node.is_dir and node.children:
            self.node_activated.emit(node)
        super().mouseDoubleClickEvent(event)
