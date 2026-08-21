"""Подготовка дерева к показу: плитки treemap, строки таблицы, хлебные крошки.

Здесь данные приводятся к виду, готовому для отрисовки, но без привязки к
конкретному интерфейсу: наружу отдаются обычные словари и числа.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..core.model import FileNode
from ..core.platform_utils import human_size
from ..core.search import NameGroup, category_of
from ..core.treemap import Rect, color_for, squarify


@dataclass
class Tile:
    """Плитка treemap."""

    name: str
    path: str
    size: int
    size_human: str
    is_dir: bool
    drillable: bool
    percent: float
    color: str
    x: float
    y: float
    w: float
    h: float
    # Для сгруппированного «хвоста»: сколько объектов он собрал.
    grouped: int = 0
    trash: bool = False


def is_drillable(node: FileNode) -> bool:
    """Можно ли провалиться внутрь узла."""
    return bool(node.is_dir and node.children)


def layout_tiles(
    node: FileNode,
    width: float,
    height: float,
    max_tiles: int = 120,
    min_side: float = 3.0,
) -> list[Tile]:
    """Разложить детей узла в плитки внутри прямоугольника width × height.

    Мелочь сверх ``max_tiles`` собирается в одну плитку «остальное»: иначе на
    каталоге с тысячами детей рисуются тысячи элементов, которые всё равно не
    видно.
    """
    children = node.sorted_children()
    if not children or width <= 0 or height <= 0:
        return []

    visible = [c for c in children if c.size > 0]
    if not visible:
        return []

    head = visible[:max_tiles]
    tail = visible[max_tiles:]

    values = [float(c.size) for c in head]
    tail_size = sum(c.size for c in tail)
    if tail_size > 0:
        values.append(float(tail_size))

    rects = squarify(values, Rect(0.0, 0.0, float(width), float(height)))
    total = node.size or 1

    tiles: list[Tile] = []
    for index, (child, rect) in enumerate(zip(head, rects)):
        if rect.w < min_side or rect.h < min_side:
            continue
        tiles.append(
            Tile(
                name=child.name,
                path=child.path,
                size=child.size,
                size_human=human_size(child.size),
                is_dir=child.is_dir,
                drillable=is_drillable(child),
                percent=child.size / total * 100,
                color=color_for(index),
                x=rect.x, y=rect.y, w=rect.w, h=rect.h,
                trash=child.is_trash,
            )
        )

    if tail_size > 0:
        rect = rects[-1]
        if rect.w >= min_side and rect.h >= min_side:
            tiles.append(
                Tile(
                    name=f"ещё {len(tail)} мелких",
                    path="",
                    size=tail_size,
                    size_human=human_size(tail_size),
                    is_dir=False,
                    drillable=False,
                    percent=tail_size / total * 100,
                    color="#8A8A8A",
                    x=rect.x, y=rect.y, w=rect.w, h=rect.h,
                    grouped=len(tail),
                )
            )
    return tiles


@dataclass
class Row:
    """Строка таблицы детей или результатов поиска."""

    name: str
    path: str
    size: int
    size_human: str
    is_dir: bool
    drillable: bool
    percent: float
    file_count: int
    mtime: float
    # Каталог, в котором лежит объект: в списке детей не нужен, в результатах
    # поиска — единственное, что отличает десять одинаковых venv друг от друга.
    parent_path: str = ""
    category: str = ""
    hardlink: bool = False
    trash: bool = False


def _row(node: FileNode, total: int, with_parent: bool = False) -> Row:
    return Row(
        name=node.name,
        path=node.path,
        size=node.size,
        size_human=human_size(node.size),
        is_dir=node.is_dir,
        drillable=is_drillable(node),
        percent=node.size / total * 100 if total else 0.0,
        file_count=node.file_count,
        mtime=node.mtime,
        parent_path=os.path.dirname(node.path) if with_parent else "",
        category="" if node.is_dir else category_of(node.name),
        hardlink=node.is_hardlink_dup,
        trash=node.is_trash,
    )


def children_rows(node: FileNode, limit: int = 500) -> list[Row]:
    """Дети узла, по убыванию размера."""
    total = node.size or 1
    return [_row(child, total) for child in node.sorted_children()[:limit]]


def search_rows(nodes: list[FileNode], root: FileNode) -> list[Row]:
    """Результаты поиска: доля считается от корня скана, а не от родителя."""
    total = root.size or 1
    return [_row(node, total, with_parent=True) for node in nodes]


@dataclass
class GroupRow:
    """Сводка по группе одноимённых узлов — кейс «все venv разом».

    ``items`` — сами найденные объекты, по убыванию размера. Без них группа
    остаётся справкой, с которой ничего нельзя сделать: увидеть, что venv
    занимают 2.6 ГБ, полезно только если тут же можно посмотреть, какие именно
    это папки, и убрать ненужные.
    """

    name: str
    count: int
    total_size: int
    total_size_human: str
    percent: float
    items: list[Row]


def group_rows(groups: list[NameGroup], root: FileNode, limit: int = 200) -> list[GroupRow]:
    """Группы по имени с суммарным размером, по убыванию суммы."""
    total = root.size or 1
    return [
        GroupRow(
            name=group.name,
            count=group.count,
            total_size=group.total_size,
            total_size_human=human_size(group.total_size),
            percent=group.total_size / total * 100,
            items=[
                _row(node, total, with_parent=True)
                for node in sorted(group.nodes, key=lambda n: n.size, reverse=True)[:limit]
            ],
        )
        for group in groups
    ]


def breadcrumbs(root: FileNode, node: FileNode) -> list[dict]:
    """Цепочка от корня скана до узла, для навигации вверх."""
    chain = [{"name": root.name or root.path, "path": root.path}]
    if node.path == root.path:
        return chain

    try:
        relative = os.path.relpath(node.path, root.path)
    except ValueError:
        return chain
    if relative.startswith(".."):
        return chain

    current = root.path
    for part in relative.split(os.sep):
        current = os.path.join(current, part)
        chain.append({"name": part, "path": current})
    return chain
