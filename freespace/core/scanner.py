"""Быстрый обход файловой системы через os.scandir.

Обход устроен как очередь каталогов и пул воркеров: каждый воркер читает один
каталог, сразу создаёт узлы-файлы, а найденные подкаталоги кладёт обратно в
очередь. Размеры считаются одним проходом в конце (``recompute_sizes``), поэтому
во время обхода нет ни блокировок на агрегатах, ни риска взаимной блокировки от
рекурсивной постановки задач в собственный пул.
"""

from __future__ import annotations

import os
import queue
import stat as stat_module
import threading
from dataclasses import dataclass, field
from typing import Callable

from .model import HARDLINK, TRASH, FileNode, recompute_sizes
from .trash import TRASH_DIR_NAME

ProgressCallback = Callable[[int, str], None]

# Как считать размер файла.
SIZE_DISK = "disk"          # реально занятое место: st_blocks * 512
SIZE_APPARENT = "apparent"  # номинальный st_size


@dataclass
class ScanResult:
    """Результат сканирования.

    ``skipped``    — пути, которые не удалось прочитать (права, ошибки ввода-вывода);
    ``boundaries`` — каталоги, куда обход намеренно не пошёл: другая файловая
    система или служебная псевдо-ФС. Это не ошибка, а информация: такой каталог
    осмысленно сканировать отдельно, как самостоятельный корень.
    ``hardlink_saved`` — сколько байт не посчитано дважды благодаря учёту
    жёстких ссылок.
    """

    root: FileNode
    skipped: list[str] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)
    hardlink_saved: int = 0
    # Каталоги, пропущенные как повтор: тот же inode уже встречался под другим
    # путём. На macOS так выглядят firmlinks — /opt и /System/Volumes/Data/opt
    # это один и тот же каталог.
    aliases: list[str] = field(default_factory=list)
    # Найденные корзины приложения: их содержимое показывает отдельная вкладка.
    trash_dirs: list[str] = field(default_factory=list)
    size_mode: str = SIZE_DISK
    dedup_hardlinks: bool = True


class ScanCancelled(Exception):
    """Сканирование было отменено пользователем."""


# Служебные файловые системы: размеры в них выдуманы, а содержимое к занятому
# месту отношения не имеет. В контейнере внутри /proc и /sys/fs/cgroup бывают
# десятки вложенных монтирований, и обход без этого списка тонет в них.
PSEUDO_DIRS = frozenset(
    {"/proc", "/sys", "/dev", "/run", "/lost+found", "/.aquasec"}
)

# Признак точки подключения на Windows; на других системах его нет, и связанный
# с ним обход не выполняется.
_REPARSE_FLAG = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


def _long_path(path: str) -> str:
    """На Windows добавляет префикс \\\\?\\ для путей длиннее 260 символов."""
    if os.name == "nt" and len(path) >= 260 and not path.startswith("\\\\?\\"):
        if path.startswith("\\\\"):
            return "\\\\?\\UNC\\" + path[2:]
        return "\\\\?\\" + path
    return path


class Scanner:
    """Сканер дерева каталогов.

    Использование::

        scanner = Scanner(on_progress=cb)
        result = scanner.scan("C:/Users/me")
    """

    def __init__(
        self,
        on_progress: ProgressCallback | None = None,
        progress_every: int = 2000,
        max_workers: int | None = None,
        cross_filesystems: bool = False,
        size_mode: str = SIZE_DISK,
        dedup_hardlinks: bool = True,
    ) -> None:
        self.on_progress = on_progress
        self.progress_every = progress_every
        # Обход упирается в задержку ввода-вывода, а не в процессор: выигрыш
        # даёт большое число одновременно ожидающих запросов, особенно на
        # сетевых дисках.
        self.max_workers = max_workers or min(32, (os.cpu_count() or 4) * 4)
        # По умолчанию обход не покидает файловую систему корня: иначе скан
        # домашнего каталога незаметно утянет вложенный сетевой том и смешает
        # в одной цифре два разных хранилища.
        self.cross_filesystems = cross_filesystems
        self.size_mode = size_mode
        self.dedup_hardlinks = dedup_hardlinks

        self._cancel = threading.Event()
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._skipped: list[str] = []
        self._skipped_lock = threading.Lock()
        self._boundaries: list[str] = []
        self._root_dev: int | None = None
        # (st_dev, st_ino) уже посчитанных файлов с несколькими именами.
        self._seen_inodes: set[tuple[int, int]] = set()
        self._inode_lock = threading.Lock()
        self._hardlink_saved = 0
        # Каталоги, в которые уже заходили. Один и тот же каталог бывает виден
        # под несколькими путями: firmlinks на macOS, bind-mount на Linux,
        # junction на Windows. Проверка по st_dev тут не спасает — устройство у
        # них одно и то же. Без этой защиты дерево обходится по нескольку раз:
        # размеры удваиваются, а в поиске каждая папка появляется дважды.
        self._seen_dirs: set[tuple[int, int]] = set()
        self._seen_dirs_lock = threading.Lock()
        self._aliases: list[str] = []
        self._trash_dirs: list[str] = []

    def cancel(self) -> None:
        """Запросить отмену текущего сканирования."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # --- вспомогательное ---------------------------------------------------

    def _bump(self, path: str) -> None:
        with self._counter_lock:
            self._counter += 1
            count = self._counter
        if self.on_progress and count % self.progress_every == 0:
            self.on_progress(count, path)

    def _add_skipped(self, path: str) -> None:
        with self._skipped_lock:
            self._skipped.append(path)

    def _add_boundary(self, path: str) -> None:
        with self._skipped_lock:
            self._boundaries.append(path)

    def _resolve_dir_stat(self, path: str, st: os.stat_result) -> os.stat_result:
        """Уточнить сведения о каталоге там, где ``scandir`` их не дал.

        На Windows перечисление каталога не приносит ни ``st_dev``, ни
        ``st_ino`` — там нули. Из-за этого проверка «другая файловая система»
        считала чужим каждый каталог, и скан ``C:`` обрывался на первом уровне:
        находились только файлы в корне вроде pagefile.sys.

        Настоящий ``os.stat`` стоит отдельного обращения к диску, поэтому он
        делается лишь там, где нули действительно что-то скрывают, — на точках
        подключения и прочих reparse point'ах. Обычных каталогов это не
        касается, и на большом дереве лишней работы не появляется.
        """
        if st.st_dev or not _REPARSE_FLAG:
            return st
        if not getattr(st, "st_file_attributes", 0) & _REPARSE_FLAG:
            return st
        try:
            return os.stat(_long_path(path))
        except OSError:
            return st

    def _is_boundary(self, entry_path: str, st: os.stat_result) -> bool:
        """Нужно ли остановиться на этом каталоге, не заходя внутрь."""
        if entry_path in PSEUDO_DIRS:
            self._add_boundary(entry_path)
            return True
        if self.cross_filesystems or self._root_dev is None:
            return False
        # Ноль означает «сведений нет», а не «другое устройство». Считать
        # отсутствие сведений границей — значит не зайти вообще никуда.
        if st.st_dev and st.st_dev != self._root_dev:
            self._add_boundary(entry_path)
            return True
        return False

    def _file_size(self, stat: os.stat_result) -> int:
        """Размер файла в выбранном режиме.

        ``st_blocks`` есть только на POSIX; на Windows остаётся номинальный
        размер. Реально занятое место честнее для sparse-файлов и для каталогов
        с мелочью, где каждый файл занимает целый блок.
        """
        if self.size_mode == SIZE_DISK:
            blocks = getattr(stat, "st_blocks", None)
            if blocks is not None:
                return blocks * 512
        return stat.st_size

    def _charge(self, stat: os.stat_result, size: int) -> tuple[int, int]:
        """Сколько записать узлу и какие флаги ему поставить.

        Файл с несколькими жёсткими ссылками занимает место один раз: первое
        встреченное имя несёт весь размер, последующие — ноль и пометку.
        """
        if not self.dedup_hardlinks or getattr(stat, "st_nlink", 1) <= 1:
            return size, 0
        key = (stat.st_dev, stat.st_ino)
        with self._inode_lock:
            if key in self._seen_inodes:
                self._hardlink_saved += size
                return 0, HARDLINK
            self._seen_inodes.add(key)
        return size, 0

    # --- обход -------------------------------------------------------------

    def scan(self, root_path: str) -> ScanResult:
        """Просканировать ``root_path`` и вернуть дерево с агрегированными размерами."""
        self._cancel.clear()
        self._counter = 0
        self._skipped = []
        self._boundaries = []
        self._seen_inodes = set()
        self._hardlink_saved = 0
        self._seen_dirs = set()
        self._aliases = []
        self._trash_dirs = []

        # Канонический корень: иначе одно и то же дерево можно открыть под
        # разными именами и получить два несовместимых набора путей.
        root_path = os.path.realpath(os.path.abspath(os.path.expanduser(root_path)))
        name = os.path.basename(root_path.rstrip(os.sep)) or root_path
        try:
            self._root_dev = os.stat(_long_path(root_path)).st_dev
        except OSError:
            self._root_dev = None

        try:
            self._mark_visited(os.stat(_long_path(root_path)))
        except OSError:
            pass

        root = FileNode(path=root_path, name=name, is_dir=True,
                        mtime=_safe_mtime(root_path))

        tasks: queue.Queue = queue.Queue()
        tasks.put((root, root_path))

        workers = [
            threading.Thread(target=self._worker, args=(tasks,),
                             name=f"fs-scan-{i}", daemon=True)
            for i in range(self.max_workers)
        ]
        for thread in workers:
            thread.start()
        tasks.join()
        # Сентинелы гасят воркеров: без них потоки остались бы висеть на get().
        for _ in workers:
            tasks.put(None)
        for thread in workers:
            thread.join(timeout=5.0)

        recompute_sizes(root)

        if self.on_progress:
            self.on_progress(self._counter, root_path)
        return ScanResult(
            root=root,
            skipped=list(self._skipped),
            boundaries=list(self._boundaries),
            hardlink_saved=self._hardlink_saved,
            aliases=list(self._aliases),
            trash_dirs=list(self._trash_dirs),
            size_mode=self.size_mode,
            dedup_hardlinks=self.dedup_hardlinks,
        )

    def _worker(self, tasks: queue.Queue) -> None:
        while True:
            item = tasks.get()
            # task_done обязателен на каждом пути выхода, включая неожиданное
            # исключение: без него tasks.join() повиснет навсегда.
            try:
                if item is None:
                    return
                # После отмены задачи всё равно снимаются с очереди. Отданное
                # дерево при этом остаётся частичным — так и задумано.
                if self._cancel.is_set():
                    continue
                node, path = item
                try:
                    self._fill_dir(node, path, tasks)
                except Exception:  # noqa: BLE001 — воркер не должен падать молча
                    self._add_skipped(path)
            finally:
                tasks.task_done()

    def _mark_visited(self, stat: os.stat_result) -> bool:
        """Отметить каталог посещённым. ``False`` — если уже заходили.

        Где ``st_ino`` не сообщается (нули), защита выключается: лучше посчитать
        дважды, чем пропустить настоящие каталоги.
        """
        if not stat.st_ino:
            return True
        key = (stat.st_dev, stat.st_ino)
        with self._seen_dirs_lock:
            if key in self._seen_dirs:
                return False
            self._seen_dirs.add(key)
        return True

    def _trash_size(self, path: str) -> int:
        """Сколько занимает корзина. Узлы внутри не создаются: они не нужны."""
        return _dir_bytes(path, self._file_size)

    def _fill_dir(self, node: FileNode, dir_path: str, tasks: queue.Queue) -> None:
        """Прочитать один каталог: файлы — сразу в узлы, папки — обратно в очередь."""
        try:
            with os.scandir(_long_path(dir_path)) as it:
                for entry in it:
                    if self._cancel.is_set():
                        return
                    entry_path = os.path.join(dir_path, entry.name)
                    try:
                        if entry.is_symlink():
                            # Не ходим по симлинкам/реепарс-поинтам: иначе цикл
                            # или двойной счёт одного и того же содержимого.
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            try:
                                dir_stat = self._resolve_dir_stat(
                                    entry_path, entry.stat(follow_symlinks=False)
                                )
                            except OSError:
                                self._add_skipped(entry_path)
                                continue
                            if self._is_boundary(entry_path, dir_stat):
                                continue
                            if not self._mark_visited(dir_stat):
                                with self._skipped_lock:
                                    self._aliases.append(entry_path)
                                continue

                            # Своя корзина: размер считаем, чтобы было видно,
                            # сколько места ещё занято, но внутрь не идём.
                            # Удалённое не должно возвращаться в поиск и в
                            # списки — пользователь считает его исчезнувшим.
                            is_trash = entry.name == TRASH_DIR_NAME
                            child = FileNode(
                                name=entry.name, is_dir=True,
                                mtime=_entry_mtime(entry),
                                flags=TRASH if is_trash else 0,
                            )
                            node.attach(child)
                            if is_trash:
                                child.size = self._trash_size(entry_path)
                                with self._skipped_lock:
                                    self._trash_dirs.append(entry_path)
                            else:
                                tasks.put((child, entry_path))
                        else:
                            stat = entry.stat(follow_symlinks=False)
                            size, flags = self._charge(stat, self._file_size(stat))
                            node.attach(FileNode(
                                name=entry.name, size=size, mtime=stat.st_mtime,
                                flags=flags,
                            ))
                            self._bump(entry_path)
                    except (PermissionError, OSError):
                        self._add_skipped(entry_path)
        except (PermissionError, OSError):
            self._add_skipped(dir_path)


def _dir_bytes(path: str, size_of) -> int:
    """Суммарный размер поддерева без построения узлов."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=None):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            try:
                if not os.path.islink(full):
                    total += size_of(os.stat(full, follow_symlinks=False))
            except OSError:
                pass
    return total


def _safe_mtime(path: str) -> float:
    try:
        return os.stat(_long_path(path)).st_mtime
    except (PermissionError, OSError):
        return 0.0


def _entry_mtime(entry: os.DirEntry) -> float:
    try:
        return entry.stat(follow_symlinks=False).st_mtime
    except (PermissionError, OSError):
        return 0.0


def scan(
    root_path: str,
    on_progress: ProgressCallback | None = None,
    cross_filesystems: bool = False,
    size_mode: str = SIZE_DISK,
    dedup_hardlinks: bool = True,
    max_workers: int | None = None,
) -> ScanResult:
    """Удобная функция-обёртка для разового сканирования."""
    return Scanner(
        on_progress=on_progress,
        cross_filesystems=cross_filesystems,
        size_mode=size_mode,
        dedup_hardlinks=dedup_hardlinks,
        max_workers=max_workers,
    ).scan(root_path)
