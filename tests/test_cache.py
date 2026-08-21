"""Тесты кэша снимков: round-trip, замена, вытеснение, устойчивость к мусору."""

from __future__ import annotations

import gzip
import os
import time

from freespace.core.cache import SUFFIX, Cache
from freespace.core.scanner import SIZE_APPARENT, scan


def _node_summary(node):
    """Множество (path, size, is_dir) для всего поддерева — для сравнения."""
    return {(n.path, n.size, n.is_dir) for n in node.iter_subtree()}


def test_save_and_load_roundtrip(sample_tree, tmp_path):
    result = scan(sample_tree, size_mode=SIZE_APPARENT)
    cache = Cache(dir_path=str(tmp_path / "cache"))

    cache.save_snapshot(result.root, size_mode=SIZE_APPARENT)
    loaded = cache.load_snapshot(sample_tree)

    assert loaded is not None
    assert loaded.size == result.root.size
    assert loaded.file_count == result.root.file_count
    assert _node_summary(loaded) == _node_summary(result.root)


def test_names_with_tabs_and_backslashes_survive(tmp_path):
    """Имена с табуляцией и обратным слэшем не должны рвать построчный формат."""
    root = tmp_path / "root"
    root.mkdir()
    tricky = ["с\tтабом.txt", "с\\слэшем.txt", "обычный.txt"]
    for name in tricky:
        (root / name).write_bytes(b"x" * 10)

    result = scan(str(root), size_mode=SIZE_APPARENT)
    cache = Cache(dir_path=str(tmp_path / "cache"))
    cache.save_snapshot(result.root, size_mode=SIZE_APPARENT)
    loaded = cache.load_snapshot(str(root))

    assert loaded is not None
    assert {c.name for c in loaded.children} == set(tricky)


def test_resave_replaces_previous(sample_tree, tmp_path):
    """Снимок один на корень: повторное сохранение заменяет прежний."""
    cache = Cache(dir_path=str(tmp_path / "cache"))
    root = scan(sample_tree, size_mode=SIZE_APPARENT).root
    cache.save_snapshot(root, size_mode=SIZE_APPARENT)
    cache.save_snapshot(root, size_mode=SIZE_APPARENT)

    assert len(cache.list_snapshots()) == 1


def test_latest_snapshot_reads_only_header(sample_tree, tmp_path):
    cache = Cache(dir_path=str(tmp_path / "cache"))
    root = scan(sample_tree, size_mode=SIZE_APPARENT).root
    cache.save_snapshot(root, size_mode=SIZE_APPARENT)

    info = cache.latest_snapshot(sample_tree)
    assert info is not None
    assert info.root_path == os.path.abspath(sample_tree)
    assert info.total_size == 8350
    assert info.file_count == 5
    assert info.size_mode == SIZE_APPARENT
    assert info.bytes_on_disk > 0


def test_corrupt_snapshot_is_discarded(sample_tree, tmp_path):
    """Оборванный файл не должен ронять приложение — его просто выбрасывают."""
    cache_dir = tmp_path / "cache"
    cache = Cache(dir_path=str(cache_dir))
    cache.save_snapshot(scan(sample_tree, size_mode=SIZE_APPARENT).root,
                        size_mode=SIZE_APPARENT)

    snapshot_file = next(p for p in cache_dir.iterdir() if p.name.endswith(SUFFIX))
    with gzip.open(snapshot_file, "wt", encoding="utf-8") as fh:
        fh.write('{"v":1,"root":"/x","created":1,"total":1,"files":1}\n0\td\tмусор\n')

    assert cache.load_snapshot(sample_tree) is None
    assert not snapshot_file.exists(), "битый снимок должен удаляться"


def test_limit_evicts_oldest(sample_tree, tmp_path):
    """При превышении потолка вытесняются самые старые снимки."""
    cache_dir = tmp_path / "cache"
    cache = Cache(dir_path=str(cache_dir), limit_bytes=1)
    root = scan(sample_tree, size_mode=SIZE_APPARENT).root

    # Кладём два снимка разных корней: потолок в 1 байт переживёт только один.
    cache.save_snapshot(root, size_mode=SIZE_APPARENT)
    other = scan(str(tmp_path), size_mode=SIZE_APPARENT).root
    time.sleep(0.01)
    cache.save_snapshot(other, size_mode=SIZE_APPARENT)

    assert len(cache.list_snapshots()) == 1


def test_expired_snapshot_is_removed(sample_tree, tmp_path):
    cache_dir = tmp_path / "cache"
    cache = Cache(dir_path=str(cache_dir), max_age_days=1)
    cache.save_snapshot(scan(sample_tree, size_mode=SIZE_APPARENT).root,
                        size_mode=SIZE_APPARENT)

    snapshot_file = next(p for p in cache_dir.iterdir() if p.name.endswith(SUFFIX))
    old = time.time() - 5 * 86400
    os.utime(snapshot_file, (old, old))

    cache.enforce_limits()
    assert cache.list_snapshots() == []


def test_clear_frees_everything(sample_tree, tmp_path):
    cache = Cache(dir_path=str(tmp_path / "cache"))
    cache.save_snapshot(scan(sample_tree, size_mode=SIZE_APPARENT).root,
                        size_mode=SIZE_APPARENT)

    assert cache.total_bytes() > 0
    assert cache.clear() > 0
    assert cache.total_bytes() == 0
    assert cache.list_snapshots() == []


def test_snapshot_older_than_a_day_is_dropped(sample_tree, tmp_path):
    """Сутки без пересканирования — и данные берутся с диска заново.

    За день дерево успевает разойтись с диском настолько, что показывать
    вчерашние цифры вреднее, чем подождать нового обхода.
    """
    cache_dir = tmp_path / "cache"
    cache = Cache(dir_path=str(cache_dir))
    cache.save_snapshot(scan(sample_tree, size_mode=SIZE_APPARENT).root,
                        size_mode=SIZE_APPARENT)
    assert cache.latest_snapshot(sample_tree) is not None

    snapshot_file = next(p for p in cache_dir.iterdir() if p.name.endswith(SUFFIX))
    _age_snapshot(snapshot_file, hours=25)

    assert cache.latest_snapshot(sample_tree) is None
    assert not snapshot_file.exists(), "просроченный снимок должен удаляться"


def test_snapshot_within_a_day_survives(sample_tree, tmp_path):
    cache_dir = tmp_path / "cache"
    cache = Cache(dir_path=str(cache_dir))
    cache.save_snapshot(scan(sample_tree, size_mode=SIZE_APPARENT).root,
                        size_mode=SIZE_APPARENT)

    snapshot_file = next(p for p in cache_dir.iterdir() if p.name.endswith(SUFFIX))
    _age_snapshot(snapshot_file, hours=23)

    assert cache.latest_snapshot(sample_tree) is not None


def _age_snapshot(path, hours):
    """Состарить снимок: возраст берётся из заголовка внутри файла."""
    import json

    with gzip.open(path, "rt", encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    header = json.loads(lines[0])
    header["created"] = time.time() - hours * 3600
    lines[0] = json.dumps(header, ensure_ascii=False)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
