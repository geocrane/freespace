"""Тесты сканера: корректность агрегированных размеров и структуры."""

from __future__ import annotations

import os
import time

from freespace.core.scanner import SIZE_APPARENT, Scanner, _activity, scan


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


# --- время последней активности --------------------------------------------
#
# Главная ловушка этой части: st_mtime переносится вместе с файлом при
# копировании, и «не менялось три года» сплошь и рядом означает «принесено
# вчера из старого архива». Поэтому берётся ещё и время появления файла здесь.


class _FakeStat:
    """Заглушка ``os.stat_result``: настоящие ctime и birthtime не подделать."""

    def __init__(self, mtime, ctime, birthtime=None):
        self.st_mtime = mtime
        self.st_ctime = ctime
        if birthtime is not None:
            self.st_birthtime = birthtime


def test_activity_prefers_the_newer_of_the_two_stamps():
    old, now = 1_000_000.0, 2_000_000.0
    # Файл принесли сюда сегодня, а содержимое не меняли годами.
    assert _activity(_FakeStat(mtime=old, ctime=now)) == now
    # Содержимое поменяли только что, а лежит он тут давно.
    assert _activity(_FakeStat(mtime=now, ctime=old)) == now
    # Где есть birthtime (macOS, Windows с 3.12), берётся он, а не ctime.
    assert _activity(_FakeStat(mtime=old, ctime=old, birthtime=now)) == now


def test_copied_file_is_not_counted_as_ancient(tmp_path):
    """Старое время изменения при свежем появлении здесь — файл считается свежим.

    Ровно случай «компьютер 2025 года, а файлы показывают 2022-й»: содержимое
    действительно трёхлетней давности, но принесено оно сюда только что.
    """
    target = tmp_path / "copied.bin"
    target.write_bytes(b"x" * 10)
    three_years = time.time() - 3 * 365 * 86400
    os.utime(target, (three_years, three_years))
    assert os.stat(target).st_mtime < time.time() - 86400  # состарен по-настоящему

    root = scan(str(tmp_path), size_mode=SIZE_APPARENT).root
    node = next(c for c in root.children if c.name == "copied.bin")
    assert node.mtime > time.time() - 86400


def test_empty_folder_keeps_its_own_time(tmp_path):
    """Файлов внутри нет — говорить об их возрасте нечего, остаётся своё время."""
    (tmp_path / "пусто").mkdir()
    root = scan(str(tmp_path), size_mode=SIZE_APPARENT).root
    node = next(c for c in root.children if c.name == "пусто")
    assert node.mtime > 0
