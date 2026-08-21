"""Тесты узла дерева: пути, флаги, пересчёт агрегатов, отсоединение."""

from __future__ import annotations

import os

from freespace.core.model import FileNode, recompute_sizes


def _tree():
    """root/ (файл a) + sub/ (файл b)."""
    root = FileNode(path=os.path.join(os.sep, "tmp", "root"), is_dir=True)
    sub = FileNode(name="sub", is_dir=True)
    root.attach(sub)
    root.attach(FileNode(name="a.txt", size=100))
    sub.attach(FileNode(name="b.txt", size=300))
    recompute_sizes(root)
    return root, sub


def test_path_is_built_from_parents():
    """Путь не хранится в узле — он собирается по цепочке родителей."""
    root, sub = _tree()
    assert root.path == os.path.join(os.sep, "tmp", "root")
    assert sub.path == os.path.join(os.sep, "tmp", "root", "sub")
    assert sub.children[0].path == os.path.join(os.sep, "tmp", "root", "sub", "b.txt")


def test_root_name_falls_back_to_basename():
    node = FileNode(path=os.path.join(os.sep, "tmp", "root"), is_dir=True)
    assert node.name == "root"


def test_recompute_sizes_aggregates_bottom_up():
    root, sub = _tree()
    assert sub.size == 300
    assert root.size == 400
    assert root.file_count == 2
    assert sub.file_count == 1


def test_recompute_survives_deep_tree():
    """Рекурсивный пересчёт упёрся бы в лимит стека — обход должен быть плоским."""
    root = FileNode(path=os.sep + "deep", is_dir=True)
    node = root
    for i in range(3000):
        child = FileNode(name=f"d{i}", is_dir=True)
        node.attach(child)
        node = child
    node.attach(FileNode(name="leaf.bin", size=7))

    recompute_sizes(root)
    assert root.size == 7
    assert root.file_count == 1


def test_detach_subtracts_along_whole_chain():
    """Удаление узла обновляет размеры всех предков, а не только родителя."""
    root, sub = _tree()
    sub.children[0].detach()

    assert sub.size == 0
    assert root.size == 100
    assert root.file_count == 1
    assert sub.children == []


def test_is_dir_survives_flag_round_trip():
    node = FileNode(name="x")
    assert node.is_dir is False
    node.is_dir = True
    assert node.is_dir is True
    node.is_dir = False
    assert node.is_dir is False


def test_iter_subtree_covers_everything():
    root, _sub = _tree()
    names = {n.name for n in root.iter_subtree()}
    assert names == {"root", "sub", "a.txt", "b.txt"}


def test_node_has_no_dict():
    """__slots__ на месте: без него миллион узлов стоит лишних сотен мегабайт."""
    assert not hasattr(FileNode(name="x"), "__dict__")
