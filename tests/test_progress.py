"""Ход долгих операций: реестр, доклад по объектам и опрос по HTTP."""

from __future__ import annotations

import os
import time

import pytest

from freespace.core.trash import Trash, tree_size
from freespace.service.progress import (
    DONE,
    ERROR,
    RUNNING,
    SILENT,
    Operations,
    Progress,
    track,
)
from freespace.service.scan_service import ScanService
from freespace.service.trash_service import TrashService


class Recorder(Progress):
    """Запоминает всё, о чём доложили, — чтобы проверить порядок."""

    def __init__(self) -> None:
        self.planned: tuple[int, int] | None = None
        self.steps: list[str] = []
        self.advances: list[int] = []
        self.counted: list[int] = []

    def plan(self, total, total_bytes=0):
        self.planned = (total, total_bytes)

    def step(self, path):
        self.steps.append(path)

    def count(self, files):
        self.counted.append(files)

    def advance(self, freed=0):
        self.advances.append(freed)


def _scanned(path):
    """Готовое дерево: удалять можно только по завершённому обходу."""
    service = ScanService(use_cache=False)
    job = service.start(path, from_cache=False)
    deadline = time.time() + 10
    while job.state == RUNNING and time.time() < deadline:
        time.sleep(0.01)
    assert job.state != RUNNING, "обход не закончился"
    return service, job


def test_unknown_token_is_not_an_error():
    """Опрос обгоняет запрос — это нормальный ход, а не сбой."""
    assert Operations().snapshot("нет-такого")["state"] == "unknown"


def test_reporter_counts_objects_and_bytes():
    ops = Operations()
    progress = ops.open("tok", "delete")
    progress.plan(3)
    progress.step("/a")
    progress.count(1200)
    assert ops.snapshot("tok")["counted"] == 1200

    progress.advance(500)
    snapshot = ops.snapshot("tok")
    assert (snapshot["done"], snapshot["freed"], snapshot["counted"]) == (1, 500, 0)
    assert snapshot["state"] == RUNNING


def test_track_closes_operation_even_on_failure():
    """Незакрытая операция — это полоса, которая крутится вечно."""
    ops = Operations()
    with track(ops, "ok", "delete"):
        pass
    assert ops.snapshot("ok")["state"] == DONE

    with pytest.raises(ValueError):
        with track(ops, "bad", "delete"):
            raise ValueError("так вышло")
    failed = ops.snapshot("bad")
    assert failed["state"] == ERROR and failed["error"] == "так вышло"


def test_empty_token_gives_silent_progress():
    """Без токена никто не смотрит — операция не должна ничего заводить."""
    ops = Operations()
    assert ops.open("", "delete") is SILENT
    assert ops.snapshot("")["state"] == "unknown"


def test_old_finished_operations_are_forgotten():
    ops = Operations(keep=3)
    for index in range(6):
        with track(ops, f"t{index}", "delete"):
            pass
    assert ops.snapshot("t0")["state"] == "unknown"
    assert ops.snapshot("t5")["state"] == DONE


def test_tree_size_reports_files_it_walked(tmp_path):
    """Единственный признак жизни на папке из сотни тысяч мелких файлов."""
    for index in range(25):
        (tmp_path / f"f{index}.bin").write_bytes(b"x" * 10)

    seen: list[int] = []
    total = tree_size(str(tmp_path), on_progress=seen.append, every=1)
    assert total == 250
    assert seen and seen[-1] == 25


def test_delete_many_advances_on_every_path(sample_tree):
    """Полоса должна доходить до конца и на отказах, и на пропусках."""
    service, job = _scanned(sample_tree)
    trash = TrashService(service)
    recorder = Recorder()
    paths = [
        os.path.join(sample_tree, "docs"),
        os.path.join(sample_tree, "docs", "venv"),   # уедет вместе с docs
        os.path.join(sample_tree, "нет-такого"),     # уже отсутствует
    ]
    result = trash.delete_many(job.id, paths, recorder)

    assert recorder.planned == (3, 0)
    assert len(recorder.steps) == 3
    assert len(recorder.advances) == 3, "полоса обязана дойти до 3 из 3"
    assert len(result.deleted) == 1 and result.inside_deleted and result.already_gone


def test_empty_reports_each_entry_with_its_size(sample_tree):
    service, job = _scanned(sample_tree)
    trash = TrashService(service)
    trash.delete(job.id, os.path.join(sample_tree, "a.txt"))
    trash.delete(job.id, os.path.join(sample_tree, "project"))

    recorder = Recorder()
    count, freed = trash.empty(job.id, recorder)

    assert count == 2
    # Объём работы известен заранее: размеры лежат в meta.json.
    assert recorder.planned == (2, freed)
    assert sum(recorder.advances) == freed
    assert len(recorder.steps) == 2


def test_move_to_trash_accepts_progress_callback(tmp_path):
    root = tmp_path / "root"
    (root / "big").mkdir(parents=True)
    for index in range(10):
        (root / "big" / f"f{index}.bin").write_bytes(b"x" * 100)

    seen: list[int] = []
    entry = Trash(str(root)).move_to_trash(str(root / "big"), on_size=seen.append)
    assert entry.size == 1000
    assert not os.path.exists(str(root / "big"))
