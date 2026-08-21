"""Тесты честных размеров: жёсткие ссылки, место на диске, параллелизм."""

from __future__ import annotations

import os

import pytest

from freespace.core.scanner import SIZE_APPARENT, SIZE_DISK, Scanner, scan


@pytest.fixture
def linked_tree(tmp_path):
    """Один файл под двумя именами: место он занимает один раз."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    original = root / "original.bin"
    original.write_bytes(b"x" * 40000)
    try:
        os.link(original, root / "sub" / "same.bin")
    except (OSError, NotImplementedError):
        pytest.skip("файловая система не поддерживает жёсткие ссылки")
    return str(root)


def test_hardlink_counted_once(linked_tree):
    result = scan(linked_tree, size_mode=SIZE_APPARENT)

    assert result.root.size == 40000, "файл с двумя именами занимает место один раз"
    assert result.hardlink_saved == 40000


def test_hardlink_duplicate_is_marked(linked_tree):
    result = scan(linked_tree, size_mode=SIZE_APPARENT)
    duplicates = [n for n in result.root.iter_subtree() if n.is_hardlink_dup]

    assert len(duplicates) == 1
    assert duplicates[0].size == 0, "повтор не несёт размера, иначе он посчитан дважды"


def test_dedup_can_be_turned_off(linked_tree):
    result = scan(linked_tree, size_mode=SIZE_APPARENT, dedup_hardlinks=False)

    assert result.root.size == 80000, "без дедупликации оба имени считаются полностью"
    assert result.hardlink_saved == 0


@pytest.mark.skipif(os.name == "nt", reason="st_blocks есть только на POSIX")
def test_disk_size_rounds_up_to_blocks(tmp_path):
    """Файл в 10 байт занимает на диске целый блок, а не 10 байт."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "tiny.txt").write_bytes(b"x" * 10)

    apparent = scan(str(root), size_mode=SIZE_APPARENT).root.size
    on_disk = scan(str(root), size_mode=SIZE_DISK).root.size

    assert apparent == 10
    assert on_disk >= 512
    assert on_disk % 512 == 0


def _deep_tree(base, width=4, depth=5):
    """Дерево, которое гарантированно не помещается в один каталог."""
    made = 0
    stack = [(base, 0)]
    while stack:
        path, level = stack.pop()
        os.makedirs(path, exist_ok=True)
        for i in range(width):
            with open(os.path.join(path, f"f{i}.bin"), "wb") as fh:
                fh.write(b"x" * (100 + i))
            made += 1
        if level < depth:
            for i in range(2):
                stack.append((os.path.join(path, f"d{i}"), level + 1))
    return made


def test_parallel_scan_matches_single_threaded(tmp_path):
    """Результат обхода не должен зависеть от числа воркеров."""
    root = tmp_path / "root"
    expected_files = _deep_tree(str(root))

    single = Scanner(max_workers=1, size_mode=SIZE_APPARENT).scan(str(root))
    parallel = Scanner(max_workers=16, size_mode=SIZE_APPARENT).scan(str(root))

    assert single.root.size == parallel.root.size
    assert single.root.file_count == parallel.root.file_count == expected_files
    summary = lambda res: sorted((n.path, n.size) for n in res.root.iter_subtree())
    assert summary(single) == summary(parallel)


def test_size_mode_is_reported_back(tmp_path):
    """Режим едет в результате: кэш обязан знать, чем считали."""
    root = tmp_path / "root"
    root.mkdir()
    assert scan(str(root)).size_mode == SIZE_DISK
    assert scan(str(root), size_mode=SIZE_APPARENT).size_mode == SIZE_APPARENT
