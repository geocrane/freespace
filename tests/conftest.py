"""Общие фикстуры тестов: построение тестового дерева на диске."""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def sample_tree(tmp_path):
    """Создаёт дерево с известными размерами.

    Структура (размеры в байтах)::

        root/
          a.txt              (100)
          docs/
            b.txt            (200)
            venv/
              big.bin        (5000)
          project/
            venv/
              lib.bin        (3000)
            src.py           (50)

    Суммарно: 100 + 200 + 5000 + 3000 + 50 = 8350
    """
    root = tmp_path / "root"
    (root / "docs" / "venv").mkdir(parents=True)
    (root / "project" / "venv").mkdir(parents=True)

    def write(path, size):
        path.write_bytes(b"x" * size)

    write(root / "a.txt", 100)
    write(root / "docs" / "b.txt", 200)
    write(root / "docs" / "venv" / "big.bin", 5000)
    write(root / "project" / "venv" / "lib.bin", 3000)
    write(root / "project" / "src.py", 50)

    return str(root)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path_factory, monkeypatch):
    """Кэш каждого теста — во временном каталоге.

    Без этого тесты писали бы снимки в настоящий кэш пользователя и, что хуже,
    подхватывали бы оттуда чужие данные вместо свежего скана.
    """
    monkeypatch.setenv("FREESPACE_CACHE_DIR",
                       str(tmp_path_factory.mktemp("freespace-cache")))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Домашний каталог каждого теста — свой.

    Корзина живёт в домашнем каталоге и потому общая для всего тома. Без
    подмены тесты складывали бы удалённое в настоящую корзину пользователя и
    видели там записи друг друга.
    """
    home = tmp_path_factory.mktemp("freespace-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
