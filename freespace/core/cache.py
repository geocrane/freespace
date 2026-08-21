"""Кэш снапшотов сканирования: по одному gzip-файлу на корень.

Почему не база. Кэш живёт рядом с домашним каталогом, а он в контейнере обычно
на NFS. SQLite там упирается в блокировки ``fcntl``: «database is locked»,
подвисания на минуты, иногда битый файл. Здесь блокировок нет вовсе: снимок
пишется во временный файл в том же каталоге и переезжает на место одним
``os.replace`` — атомарной операцией на любой файловой системе. Читатель либо
видит старый файл целиком, либо новый целиком.

Формат — заголовок-JSON первой строкой, дальше по строке на узел в порядке
обхода в глубину::

    {"v":1,"root":"/Users/me","created":1755123456,"total":812734,"files":1240}
    0	d	812734	1755123456	1240	/Users/me
    1	d	40213	1755123400	821	Dev
    2	f	1024	1755123000	0	README.md

Путь в строках не хранится — он восстанавливается из глубины и имени, поэтому
файл в разы компактнее таблицы, где путь лежал в каждой записи.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass

from .model import FileNode
from .platform_utils import cache_dir

SUFFIX = ".fsnap.gz"
FORMAT_VERSION = 1

# Потолок на каталог кэша и предельный возраст снимка. При превышении
# вытесняются самые старые — кэш не должен сам становиться проблемой с местом.
DEFAULT_LIMIT_BYTES = 100 * 1024 * 1024
# Сутки: за день дерево успевает разойтись с диском настолько, что показывать
# вчерашние цифры вреднее, чем подождать нового обхода.
DEFAULT_MAX_AGE_DAYS = 1

_ESCAPE = str.maketrans({"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"})
_UNESCAPE = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}


@dataclass
class SnapshotInfo:
    """Что известно о снимке, не читая его целиком."""

    id: int
    root_path: str
    created_at: float
    total_size: int
    file_count: int
    file_path: str = ""
    bytes_on_disk: int = 0
    size_mode: str = "disk"


def _escape(name: str) -> str:
    if "\\" in name or "\t" in name or "\n" in name or "\r" in name:
        return name.translate(_ESCAPE)
    return name


def _unescape(name: str) -> str:
    if "\\" not in name:
        return name
    out: list[str] = []
    i = 0
    length = len(name)
    while i < length:
        ch = name[i]
        if ch == "\\" and i + 1 < length:
            out.append(_UNESCAPE.get(name[i + 1], name[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class Cache:
    """Хранилище снимков дерева в каталоге с gzip-файлами."""

    def __init__(
        self,
        dir_path: str | None = None,
        limit_bytes: int = DEFAULT_LIMIT_BYTES,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    ) -> None:
        self.dir_path = dir_path or cache_dir()
        os.makedirs(self.dir_path, exist_ok=True)
        self.limit_bytes = limit_bytes
        self.max_age_days = max_age_days

    def close(self) -> None:
        """Ничего не держим открытым — метод оставлен для совместимости."""

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- адресация ---------------------------------------------------------

    def _file_for(self, root_path: str) -> str:
        digest = hashlib.sha1(os.path.abspath(root_path).encode("utf-8")).hexdigest()
        return os.path.join(self.dir_path, digest[:16] + SUFFIX)

    # --- запись ------------------------------------------------------------

    def save_snapshot(
        self, root: FileNode, size_mode: str = "disk", dedup_hardlinks: bool = True
    ) -> int:
        """Сохранить дерево как снимок корня, заменив прежний."""
        root_path = os.path.abspath(root.path)
        target = self._file_for(root_path)
        created = time.time()
        header = {
            "v": FORMAT_VERSION,
            "root": root_path,
            "created": created,
            "total": root.size,
            "files": root.file_count,
            "sizes": size_mode,
            "hardlinks": dedup_hardlinks,
        }

        fd, tmp_path = tempfile.mkstemp(dir=self.dir_path, suffix=".part")
        os.close(fd)
        try:
            with gzip.open(tmp_path, "wt", encoding="utf-8", newline="\n",
                           compresslevel=6) as fh:
                fh.write(json.dumps(header, ensure_ascii=False) + "\n")
                fh.writelines(_dump_lines(root))
            # Атомарная подмена: читатель никогда не увидит половину файла.
            os.replace(tmp_path, target)
        except BaseException:
            _silent_remove(tmp_path)
            raise

        # Свежий снимок из вытеснения исключён: иначе сохранение большого дерева
        # при малом потолке молча стирало бы само себя.
        self.enforce_limits(keep=target)
        return int(created)

    # --- чтение ------------------------------------------------------------

    def load_snapshot(self, root_path: str) -> FileNode | None:
        """Загрузить дерево снимка для ``root_path`` (или ``None``)."""
        path = self._file_for(root_path)
        if not os.path.exists(path):
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                header_line = fh.readline()
                if not header_line:
                    raise ValueError("пустой снимок")
                header = json.loads(header_line)
                if header.get("v") != FORMAT_VERSION:
                    raise ValueError(f"неизвестная версия формата: {header.get('v')}")
                return _load_lines(fh, header["root"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            # Оборванный или чужой файл — не повод падать: выбрасываем и
            # сканируем заново.
            _silent_remove(path)
            return None

    def load_snapshot_by_id(self, snapshot_id: int) -> FileNode | None:
        """Совместимость со старым API: ``id`` — это ``created_at`` снимка."""
        for info in self.list_snapshots():
            if int(info.created_at) == int(snapshot_id):
                return self.load_snapshot(info.root_path)
        return None

    def latest_snapshot(self, root_path: str) -> SnapshotInfo | None:
        """Заголовок свежего снимка. Просроченный отбрасывается вместе с файлом."""
        path = self._file_for(root_path)
        info = _read_info(path)
        if info is None:
            return None
        if time.time() - info.created_at > self.max_age_days * 86400:
            _silent_remove(path)
            return None
        return info

    def list_snapshots(self) -> list[SnapshotInfo]:
        """Все снимки, свежие первыми."""
        infos = []
        for name in os.listdir(self.dir_path):
            if not name.endswith(SUFFIX):
                continue
            info = _read_info(os.path.join(self.dir_path, name))
            if info is not None:
                infos.append(info)
        infos.sort(key=lambda i: i.created_at, reverse=True)
        return infos

    def forget(self, root_path: str) -> None:
        """Выбросить снимок этого корня. Имя файла выводится из пути, поэтому
        каталог перебирать не нужно."""
        _silent_remove(self._file_for(root_path))

    def delete_snapshot(self, snapshot_id: int | str) -> None:
        """Удалить снимок по ``created_at`` или по пути корня."""
        for info in self.list_snapshots():
            if info.root_path == snapshot_id or int(info.created_at) == _as_int(snapshot_id):
                _silent_remove(info.file_path)

    # --- обслуживание ------------------------------------------------------

    def total_bytes(self) -> int:
        """Сколько места занимает кэш."""
        total = 0
        for name in os.listdir(self.dir_path):
            if name.endswith(SUFFIX):
                try:
                    total += os.path.getsize(os.path.join(self.dir_path, name))
                except OSError:
                    pass
        return total

    def clear(self) -> int:
        """Удалить все снимки. Возвращает, сколько байт освобождено."""
        freed = 0
        for name in os.listdir(self.dir_path):
            if name.endswith(SUFFIX):
                path = os.path.join(self.dir_path, name)
                try:
                    freed += os.path.getsize(path)
                except OSError:
                    pass
                _silent_remove(path)
        return freed

    def enforce_limits(self, keep: str | None = None) -> None:
        """Выбросить просроченные и самые старые снимки сверх потолка.

        ``keep`` — файл, который нельзя трогать ни при каких условиях.
        """
        entries: list[tuple[float, int, str]] = []
        for name in os.listdir(self.dir_path):
            path = os.path.join(self.dir_path, name)
            if name.endswith(".part"):
                # Мусор от прерванной записи: живёт секунды, старше — брошенный.
                if _age_seconds(path) > 3600:
                    _silent_remove(path)
                continue
            if not name.endswith(SUFFIX):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))

        cutoff = time.time() - self.max_age_days * 86400
        alive = []
        for mtime, size, path in entries:
            if mtime < cutoff and path != keep:
                _silent_remove(path)
            else:
                alive.append((mtime, size, path))

        alive.sort()  # самые старые первыми
        total = sum(size for _, size, _ in alive)
        while total > self.limit_bytes and alive:
            mtime, size, path = alive.pop(0)
            if path == keep:
                continue
            _silent_remove(path)
            total -= size


# --- сериализация ---------------------------------------------------------


def _dump_lines(root: FileNode):
    """Строки снимка в порядке обхода в глубину."""
    stack: list[tuple[FileNode, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        yield "%d\t%s\t%d\t%d\t%d\t%s\n" % (
            depth,
            "d" if node.is_dir else "f",
            node.size,
            int(node.mtime),
            node.file_count,
            _escape(root.path if depth == 0 else node.name),
        )
        if node.children:
            # В обратном порядке, чтобы на выходе дети шли как в дереве.
            for child in reversed(node.children):
                stack.append((child, depth + 1))


def _load_lines(fh, root_path: str) -> FileNode | None:
    """Собрать дерево из строк снимка, опираясь на глубину."""
    root: FileNode | None = None
    # chain[d] — узел глубины d, к которому цепляются потомки глубины d+1.
    chain: list[FileNode] = []
    for line in fh:
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t", 5)
        if len(parts) != 6:
            raise ValueError("битая строка снимка")
        depth = int(parts[0])
        is_dir = parts[1] == "d"
        if depth == 0:
            root = FileNode(
                path=root_path,
                name=os.path.basename(root_path.rstrip(os.sep)) or root_path,
                size=int(parts[2]), is_dir=True, mtime=float(parts[3]),
                file_count=int(parts[4]), children=[],
            )
            chain = [root]
            continue
        node = FileNode(
            name=_unescape(parts[5]),
            size=int(parts[2]),
            is_dir=is_dir,
            mtime=float(parts[3]),
            file_count=int(parts[4]),
            children=[] if is_dir else None,
        )
        if root is None or depth > len(chain):
            raise ValueError("нарушен порядок глубины в снимке")
        parent = chain[depth - 1]
        parent.attach(node)
        del chain[depth:]
        chain.append(node)
    return root


def _read_info(path: str) -> SnapshotInfo | None:
    """Прочитать только заголовок снимка."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            header = json.loads(fh.readline())
        if header.get("v") != FORMAT_VERSION:
            return None
        return SnapshotInfo(
            id=int(header["created"]),
            root_path=header["root"],
            created_at=float(header["created"]),
            total_size=int(header["total"]),
            file_count=int(header["files"]),
            file_path=path,
            bytes_on_disk=os.path.getsize(path),
            size_mode=header.get("sizes", "disk"),
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _age_seconds(path: str) -> float:
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return 0.0


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
