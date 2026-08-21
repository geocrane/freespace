"""Тесты сканера: корректность агрегированных размеров и структуры."""

from __future__ import annotations

import os

from freespace.core.scanner import SIZE_APPARENT, Scanner, scan


def test_total_size_aggregation(sample_tree):
    result = scan(sample_tree, size_mode=SIZE_APPARENT)
    assert result.root.size == 8350
    assert result.root.is_dir


def test_file_count(sample_tree):
    result = scan(sample_tree, size_mode=SIZE_APPARENT)
    # 5 файлов в дереве
    assert result.root.file_count == 5


def test_subtree_sizes(sample_tree):
    result = scan(sample_tree, size_mode=SIZE_APPARENT)
    children = {c.name: c for c in result.root.children}
    assert children["docs"].size == 5200  # 200 + 5000
    assert children["project"].size == 3050  # 3000 + 50
    assert children["a.txt"].size == 100
    assert not children["a.txt"].is_dir


def test_progress_callback(sample_tree):
    seen = []
    scanner = Scanner(on_progress=lambda c, p: seen.append(c), progress_every=1,
                      size_mode=SIZE_APPARENT)
    scanner.scan(sample_tree)
    assert seen  # коллбэк вызывался
    assert seen[-1] >= 5


def test_nonexistent_path():
    result = scan(os.path.join("definitely", "missing", "path"))
    # Не падает, корень с нулевым размером, путь пропущен
    assert result.root.size == 0
