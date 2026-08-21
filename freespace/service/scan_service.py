"""Фоновые задачи сканирования: запуск, прогресс, отмена, доступ к дереву, кэш."""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field

from ..core.cache import Cache
from ..core.model import FileNode
from ..core.platform_utils import cache_dir
from ..core.scanner import SIZE_DISK, ScanCancelled, Scanner, ScanResult
from ..core.trash import canonical

# Состояния задачи.
RUNNING = "running"
DONE = "done"
CANCELLED = "cancelled"
ERROR = "error"


@dataclass
class ScanJob:
    """Одна задача сканирования и всё, что о ней известно."""

    id: str
    root_path: str
    state: str = RUNNING
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    scanned: int = 0
    current_path: str = ""
    error: str = ""
    result: ScanResult | None = None
    size_mode: str = SIZE_DISK
    dedup_hardlinks: bool = True
    # Данные взяты из снимка, а не с диска.
    from_cache: bool = False
    snapshot_at: float | None = None
    _scanner: Scanner | None = field(default=None, repr=False)

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at


class ScanService:
    """Реестр задач сканирования.

    Потокобезопасен: сканер докладывает о прогрессе из рабочих потоков, а
    интерфейс опрашивает состояние из своих.
    """

    def __init__(self, max_jobs: int = 8, cache: Cache | None = None,
                 use_cache: bool = True) -> None:
        self._jobs: dict[str, ScanJob] = {}
        self._lock = threading.Lock()
        self._max_jobs = max_jobs
        self.use_cache = use_cache
        self._cache = cache
        self._cache_lock = threading.Lock()
        _remove_legacy_files()

    @property
    def cache(self) -> Cache | None:
        """Кэш создаётся лениво: без него приложение обязано работать."""
        if not self.use_cache:
            return None
        if self._cache is None:
            with self._cache_lock:
                if self._cache is None:
                    try:
                        self._cache = Cache()
                    except OSError:
                        self.use_cache = False
                        return None
        return self._cache

    # --- запуск и управление ---------------------------------------------

    def start(
        self,
        root_path: str,
        cross_filesystems: bool = False,
        size_mode: str = SIZE_DISK,
        dedup_hardlinks: bool = True,
        from_cache: bool = True,
    ) -> ScanJob:
        """Запустить скан в фоне и сразу вернуть задачу.

        При ``from_cache`` готовый снимок подставляется мгновенно, без обхода
        диска: задача сразу оказывается в состоянии ``done``.
        """
        root_path = os.path.abspath(os.path.expanduser(root_path))
        if not os.path.isdir(root_path):
            raise NotADirectoryError(
                f"Это не папка: {root_path}. Укажите папку или диск, а не отдельный файл."
            )
        if not os.access(root_path, os.R_OK | os.X_OK):
            raise PermissionError(
                f"Нет доступа к папке {root_path}. Выберите другую или запустите "
                "приложение от имени пользователя, которому она открыта."
            )

        if from_cache:
            cached = self._job_from_cache(root_path, size_mode, dedup_hardlinks)
            if cached is not None:
                return cached

        job = ScanJob(id=uuid.uuid4().hex[:12], root_path=root_path,
                      size_mode=size_mode, dedup_hardlinks=dedup_hardlinks)
        scanner = Scanner(
            on_progress=lambda count, path: self._on_progress(job.id, count, path),
            cross_filesystems=cross_filesystems,
            size_mode=size_mode,
            dedup_hardlinks=dedup_hardlinks,
        )
        job._scanner = scanner

        with self._lock:
            self._evict_locked()
            self._jobs[job.id] = job

        thread = threading.Thread(
            target=self._run, args=(job, scanner), name=f"scan-{job.id}", daemon=True
        )
        thread.start()
        return job

    def _job_from_cache(
        self, root_path: str, size_mode: str, dedup_hardlinks: bool
    ) -> ScanJob | None:
        """Готовая задача из снимка, если он есть и снят в том же режиме."""
        cache = self.cache
        if cache is None:
            return None
        info = cache.latest_snapshot(root_path)
        if info is None or info.size_mode != size_mode:
            return None
        root = cache.load_snapshot(root_path)
        if root is None:
            return None

        job = ScanJob(
            id=uuid.uuid4().hex[:12],
            root_path=root_path,
            state=DONE,
            finished_at=time.time(),
            scanned=root.file_count,
            result=ScanResult(root=root, size_mode=size_mode,
                              dedup_hardlinks=dedup_hardlinks),
            size_mode=size_mode,
            dedup_hardlinks=dedup_hardlinks,
            from_cache=True,
            snapshot_at=info.created_at,
        )
        with self._lock:
            self._evict_locked()
            self._jobs[job.id] = job
        return job

    def rescan(self, job_id: str, path: str | None = None) -> ScanJob:
        """Обойти заново только указанную папку, не трогая остальное дерево.

        Пересканировать весь том ради одной подпапки — минуты ожидания там, где
        достаточно секунды. Поддерево обходится отдельно и вставляется на место,
        а размеры предков поправляются на разницу: ссылка на родителя у узла уже
        есть, так что это дешёвый проход вверх, а не пересчёт всего дерева.
        """
        job = self.get(job_id)
        if job is None or job.result is None:
            raise LookupError("Результаты сканирования не найдены.")

        target = canonical(path) if path else job.root_path
        node = self.find_node(job_id, target)
        if node is None:
            raise LookupError(f"Папки {target} нет в результатах обхода.")
        if node is job.result.root:
            # Корень — это обычный полный обход, отдельная задача.
            return self.start(job.root_path, size_mode=job.size_mode,
                              dedup_hardlinks=job.dedup_hardlinks, from_cache=False)

        scanner = Scanner(
            on_progress=lambda count, p: self._on_progress(job.id, count, p),
            size_mode=job.size_mode,
            dedup_hardlinks=job.dedup_hardlinks,
        )
        with self._lock:
            job.state = RUNNING
            job.started_at = time.time()
            job.finished_at = None
            job.current_path = target
            job.error = ""
            job._scanner = scanner

        threading.Thread(
            target=self._run_partial, args=(job, scanner, node, target),
            name=f"rescan-{job.id}", daemon=True,
        ).start()
        return job

    def _run_partial(self, job: ScanJob, scanner: Scanner,
                     node: FileNode, target: str) -> None:
        try:
            fresh = scanner.scan(target)
        except (OSError, MemoryError) as exc:
            self._finish(job, ERROR, result=job.result,
                         error=f"{type(exc).__name__}: {exc}")
            return

        with self._lock:
            _splice(node, fresh.root)
            result = job.result
            if result is not None:
                inside = target.rstrip(os.sep) + os.sep
                result.trash_dirs = [d for d in result.trash_dirs
                                     if not d.startswith(inside)] + fresh.trash_dirs
            job.state = CANCELLED if scanner.cancelled else DONE
            job.finished_at = time.time()
            job.from_cache = False
            job.snapshot_at = None
            if result is not None:
                job.scanned = result.root.file_count

        if not scanner.cancelled and job.result is not None:
            self._save_snapshot(job, job.result)

    def _run(self, job: ScanJob, scanner: Scanner) -> None:
        try:
            result = scanner.scan(job.root_path)
        except ScanCancelled:
            self._finish(job, CANCELLED)
            return
        except (OSError, MemoryError) as exc:
            self._finish(job, ERROR, error=f"{type(exc).__name__}: {exc}")
            return

        # Отмена могла прийти в момент, когда обход уже сворачивался сам.
        if scanner.cancelled:
            self._finish(job, CANCELLED, result=result)
        else:
            self._finish(job, DONE, result=result)
            self._save_snapshot(job, result)

    def _save_snapshot(self, job: ScanJob, result: ScanResult) -> None:
        """Сохранить снимок, не задерживая ответ интерфейсу.

        Ошибка записи кэша не должна ломать сам скан: он уже удался, а кэш —
        только ускорение.
        """
        cache = self.cache
        if cache is None:
            return

        def write() -> None:
            try:
                cache.save_snapshot(result.root, size_mode=result.size_mode,
                                    dedup_hardlinks=result.dedup_hardlinks)
            except (OSError, ValueError):
                pass

        threading.Thread(target=write, name=f"snapshot-{job.id}", daemon=True).start()

    def _finish(
        self, job: ScanJob, state: str, result: ScanResult | None = None, error: str = ""
    ) -> None:
        with self._lock:
            job.state = state
            job.result = result
            job.error = error
            job.finished_at = time.time()
            if result is not None:
                job.scanned = result.root.file_count

    def _on_progress(self, job_id: str, count: int, path: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.scanned = count
                job.current_path = path

    def cancel(self, job_id: str) -> bool:
        """Запросить отмену. ``False``, если задачи нет или она уже закончилась."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state != RUNNING or job._scanner is None:
                return False
            scanner = job._scanner
        scanner.cancel()
        return True

    # --- доступ ----------------------------------------------------------

    def get(self, job_id: str) -> ScanJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[ScanJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def invalidate_snapshot(self, root_path: str) -> None:
        """Забыть сохранённый снимок корня.

        После удаления снимок описывает уже несуществующее дерево. Если его не
        выбросить, следующее нажатие «Сканировать» покажет удалённые папки на
        прежних местах — как будто удаления не было.
        """
        cache = self.cache
        if cache is None:
            return
        try:
            cache.forget(os.path.abspath(os.path.expanduser(root_path)))
        except OSError:
            pass

    def find_node(self, job_id: str, path: str | None = None) -> FileNode | None:
        """Узел дерева по абсолютному пути. Без пути — корень."""
        job = self.get(job_id)
        if job is None or job.result is None:
            return None
        root = job.result.root
        if not path:
            return root

        target = os.path.abspath(path)
        if target == root.path:
            return root
        if not target.startswith(root.path.rstrip(os.sep) + os.sep):
            return None

        relative = os.path.relpath(target, root.path)
        node = root
        for part in relative.split(os.sep):
            if not node.children:
                return None
            for child in node.children:
                if child.name == part:
                    node = child
                    break
            else:
                return None
        return node

    def _evict_locked(self) -> None:
        """Выбросить самые старые завершённые задачи, если их накопилось много."""
        if len(self._jobs) < self._max_jobs:
            return
        finished = [j for j in self._jobs.values() if j.state != RUNNING]
        finished.sort(key=lambda j: j.finished_at or 0)
        while finished and len(self._jobs) >= self._max_jobs:
            self._jobs.pop(finished.pop(0).id, None)


def _splice(node: FileNode, fresh: FileNode) -> None:
    """Заменить содержимое узла свежим и поправить размеры предков."""
    old_size, old_count = node.size, node.file_count

    node.children = fresh.children
    for child in node.children or ():
        child.parent = node
        child._abs = None
    node.size = fresh.size
    node.file_count = fresh.file_count
    node.mtime = fresh.mtime

    delta_size = node.size - old_size
    delta_count = node.file_count - old_count
    if not delta_size and not delta_count:
        return
    parent = node.parent
    while parent is not None:
        parent.size += delta_size
        parent.file_count += delta_count
        parent = parent.parent


def _remove_legacy_files() -> None:
    """Стереть то, что осталось от прежних версий.

    Корзина в служебном каталоге приложения: сканер её не прятал, и её
    содержимое возвращалось в поиск как обычные папки. Новая версия такой
    корзины не создаёт.

    База SQLite: кэш снимков давно переехал в gzip-файлы, а база осталась
    лежать — на этой машине она занимала 232 МБ. Инструмент, показывающий, чем
    занято место, не должен сам держать сотни мегабайт мусора.
    """
    base = cache_dir()
    for name, remove in (("trash", shutil.rmtree),
                         ("cache.db", os.remove),
                         ("cache.db-wal", os.remove),
                         ("cache.db-shm", os.remove)):
        path = os.path.join(base, name)
        if not os.path.exists(path):
            continue
        try:
            remove(path)
            print(f"FreeSpace: удалён остаток прежней версии {path}", flush=True)
        except OSError as exc:
            print(f"FreeSpace: не удалось удалить {path}: {exc}", flush=True)
