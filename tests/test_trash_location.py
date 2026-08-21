"""Тесты правила размещения корзины.

Правило одно и без исключений: корень скана, если в него можно писать, иначе
домашний каталог, если он внутри корня, иначе честный отказ. Прежняя версия при
недоступном корне тихо уводила корзину в служебный каталог приложения — сканер
её не прятал, и удалённое возвращалось в поиск.
"""

from __future__ import annotations

import os

import pytest

from freespace.core.trash import (
    TRASH_DIR_NAME,
    Trash,
    TrashUnavailable,
    canonical,
    trash_dir_for,
)


def test_home_is_preferred(tmp_path, monkeypatch):
    """Корзина ложится в домашний каталог: туда доступ есть всегда.

    В корень диска писать обычно нельзя — на Windows попытка создать
    C:\\.freespace-trash заканчивается «Отказано в доступе».
    """
    root = tmp_path / "root"
    root.mkdir()
    home = tmp_path / "дом"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert trash_dir_for(str(root)) == os.path.join(canonical(home), TRASH_DIR_NAME)


def test_home_on_another_volume_is_skipped(tmp_path, monkeypatch):
    """Между томами перенос стал бы копированием — тогда лучше корень скана."""
    root = tmp_path / "root"
    root.mkdir()
    home = tmp_path / "дом"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    real_stat = os.stat

    def other_volume(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if str(path).startswith(str(home)):
            return type("S", (), {**{k: getattr(info, k) for k in dir(info)
                                     if k.startswith("st_")},
                                  "st_dev": info.st_dev + 1})()
        return info

    monkeypatch.setattr(os, "stat", other_volume)
    assert trash_dir_for(str(root)) == os.path.join(canonical(root), TRASH_DIR_NAME)


def test_nowhere_to_write_is_refused(tmp_path, monkeypatch):
    """Ни дом, ни корень не подходят — отказ, а не тихий обходной путь."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "нет-такого-дома"))
    monkeypatch.setattr(os, "mkdir", _always_denied)

    with pytest.raises(TrashUnavailable):
        trash_dir_for(str(root))
    assert Trash(str(root)).available is False


def test_move_is_always_a_rename(tmp_path):
    """Корзина в предке объекта, значит том тот же и копирования не будет."""
    root = tmp_path / "root"
    (root / "big").mkdir(parents=True)
    (root / "big" / "data.bin").write_bytes(b"x" * 4096)

    trash = Trash(str(root))
    before = os.stat(root / "big").st_ino
    entry = trash.move_to_trash(str(root / "big"))

    # Тот же inode на новом месте — объект переименован, а не пересоздан.
    assert os.stat(entry.trashed_path).st_ino == before
    assert not os.path.exists(root / "big")


def test_original_path_is_canonical(tmp_path):
    """Возврат должен класть объект туда, где он был на самом деле."""
    root = tmp_path / "root"
    (root / "папка").mkdir(parents=True)
    (root / "папка" / "f.bin").write_bytes(b"x" * 10)
    link = tmp_path / "ссылка"
    os.symlink(root, link)

    trash = Trash(str(root))
    entry = trash.move_to_trash(os.path.join(str(link), "папка"))

    assert entry.original_path == os.path.join(canonical(root), "папка")
    trash.restore(entry.id)
    assert (root / "папка" / "f.bin").exists()


def test_writability_is_checked_by_creating_not_by_asking(tmp_path, monkeypatch):  # noqa: D401
    """Права проверяются делом.

    На Windows ``os.access(path, os.W_OK)`` смотрит только атрибут «только
    чтение» и для корня диска отвечает «можно», хотя обычному пользователю туда
    писать нельзя. Корзина пыталась появиться в ``C:\\`` и падала с «Отказано в
    доступе» уже в момент удаления — то есть после того, как пользователь нажал
    кнопку.
    """
    root = tmp_path / "root"
    home = root / "Users" / "имя"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    forbidden = os.path.join(canonical(root), TRASH_DIR_NAME)
    real_mkdir = os.mkdir

    def fake_mkdir(path, *args, **kwargs):
        # Каталог создаётся, но записать в него ничего нельзя — так выглядит
        # корень диска на Windows: makedirs проходит, а проба падает.
        if str(path).startswith(forbidden):
            raise PermissionError(13, "Отказано в доступе")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", fake_mkdir)
    # os.access говорит «можно» — и ошибается.
    monkeypatch.setattr(os, "access", lambda *a, **k: True)

    assert trash_dir_for(str(root)) == os.path.join(canonical(home), TRASH_DIR_NAME)


def test_refusal_names_every_place_it_tried(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "снаружи"))
    monkeypatch.setattr(os, "mkdir", _always_denied)

    with pytest.raises(TrashUnavailable) as exc:
        trash_dir_for(str(root))
    assert TRASH_DIR_NAME in str(exc.value)


def _always_denied(*args, **kwargs):
    raise PermissionError(13, "Отказано в доступе")
