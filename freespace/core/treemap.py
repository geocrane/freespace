"""Squarified treemap: раскладка значений по прямоугольникам.

Алгоритм Bruls/Huizing/van Wijk. Чистый Python без привязки к способу
отрисовки — используется и Qt-виджетом, и веб-интерфейсом.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float


def squarify(values: list[float], rect: Rect) -> list[Rect]:
    """Разложить значения в прямоугольники внутри ``rect``.

    Значения должны быть отсортированы по убыванию. Возвращает список
    прямоугольников в исходном порядке значений.
    """
    if not values:
        return []
    total = sum(values)
    if total <= 0:
        return [Rect(rect.x, rect.y, 0, 0) for _ in values]

    # Нормируем значения к площади прямоугольника.
    scale = (rect.w * rect.h) / total
    scaled = [v * scale for v in values]

    result: list[Rect] = [None] * len(values)  # type: ignore[list-item]
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
                result[idx] = Rect(x, oy, col_w, rh)
                oy += rh
            x += col_w
            w -= col_w
        else:
            # выкладываем ряд сверху, высотой row_sum / w
            row_h = row_sum / w if w > 0 else 0
            ox = x
            for idx, area in zip(row_idx, row):
                rw = area / row_h if row_h > 0 else 0
                result[idx] = Rect(ox, y, rw, row_h)
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


# Палитра плиток по индексу. Hex-строки, а не объекты цвета конкретного
# тулкита, — так палитра одинаково годится и для Qt, и для SVG.
PALETTE = (
    "#4F86C6", "#6FB36F", "#D9A14E", "#C06C84",
    "#7E6BC4", "#55A8A8", "#B57E4A", "#8F9B4E",
)


def color_for(index: int) -> str:
    """Цвет плитки по её порядковому номеру."""
    return PALETTE[index % len(PALETTE)]
