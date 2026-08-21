"""Тесты слоя логики приложения: задачи сканирования и подготовка к показу."""

from __future__ import annotations

import os
import time

import pytest

from freespace.service.presenter import breadcrumbs, children_rows, layout_tiles
from freespace.core.scanner import SIZE_APPARENT, SIZE_DISK
from freespace.core.trash import ProtectedPathError
from freespace.service.scan_service import DONE, ScanService
from freespace.service.trash_service import TrashService


def _wait(job, timeout=10.0):
    deadline = time.time() + timeout
    while job.state == "running" and time.time() < deadline:
        time.sleep(0.01)
    return job


@pytest.fixture
def finished_job(sample_tree):
    service = ScanService()
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    assert job.state == DONE, job.error
    return service, job


def test_scan_runs_in_background_and_finishes(finished_job):
    _, job = finished_job
    assert job.result is not None
    assert job.result.root.size == 8350  # см. фикстуру sample_tree
    assert job.finished_at is not None


def test_start_rejects_bad_path(tmp_path):
    service = ScanService()
    with pytest.raises(NotADirectoryError):
        service.start(str(tmp_path / "нет-такого"))


def test_find_node_by_path(finished_job):
    service, job = finished_job
    root = service.find_node(job.id)

    docs = service.find_node(job.id, os.path.join(root.path, "docs"))
    assert docs is not None and docs.name == "docs"
    assert docs.size == 5200  # b.txt 200 + venv/big.bin 5000

    deep = service.find_node(job.id, os.path.join(root.path, "docs", "venv"))
    assert deep is not None and deep.size == 5000


def test_find_node_rejects_outside_paths(finished_job):
    service, job = finished_job
    assert service.find_node(job.id, "/etc") is None
    assert service.find_node(job.id, os.path.join(job.root_path, "нет")) is None


def test_unknown_job_returns_nothing():
    service = ScanService()
    assert service.get("нет-такой") is None
    assert service.find_node("нет-такой") is None
    assert service.cancel("нет-такой") is False


def test_tiles_cover_area_and_carry_paths(finished_job):
    service, job = finished_job
    root = service.find_node(job.id)
    tiles = layout_tiles(root, 400, 300)

    assert tiles, "у корня есть дети — плитки должны быть"
    assert all(t.w > 0 and t.h > 0 for t in tiles)
    # Крупнейший ребёнок — docs (5200 из 8350).
    assert tiles[0].name == "docs"
    assert tiles[0].drillable is True
    assert 62 < tiles[0].percent < 63


def test_tail_is_grouped(tmp_path):
    """Хвост из мелочи собирается в одну плитку, а не рисуется поштучно."""
    root = tmp_path / "many"
    root.mkdir()
    for i in range(50):
        (root / f"f{i:03d}.bin").write_bytes(b"x" * (100 - i))

    service = ScanService()
    job = _wait(service.start(str(root)))
    node = service.find_node(job.id)

    tiles = layout_tiles(node, 600, 400, max_tiles=10)
    grouped = [t for t in tiles if t.grouped]
    assert len(grouped) == 1
    assert grouped[0].grouped == 40
    # В подписи должно быть видно, сколько объектов свернули; точная
    # формулировка — дело интерфейса и меняется свободно.
    assert "40" in grouped[0].name


def test_rows_sorted_by_size(finished_job):
    service, job = finished_job
    rows = children_rows(service.find_node(job.id))
    sizes = [r.size for r in rows]
    assert sizes == sorted(sizes, reverse=True)


def test_breadcrumbs_chain(finished_job):
    service, job = finished_job
    root = service.find_node(job.id)
    deep = service.find_node(job.id, os.path.join(root.path, "docs", "venv"))

    chain = breadcrumbs(root, deep)
    assert [c["name"] for c in chain] == [root.name, "docs", "venv"]
    assert chain[0]["path"] == root.path
    assert chain[-1]["path"] == deep.path


def test_breadcrumbs_for_root_is_single_item(finished_job):
    service, job = finished_job
    root = service.find_node(job.id)
    assert len(breadcrumbs(root, root)) == 1


def test_old_jobs_are_evicted():
    service = ScanService(max_jobs=3)
    ids = []
    for _ in range(5):
        job = _wait(service.start(os.path.dirname(__file__)))
        ids.append(job.id)
    assert len(service.list_jobs()) <= 3
    assert service.get(ids[-1]) is not None, "последняя задача должна сохраниться"


# --- кэш снимков -----------------------------------------------------------


def test_second_scan_comes_from_cache(sample_tree):
    """Повторное открытие того же корня не должно снова обходить диск."""
    service = ScanService()
    first = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    assert first.from_cache is False
    _wait_for_snapshot(service, sample_tree)

    second = service.start(sample_tree, size_mode=SIZE_APPARENT)
    assert second.state == DONE
    assert second.from_cache is True
    assert second.snapshot_at is not None
    assert second.result.root.size == first.result.root.size


def test_rescan_ignores_cache(sample_tree):
    service = ScanService()
    _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    _wait_for_snapshot(service, sample_tree)

    fresh = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT, from_cache=False))
    assert fresh.from_cache is False


def test_cache_is_not_reused_across_size_modes(sample_tree):
    """Снимок «на диске» нельзя показать как номинальный: цифры разные."""
    service = ScanService()
    _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    _wait_for_snapshot(service, sample_tree)

    other = service.start(sample_tree, size_mode=SIZE_DISK)
    assert other.from_cache is False


def test_service_works_without_cache(sample_tree):
    service = ScanService(use_cache=False)
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))

    assert job.state == DONE
    assert job.from_cache is False
    assert service.cache is None


def _wait_for_snapshot(service, root_path, timeout=5.0):
    """Снимок пишется фоновым потоком — дожидаемся появления."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if service.cache and service.cache.latest_snapshot(root_path) is not None:
            return
        time.sleep(0.02)
    raise AssertionError("снимок так и не сохранился")


# --- удаление --------------------------------------------------------------


def test_delete_updates_tree_without_rescan(sample_tree):
    """После удаления размеры в памяти обязаны сойтись без повторного скана."""
    service = ScanService(use_cache=False)
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    trash = TrashService(service)

    entry = trash.delete(job.id, os.path.join(sample_tree, "docs"))

    assert entry.name == "docs"
    assert job.result.root.size == 3150  # было 8350, ушло 5200
    assert {c.name for c in job.result.root.children} == {"a.txt", "project"}
    assert not os.path.exists(os.path.join(sample_tree, "docs"))


def test_delete_refuses_scan_root(sample_tree):
    service = ScanService(use_cache=False)
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    trash = TrashService(service)

    with pytest.raises(ProtectedPathError):
        trash.delete(job.id, sample_tree)


def test_delete_refuses_path_outside_root(sample_tree, tmp_path):
    service = ScanService(use_cache=False)
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    trash = TrashService(service)
    outside = tmp_path / "чужое.txt"
    outside.write_bytes(b"x")

    with pytest.raises(ProtectedPathError):
        trash.delete(job.id, str(outside))


# --- групповое удаление ----------------------------------------------------


def _bulk_setup(sample_tree):
    service = ScanService(use_cache=False)
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    return service, job, TrashService(service)


def test_delete_many_removes_everything_marked(sample_tree):
    service, job, trash = _bulk_setup(sample_tree)

    result = trash.delete_many(job.id, [
        os.path.join(sample_tree, "docs"),
        os.path.join(sample_tree, "a.txt"),
    ])

    assert len(result.deleted) == 2
    assert result.freed == 5300  # 5200 + 100
    assert job.result.root.size == 3050
    assert not os.path.exists(os.path.join(sample_tree, "docs"))


def test_delete_many_skips_paths_inside_deleted_parent(sample_tree):
    """Отмечены и папка, и то, что внутри: вложенное уезжает вместе с ней."""
    service, job, trash = _bulk_setup(sample_tree)

    result = trash.delete_many(job.id, [
        os.path.join(sample_tree, "docs", "venv"),   # внутри docs
        os.path.join(sample_tree, "docs"),
    ])

    assert [e.name for e in result.deleted] == ["docs"]
    assert result.inside_deleted == [os.path.join(sample_tree, "docs", "venv")]
    assert result.failed == []


def test_delete_many_reports_failures_and_keeps_going(sample_tree):
    """Один запрещённый путь не должен отменять удаление остальных."""
    service, job, trash = _bulk_setup(sample_tree)

    result = trash.delete_many(job.id, [
        "/usr",                                   # вне корня скана
        os.path.join(sample_tree, "a.txt"),
    ])

    assert [e.name for e in result.deleted] == ["a.txt"]
    assert len(result.failed) == 1
    assert result.failed[0][0] == "/usr"
    assert os.path.exists("/usr")


def test_delete_many_handles_sibling_with_similar_name(sample_tree, tmp_path):
    """/a-b не должен приниматься за содержимое /a: сортировка идёт по частям пути."""
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "a-b").mkdir()
    (root / "a" / "in.bin").write_bytes(b"x" * 10)
    (root / "a-b" / "in.bin").write_bytes(b"x" * 20)

    service = ScanService(use_cache=False)
    job = _wait(service.start(str(root), size_mode=SIZE_APPARENT))
    trash = TrashService(service)

    result = trash.delete_many(job.id, [str(root / "a"), str(root / "a-b")])

    assert len(result.deleted) == 2
    assert result.inside_deleted == []
    assert result.failed == []


# --- снимок после изменений на диске ---------------------------------------


def _snapshot_exists(service, path):
    return service.cache is not None and service.cache.latest_snapshot(path) is not None


def test_delete_invalidates_snapshot(sample_tree):
    """Снимок описывает дерево до удаления.

    Если его не выбросить, следующее нажатие «Сканировать» покажет удалённые
    папки на прежних местах — как будто удаления не было.
    """
    service = ScanService()
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    _wait_for_snapshot(service, sample_tree)
    trash = TrashService(service)

    trash.delete(job.id, os.path.join(sample_tree, "docs"))

    assert not _snapshot_exists(service, sample_tree)
    fresh = service.start(sample_tree, size_mode=SIZE_APPARENT)
    assert fresh.from_cache is False


def test_delete_many_invalidates_snapshot_once(sample_tree):
    service = ScanService()
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    _wait_for_snapshot(service, sample_tree)
    trash = TrashService(service)

    trash.delete_many(job.id, [
        os.path.join(sample_tree, "docs"),
        os.path.join(sample_tree, "a.txt"),
    ])

    assert not _snapshot_exists(service, sample_tree)


def test_restore_invalidates_snapshot(sample_tree):
    service = ScanService()
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    trash = TrashService(service)
    entry = trash.delete(job.id, os.path.join(sample_tree, "docs"))
    _wait(service.start(sample_tree, size_mode=SIZE_APPARENT, from_cache=False))
    _wait_for_snapshot(service, sample_tree)

    trash.restore(job.id, entry.id)

    assert not _snapshot_exists(service, sample_tree)


# --- «уже нет» и пересканирование папки ------------------------------------


def test_missing_paths_are_success_not_failure(sample_tree):
    """Объекта уже нет — цель достигнута.

    Раньше это считалось ошибкой, и на пачке из двухсот путей, половина
    которых была вторыми именами первой половины, получалась стена из сотни
    одинаковых красных строк.
    """
    service = ScanService(use_cache=False)
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    trash = TrashService(service)

    result = trash.delete_many(job.id, [
        os.path.join(sample_tree, "a.txt"),
        os.path.join(sample_tree, "нет-такого"),
    ])

    assert [e.name for e in result.deleted] == ["a.txt"]
    assert result.already_gone == [os.path.join(sample_tree, "нет-такого")]
    assert result.failed == []


def test_repeated_deletion_reports_already_gone(sample_tree):
    service = ScanService(use_cache=False)
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    trash = TrashService(service)
    target = os.path.join(sample_tree, "docs")

    trash.delete_many(job.id, [target])
    again = trash.delete_many(job.id, [target])

    assert again.deleted == []
    assert again.already_gone == [target]
    assert again.failed == []


def test_duplicate_paths_are_collapsed(sample_tree, tmp_path):
    """Два написания одного объекта не должны попадать в пачку дважды."""
    service = ScanService(use_cache=False)
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    trash = TrashService(service)
    target = os.path.join(sample_tree, "docs")

    result = trash.delete_many(job.id, [target, target + os.sep, target])

    assert len(result.deleted) == 1
    assert result.already_gone == []


def test_failures_are_grouped_by_reason(sample_tree):
    service = ScanService(use_cache=False)
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    trash = TrashService(service)

    result = trash.delete_many(job.id, ["/usr", "/etc", os.path.join(sample_tree, "a.txt")])

    assert len(result.deleted) == 1
    groups = result.failure_groups()
    assert sum(len(paths) for _reason, paths in groups) == 2


def test_rescan_updates_only_the_folder_and_its_parents(sample_tree):
    """Обход подпапки не должен трогать остальное дерево."""
    service = ScanService(use_cache=False)
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))
    project = service.find_node(job.id, os.path.join(sample_tree, "project"))
    docs_before = service.find_node(job.id, os.path.join(sample_tree, "docs")).size

    # Кладём в project ещё файл мимо инструмента.
    with open(os.path.join(sample_tree, "project", "новый.bin"), "wb") as fh:
        fh.write(b"x" * 1000)

    _wait(service.rescan(job.id, os.path.join(sample_tree, "project")))

    assert project.size == 4050        # было 3050
    assert job.result.root.size == 9350  # 8350 + 1000
    assert service.find_node(job.id, os.path.join(sample_tree, "docs")).size == docs_before


def test_rescan_of_root_is_a_full_scan(sample_tree):
    service = ScanService(use_cache=False)
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))

    fresh = _wait(service.rescan(job.id, sample_tree))

    assert fresh.state == DONE
    assert fresh.from_cache is False
    assert fresh.result.root.size == 8350


def test_rescan_rejects_unknown_path(sample_tree):
    service = ScanService(use_cache=False)
    job = _wait(service.start(sample_tree, size_mode=SIZE_APPARENT))

    with pytest.raises(LookupError):
        service.rescan(job.id, os.path.join(sample_tree, "нет-такой-папки"))
