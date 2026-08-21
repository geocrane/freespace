"""Поиск по дереву в памяти: по имени, шаблону, размеру, дате и типу файлов."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field

from .model import FileNode

# --- категории типов файлов ------------------------------------------------

CATEGORIES: dict[str, frozenset[str]] = {
    "video": frozenset({
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
        ".mpg", ".mpeg", ".ts", ".m2ts", ".vob",
    }),
    "audio": frozenset({
        ".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".wma", ".aiff", ".opus",
    }),
    "image": frozenset({
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
        ".heic", ".raw", ".cr2", ".nef", ".arw", ".psd", ".svg",
    }),
    "archive": frozenset({
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst", ".tgz",
        ".tbz", ".lz4", ".jar", ".war",
    }),
    "disk_image": frozenset({
        ".iso", ".dmg", ".vmdk", ".vdi", ".qcow2", ".vhd", ".vhdx", ".img",
    }),
    "document": frozenset({
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt",
        ".ods", ".odp", ".rtf", ".txt", ".md", ".epub", ".djvu", ".csv",
    }),
    "code": frozenset({
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp",
        ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
        ".sh", ".sql", ".html", ".css", ".json", ".xml", ".yml", ".yaml",
    }),
    "installer": frozenset({
        ".exe", ".msi", ".pkg", ".deb", ".rpm", ".apk", ".appimage", ".whl",
    }),
    "database": frozenset({
        ".db", ".sqlite", ".sqlite3", ".mdb", ".dbf", ".parquet", ".orc",
    }),
}

# Человеческие названия для интерфейса.
CATEGORY_LABELS = {
    "video": "видео", "audio": "аудио", "image": "изображения",
    "archive": "архивы", "disk_image": "образы дисков", "document": "документы",
    "code": "код", "installer": "установщики", "database": "базы данных",
    "other": "прочее",
}

_EXT_TO_CATEGORY = {
    ext: name for name, exts in CATEGORIES.items() for ext in exts
}


def category_of(name: str) -> str:
    """Категория файла по расширению; ``other``, если расширение незнакомо."""
    return _EXT_TO_CATEGORY.get(os.path.splitext(name)[1].lower(), "other")


# --- фильтр ----------------------------------------------------------------

# Режимы сопоставления имени.
SUBSTRING = "substring"
GLOB = "glob"
EXACT = "exact"

# Что искать.
ANY = "all"
DIRS = "dirs"
FILES = "files"


@dataclass
class SearchFilter:
    """Условия поиска. Пустые поля ничего не ограничивают."""

    term: str = ""
    mode: str = SUBSTRING
    kind: str = ANY
    min_size: int = 0
    max_size: int | None = None
    categories: tuple[str, ...] = field(default_factory=tuple)
    # Не показывать находку, если её предок тоже подошёл. Без этого поиск
    # node_modules выдаёт заодно все вложенные node_modules — список раздут,
    # а полезна в нём только верхняя строка: удалять будут её.
    top_level_only: bool = False

    def is_empty(self) -> bool:
        """Фильтр ничего не отбирает — искать нечего."""
        return (
            not self.term and self.kind == ANY and self.min_size <= 0
            and self.max_size is None and not self.categories
        )

    def matches(self, node: FileNode) -> bool:
        if self.kind == DIRS and not node.is_dir:
            return False
        if self.kind == FILES and node.is_dir:
            return False
        if node.size < self.min_size:
            return False
        if self.max_size is not None and node.size > self.max_size:
            return False
        if self.categories:
            # Каталоги по типу файлов не отбираются — у них нет расширения.
            if node.is_dir or category_of(node.name) not in self.categories:
                return False
        if self.term:
            name_low = node.name.lower()
            term_low = self.term.lower()
            if self.mode == GLOB:
                if not fnmatch.fnmatch(name_low, term_low):
                    return False
            elif self.mode == EXACT:
                if name_low != term_low:
                    return False
            elif term_low not in name_low:
                return False
        return True


def find(root: FileNode, flt: SearchFilter, limit: int = 1000) -> list[FileNode]:
    """Узлы поддерева, подходящие под фильтр, по убыванию размера.

    Отбор идёт по всему дереву, а обрезается уже отсортированный результат:
    иначе «топ-100 самых больших» оказался бы просто «первыми ста найденными».
    """
    results = [n for n in iter_searchable(root) if flt.matches(n)]
    if flt.top_level_only:
        results = drop_nested(results)
    results.sort(key=lambda n: n.size, reverse=True)
    return results[:limit] if limit else results


def iter_searchable(root: FileNode):
    """Поддерево без самого корня и без корзины.

    Удалённое пользователь считает исчезнувшим: находить его снова, теперь по
    адресу внутри ``.freespace-trash``, — верный способ решить, что удаление не
    сработало. Содержимое корзины показывает отдельная вкладка.
    """
    stack = list(root.children or ())
    while stack:
        node = stack.pop()
        if node.is_trash:
            continue
        yield node
        if node.children:
            stack.extend(node.children)


def drop_nested(nodes: list[FileNode]) -> list[FileNode]:
    """Убрать находки, лежащие внутри других находок.

    Если целая папка подошла под условие, её подпапки подошли автоматически.
    Пользователю нужна верхняя: именно её он будет удалять.
    """
    found = set(map(id, nodes))
    result = []
    for node in nodes:
        parent = node.parent
        while parent is not None and id(parent) not in found:
            parent = parent.parent
        if parent is None:
            result.append(node)
    return result


def find_by_name(
    root: FileNode,
    term: str,
    *,
    dirs_only: bool = False,
    exact: bool = False,
    use_glob: bool = False,
) -> list[FileNode]:
    """Найти узлы по имени в поддереве ``root``.

    - ``exact``  — точное совпадение имени (без учёта регистра);
    - ``use_glob`` — сопоставление как glob-шаблон (например ``*.iso``);
    - иначе — поиск подстроки (без учёта регистра).
    """
    mode = GLOB if use_glob else (EXACT if exact else SUBSTRING)
    return find(
        root,
        SearchFilter(term=term, mode=mode, kind=DIRS if dirs_only else ANY),
        limit=0,
    )


def largest_files(root: FileNode, limit: int = 100) -> list[FileNode]:
    """Топ самых больших файлов в поддереве."""
    return find(root, SearchFilter(kind=FILES), limit=limit)


@dataclass
class NameGroup:
    """Сводка по группе узлов с одинаковым именем (например, все ``venv``)."""

    name: str
    count: int
    total_size: int
    nodes: list[FileNode]


def group_by_exact_name(nodes: list[FileNode]) -> list[NameGroup]:
    """Сгруппировать найденные узлы по точному имени с суммарным размером.

    Удобно для кейса «найти все venv»: видно, сколько таких папок и сколько
    суммарно занимают.
    """
    groups: dict[str, NameGroup] = {}
    for node in nodes:
        g = groups.get(node.name)
        if g is None:
            g = NameGroup(name=node.name, count=0, total_size=0, nodes=[])
            groups[node.name] = g
        g.count += 1
        g.total_size += node.size
        g.nodes.append(node)
    result = list(groups.values())
    result.sort(key=lambda g: g.total_size, reverse=True)
    return result
