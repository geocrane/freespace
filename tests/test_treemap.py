"""Тесты squarified-раскладки."""

from __future__ import annotations

from freespace.core.treemap import PALETTE, Rect, color_for, squarify


def test_empty_values():
    assert squarify([], Rect(0, 0, 100, 100)) == []


def test_areas_match_values():
    """Площадь каждой плитки пропорциональна её значению."""
    values = [50.0, 30.0, 15.0, 5.0]
    rects = squarify(values, Rect(0, 0, 200, 100))

    total_area = sum(r.w * r.h for r in rects)
    assert total_area == 20000.0

    for value, rect in zip(values, rects):
        expected = value / sum(values) * 20000.0
        assert abs(rect.w * rect.h - expected) < 1e-6


def test_rects_stay_inside_bounds():
    rects = squarify([7.0, 5.0, 3.0, 2.0, 1.0], Rect(10, 20, 300, 150))
    for r in rects:
        assert r.x >= 10 - 1e-9 and r.y >= 20 - 1e-9
        assert r.x + r.w <= 310 + 1e-9
        assert r.y + r.h <= 170 + 1e-9


def test_order_is_preserved():
    """Прямоугольники возвращаются в порядке исходных значений."""
    rects = squarify([100.0, 10.0, 1.0], Rect(0, 0, 100, 100))
    areas = [r.w * r.h for r in rects]
    assert areas[0] > areas[1] > areas[2]


def test_zero_total_does_not_crash():
    rects = squarify([0.0, 0.0], Rect(0, 0, 100, 100))
    assert len(rects) == 2
    assert all(r.w == 0 and r.h == 0 for r in rects)


def test_palette_cycles():
    assert color_for(0) == PALETTE[0]
    assert color_for(len(PALETTE)) == PALETTE[0]
    assert color_for(len(PALETTE) + 3) == PALETTE[3]
