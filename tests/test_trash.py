"""Тесты корзины: перенос, возврат, очистка и защита системных путей."""

from __future__ import annotations

import os

import pytest

from freespace.core.trash import (
    TRASH_DIR_NAME,
    canonical,
    ProtectedPathError,
    Trash,
    TrashError,
    ensure_deletable,
)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_bytes(b"x" * 500)
    (root / "big.bin").write_bytes(b"x" * 1000)
    return str(root)


def test_move_and_restore(workspace):
    trash = Trash(workspace)
    target = os.path.join(workspace, "sub")

    entry = trash.move_to_trash(target)
    assert not os.path.exists(target), "объект должен исчезнуть с исходного места"
    assert entry.size == 500
    assert entry.is_dir

    trash.restore(entry.id)
    assert os.path.exists(os.path.join(workspace, "sub", "a.txt"))
    assert trash.list_entries() == []


def test_trash_lives_in_home(workspace):
    """Корзина в домашнем каталоге: туда доступ есть всегда.

    Заодно она оказывается снаружи просканированной папки, и удалённое не может
    попасть в результаты поиска в принципе.
    """
    trash = Trash(workspace)
    trash.move_to_trash(os.path.join(workspace, "big.bin"))

    assert trash.dir_path == os.path.join(canonical("~"), TRASH_DIR_NAME)
    assert trash.available


def test_restore_refuses_when_place_is_taken(workspace):
    trash = Trash(workspace)
    entry = trash.move_to_trash(os.path.join(workspace, "big.bin"))
    # Кто-то создал файл с тем же именем, пока объект лежал в корзине.
    open(os.path.join(workspace, "big.bin"), "wb").close()

    with pytest.raises(TrashError):
        trash.restore(entry.id)


def test_empty_removes_everything(workspace):
    trash = Trash(workspace)
    trash.move_to_trash(os.path.join(workspace, "big.bin"))
    trash.move_to_trash(os.path.join(workspace, "sub"))

    count, freed = trash.empty()
    assert count == 2
    assert freed == 1500
    assert trash.list_entries() == []


def test_delete_entry_is_permanent(workspace):
    trash = Trash(workspace)
    entry = trash.move_to_trash(os.path.join(workspace, "big.bin"))

    trash.delete_entry(entry.id)
    assert trash.list_entries() == []
    with pytest.raises(TrashError):
        trash.restore(entry.id)


def test_system_paths_are_protected(workspace):
    for guarded in ("/", "/usr", "/etc", os.path.expanduser("~")):
        with pytest.raises(ProtectedPathError):
            ensure_deletable(guarded, "/")


def test_scan_root_itself_is_protected(workspace):
    with pytest.raises(ProtectedPathError):
        ensure_deletable(workspace, workspace)


def test_path_outside_scan_root_is_refused(workspace, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_bytes(b"x")

    with pytest.raises(ProtectedPathError):
        ensure_deletable(str(outside), workspace)


def test_parent_of_protected_path_is_refused(tmp_path):
    """Удаление родителя защищённого пути равносильно удалению самого пути."""
    with pytest.raises(ProtectedPathError):
        ensure_deletable("/usr", "/")


def test_trash_itself_cannot_be_deleted(workspace):
    trash = Trash(workspace)
    trash.move_to_trash(os.path.join(workspace, "big.bin"))

    with pytest.raises(ProtectedPathError):
        ensure_deletable(trash.dir_path, workspace)


def test_missing_path_reports_clearly(workspace):
    with pytest.raises(FileNotFoundError):
        ensure_deletable(os.path.join(workspace, "нет-такого"), workspace)


def test_fallback_dir_is_used_when_root_is_read_only(workspace, tmp_path):
    """Корень только на чтение — корзина уезжает в кэш, перенос идёт копированием."""
    fallback = tmp_path / "fallback-trash"
    trash = Trash(workspace, dir_path=str(fallback))

    entry = trash.move_to_trash(os.path.join(workspace, "big.bin"))
    assert os.path.exists(entry.trashed_path)
    assert str(fallback) in entry.trashed_path
