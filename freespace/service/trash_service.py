"""Удаление файлов из просканированного дерева.

Обёртка над ``core.trash``, которая держится за два правила.

Первое: источник правды — файловая система, а не дерево в памяти. Между сканом
и щелчком по кнопке проходит время, за него файлы успевают исчезнуть сами.
Поэтому «объекта уже нет» здесь считается успехом, а не ошибкой: пользователь
хотел, чтобы объекта не было, — его нет.

Второе: один объект бывает виден под несколькими путями (firmlinks на macOS).
Пути приводятся к каноническому виду и дедуплицируются до удаления, иначе
второе имя того же объекта неминуемо даёт ошибку «не нашлось».
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

from ..core.trash import (
    ProtectedPathError,
    Trash,
    TrashEntry,
    TrashError,
    TrashUnavailable,
    canonical,
    empty_dir,
    list_entries_in,
    read_entry,
)
from .progress import SILENT, Progress
from .scan_service import ScanService


@dataclass
class BulkResult:
    """Итог группового удаления."""

    deleted: list[TrashEntry] = field(default_factory=list)
    # Уехали в корзину вместе с отмеченным родителем.
    inside_deleted: list[str] = field(default_factory=list)
    # Уже отсутствовали на диске — это тоже нужный результат, а не сбой.
    already_gone: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    freed: int = 0

    def failure_groups(self) -> list[tuple[str, list[str]]]:
        """Причины отказа с примерами путей, по убыванию числа затронутых.

        Девяносто девять одинаковых абзацев прочитать невозможно; одна строка
        «12 объектов — системные пути» говорит ровно столько же.
        """
        groups: dict[str, list[str]] = {}
        for path, reason in self.failed:
            groups.setdefault(reason, []).append(path)
        return sorted(groups.items(), key=lambda pair: len(pair[1]), reverse=True)


class TrashService:
    """Корзины по корням сканирования."""

    def __init__(self, scan_service: ScanService) -> None:
        self._scans = scan_service
        self._trashes: dict[str, Trash] = {}
        self._lock = threading.Lock()

    def for_root(self, root_path: str) -> Trash:
        key = canonical(root_path)
        with self._lock:
            trash = self._trashes.get(key)
            if trash is None:
                trash = Trash(key)
                self._trashes[key] = trash
            return trash

    def for_job(self, job_id: str) -> Trash:
        job = self._scans.get(job_id)
        if job is None:
            raise TrashError("Результаты сканирования не найдены — просканируйте папку заново.")
        return self.for_root(job.root_path)

    # --- где искать записи -------------------------------------------------

    def trash_dirs(self, job_id: str) -> list[str]:
        """Все корзины, относящиеся к этой задаче.

        Корзина у корня скана — основная. Но внутри просканированного могут
        лежать корзины от прежних, более узких сканов: сканировали ~/Dev —
        осталась ~/Dev/.freespace-trash, теперь сканируем ~. Их содержимое
        занимает место и должно попасть в тот же список, иначе до него нельзя
        добраться из интерфейса.
        """
        dirs: list[str] = []
        trash = self.for_job(job_id)
        if trash.available:
            dirs.append(trash.dir_path)

        job = self._scans.get(job_id)
        if job is not None and job.result is not None:
            for found in job.result.trash_dirs:
                if found not in dirs:
                    dirs.append(found)
        return dirs

    def entries(self, job_id: str) -> list[tuple[str, TrashEntry]]:
        """Записи всех корзин задачи, свежие первыми."""
        found: list[tuple[str, TrashEntry]] = []
        for directory in self.trash_dirs(job_id):
            found += [(directory, entry) for entry in list_entries_in(directory)]
        found.sort(key=lambda pair: pair[1].deleted_at, reverse=True)
        return found

    # --- удаление ----------------------------------------------------------

    def delete(self, job_id: str, path: str, progress: Progress = SILENT) -> TrashEntry:
        """Убрать путь в корзину и вычесть его из дерева в памяти."""
        progress.plan(1)
        progress.step(path)
        entry = self._delete_one(job_id, path, progress)
        progress.advance(entry.size)
        self._invalidate(job_id)
        return entry

    def _delete_one(self, job_id: str, path: str,
                    progress: Progress = SILENT) -> TrashEntry:
        job = self._scans.get(job_id)
        if job is None or job.result is None:
            raise TrashError(
                "Удалять можно только по готовым результатам: дождитесь конца обхода "
                "или просканируйте папку заново."
            )

        node = self._scans.find_node(job_id, path)
        entry = self.for_root(job.root_path).move_to_trash(path, on_size=progress.count)
        if node is not None and node is not job.result.root:
            # Дерево правим только после удавшегося переноса: иначе картинка
            # разойдётся с диском.
            node.detach()
        return entry

    def delete_many(self, job_id: str, paths: list[str],
                    progress: Progress = SILENT) -> BulkResult:
        """Убрать в корзину несколько объектов разом.

        Порядок важен: сначала родители, потом вложенное. Сортировка идёт по
        частям пути, а не по строке, — тогда потомки стоят сразу за предком и
        между ними не вклинится посторонний путь вроде ``/a-b`` между ``/a`` и
        ``/a/b``.
        """
        job = self._scans.get(job_id)
        if job is None or job.result is None:
            raise TrashError(
                "Удалять можно только по готовым результатам: дождитесь конца обхода "
                "или просканируйте папку заново."
            )
        # Корзину проверяем один раз, до цикла: если её негде создать, незачем
        # сообщать об этом пять тысяч раз подряд.
        self.for_root(job.root_path).dir_path

        unique = {canonical(p) for p in paths}
        ordered = sorted(unique, key=lambda p: p.split(os.sep))

        result = BulkResult()
        inside_removed = None
        # Полоса хода двигается на каждом пути, чем бы он ни кончился: пропуски
        # и отказы — тоже сделанная работа, и полоса, застрявшая на 40% из-за
        # десятка системных путей, врёт не меньше, чем её отсутствие.
        progress.plan(len(ordered))
        for path in ordered:
            progress.step(path)
            if inside_removed is not None and path.startswith(inside_removed):
                result.inside_deleted.append(path)
                progress.advance()
                continue
            try:
                entry = self._delete_one(job_id, path, progress)
            except FileNotFoundError:
                # Объекта уже нет — ровно то, чего добивался пользователь.
                result.already_gone.append(path)
                progress.advance()
                continue
            except (ProtectedPathError, TrashError, OSError) as exc:
                result.failed.append((path, str(exc)))
                progress.advance()
                continue
            result.deleted.append(entry)
            result.freed += entry.size
            inside_removed = path.rstrip(os.sep) + os.sep
            progress.advance(entry.size)

        # Один раз на всю пачку, а не на каждый путь.
        self._invalidate(job_id)
        return result

    # --- возврат и очистка --------------------------------------------------

    def restore(self, job_id: str, entry_id: str) -> str:
        """Вернуть объект. Дерево при этом устаревает — нужно пересканировать."""
        for directory in self.trash_dirs(job_id):
            if read_entry(directory, entry_id) is not None:
                restored = Trash(self.for_job(job_id).scan_root,
                                 dir_path=directory).restore(entry_id)
                self._invalidate(job_id)
                return restored
        raise TrashError("Этой записи в корзине больше нет — возможно, корзину уже очистили.")

    def empty(self, job_id: str, progress: Progress = SILENT) -> tuple[int, int]:
        """Стереть содержимое всех корзин задачи.

        Здесь файлы стираются по-настоящему, и это самая долгая операция во всём
        приложении. Зато объём работы известен заранее — размеры записаны в
        ``meta.json``, — так что полоса показывает не «что-то происходит», а
        честную долю освобождённых байт.
        """
        entries = self.entries(job_id)
        progress.plan(len(entries), sum(entry.size for _dir, entry in entries))

        count = 0
        freed = 0
        for directory in self.trash_dirs(job_id):
            removed, bytes_freed = empty_dir(
                directory,
                on_entry=lambda entry: progress.step(entry.original_path or entry.name),
                on_removed=lambda entry: progress.advance(entry.size),
            )
            count += removed
            freed += bytes_freed
        self._invalidate(job_id)
        return count, freed

    def _invalidate(self, job_id: str) -> None:
        """Снимок описывает дерево до изменения — он больше не годится."""
        job = self._scans.get(job_id)
        if job is not None:
            self._scans.invalidate_snapshot(job.root_path)


__all__ = ["BulkResult", "TrashService", "TrashError", "TrashUnavailable",
           "ProtectedPathError"]
