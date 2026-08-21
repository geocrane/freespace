"""Тесты поиска по дереву в памяти."""

from __future__ import annotations

import os
import time

import pytest

from freespace.core.scanner import SIZE_APPARENT, scan
from freespace.core.search import (
    DIRS,
    EXACT,
    FILES,
    GLOB,
    SearchFilter,
    category_of,
    find,
    find_by_name,
    group_by_exact_name,
    largest_files,
)


def test_find_all_venv(sample_tree):
    root = scan(sample_tree, size_mode=SIZE_APPARENT).root
    venvs = find_by_name(root, "venv", dirs_only=True)
    assert len(venvs) == 2
    # Отсортированы по убыванию размера
    assert venvs[0].size >= venvs[1].size


def test_group_by_exact_name(sample_tree):
    root = scan(sample_tree, size_mode=SIZE_APPARENT).root
    venvs = find_by_name(root, "venv", dirs_only=True, exact=True)
    groups = group_by_exact_name(venvs)
    assert len(groups) == 1
    assert groups[0].name == "venv"
    assert groups[0].count == 2
    assert groups[0].total_size == 8000  # 5000 + 3000


def test_glob_search(sample_tree):
    root = scan(sample_tree, size_mode=SIZE_APPARENT).root
    bins = find_by_name(root, "*.bin", use_glob=True)
    assert {n.name for n in bins} == {"big.bin", "lib.bin"}


def test_largest_files(sample_tree):
    root = scan(sample_tree, size_mode=SIZE_APPARENT).root
    top = largest_files(root, limit=3)
    assert [n.size for n in top] == [5000, 3000, 200]


# --- фильтры ---------------------------------------------------------------


def _root(sample_tree):
    return scan(sample_tree, size_mode=SIZE_APPARENT).root


def test_filter_by_min_size(sample_tree):
    found = find(_root(sample_tree), SearchFilter(kind=FILES, min_size=1000))
    assert {n.name for n in found} == {"big.bin", "lib.bin"}


def test_filter_by_max_size(sample_tree):
    found = find(_root(sample_tree), SearchFilter(kind=FILES, max_size=200))
    assert {n.name for n in found} == {"a.txt", "b.txt", "src.py"}


def test_filter_by_category(sample_tree):
    found = find(_root(sample_tree), SearchFilter(categories=("code",)))
    assert {n.name for n in found} == {"src.py"}


def test_categories_never_match_directories(sample_tree):
    """У папки нет расширения — фильтр по типу не должен её случайно поймать."""
    found = find(_root(sample_tree), SearchFilter(categories=("document",)))
    assert all(not n.is_dir for n in found)


def test_filters_combine(sample_tree):
    found = find(
        _root(sample_tree),
        SearchFilter(term="*.bin", mode=GLOB, kind=FILES, min_size=4000),
    )
    assert [n.name for n in found] == ["big.bin"]


def test_limit_returns_biggest_not_first_found(sample_tree):
    """Обрезаем уже отсортированный список, иначе «топ» был бы случайным."""
    found = find(_root(sample_tree), SearchFilter(kind=FILES), limit=2)
    assert [n.size for n in found] == [5000, 3000]


def test_empty_filter_is_recognised():
    assert SearchFilter().is_empty()
    assert not SearchFilter(term="venv").is_empty()
    assert not SearchFilter(min_size=1).is_empty()


def test_category_of_known_and_unknown():
    assert category_of("фильм.MKV") == "video"
    assert category_of("архив.tar.gz") == "archive"
    assert category_of("без-расширения") == "other"


# --- вложенные находки ----------------------------------------------------


@pytest.fixture
def nested_modules(tmp_path):
    """node_modules внутри node_modules — обычное дело в проектах на JS."""
    root = tmp_path / "root"
    outer = root / "проект" / "node_modules"
    inner = outer / "пакет" / "node_modules"
    inner.mkdir(parents=True)
    (outer / "big.bin").write_bytes(b"x" * 5000)
    (inner / "small.bin").write_bytes(b"x" * 700)
    return str(root)


def test_nested_matches_are_listed_by_default(nested_modules):
    root = scan(nested_modules, size_mode=SIZE_APPARENT).root
    found = find(root, SearchFilter(term="node_modules", mode=EXACT, kind=DIRS))

    assert len(found) == 2


def test_top_level_only_keeps_the_outermost(nested_modules):
    """Удалять будут внешнюю папку — вложенная в списке только мешает."""
    root = scan(nested_modules, size_mode=SIZE_APPARENT).root
    found = find(root, SearchFilter(term="node_modules", mode=EXACT, kind=DIRS,
                                    top_level_only=True))

    assert len(found) == 1
    assert found[0].size == 5700, "во внешней находке уже учтена вложенная"


def test_top_level_only_keeps_unrelated_matches(nested_modules, tmp_path):
    """Отсекаются только потомки находок, а не всё подряд."""
    other = tmp_path / "root" / "другой" / "node_modules"
    other.mkdir(parents=True)
    (other / "f.bin").write_bytes(b"x" * 100)

    root = scan(nested_modules, size_mode=SIZE_APPARENT).root
    found = find(root, SearchFilter(term="node_modules", mode=EXACT, kind=DIRS,
                                    top_level_only=True))

    assert {n.parent.name for n in found} == {"проект", "другой"}


def test_venv_glob_matches_dotted_variant(tmp_path):
    root = tmp_path / "root"
    for name in ["venv", ".venv", "myvenv", "venv-tools", "environment"]:
        (root / name).mkdir(parents=True)
        (root / name / "f.bin").write_bytes(b"x" * 10)

    tree = scan(str(root), size_mode=SIZE_APPARENT).root
    found = find(tree, SearchFilter(term="*venv", mode=GLOB, kind=DIRS))

    assert {n.name for n in found} == {"venv", ".venv", "myvenv"}


# --- корзина не участвует в поиске ------------------------------------------


def test_trash_contents_are_not_searchable(tmp_path):
    """Удалённое пользователь считает исчезнувшим.

    Если найти его снова — теперь по адресу внутри .freespace-trash, — решишь,
    что удаление не сработало.
    """
    root = tmp_path / "root"
    (root / ".freespace-trash" / "запись" / "__pycache__").mkdir(parents=True)
    (root / ".freespace-trash" / "запись" / "__pycache__" / "m.pyc").write_bytes(b"x" * 5000)
    (root / "живой" / "__pycache__").mkdir(parents=True)
    (root / "живой" / "__pycache__" / "m.pyc").write_bytes(b"x" * 300)

    tree = scan(str(root), size_mode=SIZE_APPARENT).root
    found = find(tree, SearchFilter(term="__pycache__", mode=EXACT, kind=DIRS))

    assert [n.path for n in found] == [str(root / "живой" / "__pycache__")]


def test_trash_size_still_counts(tmp_path):
    """Место корзиной занято, и это должно быть видно в общей цифре."""
    root = tmp_path / "root"
    (root / ".freespace-trash" / "запись").mkdir(parents=True)
    (root / ".freespace-trash" / "запись" / "big.bin").write_bytes(b"x" * 5000)
    (root / "main.bin").write_bytes(b"x" * 1000)

    result = scan(str(root), size_mode=SIZE_APPARENT)
    trash = next(c for c in result.root.children if c.is_trash)

    assert result.root.size == 6000
    assert trash.size == 5000
    assert not trash.children, "узлы внутри корзины не нужны и не строятся"
