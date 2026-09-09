"""Модель дерева файловой системы в памяти.

Узлов бывает много: на домашнем каталоге — сотни тысяч, на томе — миллионы.
Поэтому ``FileNode`` написан вручную с ``__slots__`` и не хранит путь в каждом
узле: путь восстанавливается по цепочке родителей, а абсолютный путь помнит
только корень. На миллионе узлов это экономит сотни мегабайт.
"""

from __future__ import annotations

import os

# Биты поля ``flags``.
DIR = 1          # каталог
HARDLINK = 2     # повторная жёсткая ссылка: место уже посчитано по другому имени
TRASH = 4        # корзина: содержимое удалено и в поиске участвовать не должно


class FileNode:
    """Узел дерева: файл или папка.

    Для папок ``size`` — агрегированный размер всего поддерева, ``children``
    содержит вложенные узлы. Для файлов ``children`` равно ``None``.

    ``mtime`` — время последней активности (см. ``scanner._activity``): самое
    свежее из времени изменения и времени появления объекта на этом носителе.
    У папки это максимум по всем файлам внутри, а если файлов нет вовсе —
    собственное время папки. Отдельного поля под свёртку нет намеренно: лишний
    ``float`` на узел — это указатель плюс сам объект, тридцать с лишним байт,
    то есть десятки мегабайт на большом дереве. Собственное время каталога
    показывает лишь, когда в нём последний раз добавляли или убирали запись, и
    о возрасте содержимого не говорит ничего.

    ``path`` — вычисляемое свойство. Абсолютный путь передаётся при создании
    корня; у потомков он складывается из пути родителя и имени.
    """

    __slots__ = ("name", "size", "mtime", "file_count", "flags", "parent",
                 "children", "_abs")

    def __init__(
        self,
        path: str | None = None,
        name: str = "",
        size: int = 0,
        is_dir: bool = False,
        mtime: float = 0.0,
        children: list[FileNode] | None = None,
        file_count: int = 0,
        flags: int = 0,
    ) -> None:
        self.name = name or (os.path.basename(str(path).rstrip(os.sep)) if path else "")
        self.size = size
        self.mtime = mtime
        self.file_count = file_count
        self.flags = flags | (DIR if is_dir else 0)
        self.parent: FileNode | None = None
        self.children = children
        # Абсолютный путь нужен, только пока у узла нет родителя.
        self._abs: str | None = path

    # --- признаки ---------------------------------------------------------

    @property
    def is_dir(self) -> bool:
        return bool(self.flags & DIR)

    @is_dir.setter
    def is_dir(self, value: bool) -> None:
        self.flags = (self.flags | DIR) if value else (self.flags & ~DIR)

    @property
    def is_trash(self) -> bool:
        """Корзина приложения: место ещё занято, но объекты уже удалены."""
        return bool(self.flags & TRASH)

    @property
    def is_hardlink_dup(self) -> bool:
        """Файл уже посчитан под другим именем — его ``size`` равен нулю."""
        return bool(self.flags & HARDLINK)

    @property
    def path(self) -> str:
        parent = self.parent
        if parent is None:
            return self._abs if self._abs is not None else self.name
        return os.path.join(parent.path, self.name)

    # --- построение дерева -------------------------------------------------

    def attach(self, child: FileNode) -> None:
        """Привязать ребёнка, не трогая агрегаты.

        Для многопоточного обхода: складывать размеры на лету пришлось бы под
        замком, а так они считаются одним проходом ``recompute_sizes`` в конце.
        """
        if self.children is None:
            self.children = []
        self.children.append(child)
        child.parent = self
        child._abs = None

    def add_child(self, child: FileNode) -> None:
        """Добавить дочерний узел и обновить агрегаты."""
        self.attach(child)
        self.size += child.size
        self.file_count += child.file_count if child.is_dir else 1

    def detach(self) -> None:
        """Убрать узел из родителя, вычтя его вклад по всей цепочке вверх.

        Нужно после удаления файла: картинка обновляется без пересканирования.

        Свёрнутое ``mtime`` предков при этом не пересчитывается: у папки может
        остаться время только что удалённого файла. Ошибка идёт в безопасную
        сторону — папка выглядит свежее, чем есть, и в кандидаты на удаление по
        возрасту не попадает. Ради обратного случая пришлось бы обходить всё
        поддерево заново на каждое удаление.
        """
        parent = self.parent
        if parent is None:
            return
        if parent.children:
            try:
                parent.children.remove(self)
            except ValueError:
                pass
        size = self.size
        count = self.file_count if self.is_dir else 1
        node: FileNode | None = parent
        while node is not None:
            node.size -= size
            node.file_count -= count
            node = node.parent
        self.parent = None

    # --- обход -------------------------------------------------------------

    def sorted_children(self) -> list[FileNode]:
        """Дети, отсортированные по убыванию размера."""
        if not self.children:
            return []
        return sorted(self.children, key=lambda n: n.size, reverse=True)

    def iter_subtree(self):
        """Обход поддерева в глубину, включая сам узел.

        Итеративный: на глубоком дереве цепочка ``yield from`` упирается в лимит
        рекурсии, а на большом — заметно тормозит.
        """
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            if node.children:
                stack.extend(node.children)

    def __repr__(self) -> str:
        kind = "dir" if self.is_dir else "file"
        return f"<FileNode {kind} {self.name!r} size={self.size}>"


def recompute_sizes(root: FileNode) -> None:
    """Пересчитать ``size``, ``file_count`` и ``mtime`` снизу вверх по поддереву.

    Итеративный post-order: рекурсия на дереве в несколько десятков уровней
    (а такие бывают в node_modules) упирается в лимит стека.

    Свёртка времени идёт этим же проходом и потому не стоит ничего: у папки
    ``mtime`` становится самым свежим временем активности среди файлов внутри.
    Именно это отвечает на вопрос «в этой папке хоть что-нибудь трогали за
    последний год»: одного свежего файла достаточно, чтобы вся папка перестала
    считаться залежавшейся.
    """
    order: list[FileNode] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.children:
            order.append(node)
            stack.extend(node.children)

    for node in reversed(order):
        size = 0
        count = 0
        newest = 0.0
        for child in node.children or ():
            size += child.size
            if child.is_dir:
                count += child.file_count
                # У папки без файлов внутри mtime — её собственный, и о
                # возрасте содержимого он не говорит ничего: в максимум его
                # брать нельзя, иначе пустой каталог, созданный сегодня,
                # объявит свежим всё дерево над собой.
                if child.file_count:
                    newest = max(newest, child.mtime)
            else:
                count += 1
                newest = max(newest, child.mtime)
        node.size = size
        node.file_count = count
        # Файлов в поддереве нет — говорить об их возрасте нечего, остаётся
        # собственное время каталога.
        node.mtime = newest or node.mtime
