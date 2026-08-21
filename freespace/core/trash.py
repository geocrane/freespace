"""Корзина: удаление с возможностью вернуть.

Объект не стирается, а переезжает в ``.freespace-trash`` — на той же файловой
системе это мгновенное ``os.rename`` независимо от размера. Рядом кладётся
``meta.json`` с исходным путём.

Место корзины определяется одним правилом, без исключений::

    корень скана доступен на запись        → <корень>/.freespace-trash
    иначе домашний каталог внутри корня    → ~/.freespace-trash
    иначе                                  → TrashUnavailable

Прежняя версия при недоступном корне тихо уводила корзину в служебный каталог
приложения и переключалась на копирование. Сканер такую корзину не прятал, и
удалённое возвращалось в поиск, а пути в двух местах расходились. Тихих
запасных путей больше нет: если писать некуда, удаление честно отклоняется.

Метаданные каждой записи лежат в собственном файле, а не в общем манифесте: по
той же причине, что и снимки кэша (``core/cache.py``) — дописывание в общий файл
на NFS требует блокировок, а они там ненадёжны. Список корзины — это ``listdir``.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from typing import Callable

TRASH_DIR_NAME = ".freespace-trash"
META_NAME = "meta.json"


class TrashError(Exception):
    """Не удалось выполнить операцию с корзиной."""


class TrashUnavailable(TrashError):
    """Корзину негде создать: везде только чтение."""


class ProtectedPathError(Exception):
    """Путь защищён от удаления."""


def canonical(path: str) -> str:
    """Путь в единственном написании.

    Один каталог бывает виден под несколькими именами: на macOS
    ``/opt/homebrew`` и ``/System/Volumes/Data/opt/homebrew`` — это один и тот
    же объект. Без приведения к общему виду два имени одного объекта попадают в
    одну пачку удаления, и второе завершается ошибкой «не нашлось».
    """
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _protected_paths() -> set[str]:
    """Пути, удаление которых ломает систему или домашний каталог целиком."""
    paths = {
        "/", "/System", "/Library", "/Applications", "/Volumes", "/usr", "/bin",
        "/sbin", "/etc", "/var", "/opt", "/private", "/boot", "/lib", "/lib64",
        "/proc", "/sys", "/dev", "/run", "/srv", "/tmp", "/Users", "/home", "/root",
    }
    if os.name == "nt":
        import string

        paths |= {f"{letter}:\\" for letter in string.ascii_uppercase}
        for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)",
                    "ProgramData", "SystemDrive", "windir"):
            value = os.environ.get(var)
            if value:
                paths.add(os.path.abspath(value))
    home = os.path.expanduser("~")
    if home and home != "~":
        paths.add(home)

    # Каждый путь и в исходном виде, и в каноническом: на macOS /etc — это
    # симлинк на /private/etc, и проверяемый путь приходит уже разрешённым.
    # Без этого /etc проходил бы защиту насквозь.
    result = set()
    for path in paths:
        result.add(os.path.normpath(path) if path != "/" else "/")
        try:
            result.add(canonical(path))
        except OSError:
            pass
    return result


def _is_inside(target: str, root: str) -> bool:
    """``target`` лежит строго внутри ``root``."""
    return target.startswith(root.rstrip(os.sep) + os.sep)


def _same_volume(one: str, other: str) -> bool:
    """Один ли это том. Перенос между томами — уже не переименование."""
    try:
        dev_one, dev_other = os.stat(one).st_dev, os.stat(other).st_dev
    except OSError:
        return False
    if dev_one and dev_other:
        return dev_one == dev_other
    # Сведений о томе нет (бывает на сетевых ФС) — сравниваем букву диска.
    return os.path.splitdrive(one)[0].lower() == os.path.splitdrive(other)[0].lower()


def trash_candidates(scan_root: str) -> list[str]:
    """Куда корзина может лечь, в порядке предпочтения.

    Сначала домашний каталог: туда у пользователя доступ есть всегда, а в корень
    диска писать обычно нельзя — на Windows попытка создать ``C:\\.freespace-trash``
    заканчивается «Отказано в доступе». Годится он, только если лежит на том же
    томе: перенос между томами — это уже копирование, долгое и требующее места.

    Побочная польза: при сканировании подпапки корзина оказывается снаружи
    просканированного, и удалённое не может попасть в результаты поиска в
    принципе.
    """
    root = canonical(scan_root)
    home = canonical("~")
    places = []
    if _same_volume(home, root):
        places.append(os.path.join(home, TRASH_DIR_NAME))
    root_place = os.path.join(root, TRASH_DIR_NAME)
    if root_place not in places:
        places.append(root_place)
    return places


def _can_write(directory: str) -> bool:
    """Проверить запись делом, а не вопросом к правам.

    ``os.access(path, os.W_OK)`` на Windows смотрит только атрибут «только
    чтение» и для корня диска отвечает «можно», хотя обычному пользователю туда
    писать нельзя. Из-за этого корзина пыталась появиться в ``C:\\`` и падала
    с «Отказано в доступе» уже в момент удаления. Создание временной папки —
    единственная проверка, которая говорит правду на всех системах.
    """
    probe = os.path.join(directory, f".probe-{uuid.uuid4().hex[:8]}")
    try:
        os.mkdir(probe)
    except OSError:
        return False
    try:
        os.rmdir(probe)
    except OSError:
        pass
    return True


def trash_dir_for(scan_root: str) -> str:
    """Создать корзину для этого корня и вернуть путь к ней."""
    tried = []
    for candidate in trash_candidates(scan_root):
        tried.append(candidate)
        try:
            os.makedirs(candidate, exist_ok=True)
        except OSError:
            continue
        if _can_write(candidate):
            return candidate

    raise TrashUnavailable(
        "Негде создать корзину: ни в одно из этих мест писать нельзя — "
        + ", ".join(tried)
        + ". Удаление отсюда недоступно; выберите папку, куда у вас есть доступ "
        "на запись, — например свой домашний каталог."
    )


def ensure_deletable(path: str, scan_root: str) -> str:
    """Проверить, что путь вообще можно удалять. Вернуть канонический путь.

    Ограничений три, и все нужны. Внутри корня скана — чтобы запрос по HTTP не
    дотянулся до произвольного места в файловой системе. Не системный путь — от
    опечатки в поле ввода. Не сам корень и не корзина — от удаления всего разом.
    """
    target = canonical(path)
    root = canonical(scan_root)

    if target == root:
        raise ProtectedPathError(
            "Нельзя удалить саму просканированную папку — выберите что-нибудь внутри неё."
        )
    if not _is_inside(target, root):
        raise ProtectedPathError(
            f"Удалять можно только внутри просканированной папки, а {target} лежит вне её."
        )
    if TRASH_DIR_NAME in target.split(os.sep):
        raise ProtectedPathError(
            "Это сама корзина. Чтобы стереть её содержимое, нажмите «Очистить корзину»."
        )

    protected = _protected_paths()
    if target in protected:
        raise ProtectedPathError(
            f"{target} — системная папка, без неё система работать не будет. "
            "Удаление запрещено."
        )
    # Удаление родителя защищённого пути равносильно удалению самого пути.
    prefix = target.rstrip(os.sep) + os.sep
    for guarded in protected:
        if guarded.startswith(prefix):
            raise ProtectedPathError(
                f"Внутри лежит системная папка {guarded} — удалив это, вы удалили бы и её. "
                "Выберите что-нибудь поменьше."
            )

    if not os.path.lexists(target):
        raise FileNotFoundError(f"Не нашлось: {target}")
    return target


@dataclass
class TrashEntry:
    """Запись корзины."""

    id: str
    name: str
    original_path: str
    trashed_path: str
    size: int
    is_dir: bool
    deleted_at: float


class Trash:
    """Корзина для одного корня сканирования."""

    def __init__(self, scan_root: str, dir_path: str | None = None) -> None:
        self.scan_root = canonical(scan_root)
        self._explicit_dir = dir_path
        self._dir: str | None = None

    @property
    def dir_path(self) -> str:
        """Каталог корзины, создаваемый при первом обращении."""
        if self._dir is None:
            if self._explicit_dir:
                target = os.path.abspath(self._explicit_dir)
                try:
                    os.makedirs(target, exist_ok=True)
                except OSError as exc:
                    raise TrashUnavailable(
                        f"Не удалось создать корзину {target}: {exc}"
                    ) from exc
            else:
                target = trash_dir_for(self.scan_root)
            self._dir = target
        return self._dir

    @property
    def available(self) -> bool:
        """Можно ли вообще удалять в этом корне."""
        try:
            self.dir_path
        except TrashUnavailable:
            return False
        return True

    # --- удаление ----------------------------------------------------------

    def move_to_trash(self, path: str,
                      on_size: Callable[[int], None] | None = None) -> TrashEntry:
        """Перенести объект в корзину. Возвращает запись для восстановления.

        ``on_size`` получает число файлов, пройденных при подсчёте размера, —
        это и есть всё ожидание при удалении большой папки.
        """
        target = ensure_deletable(path, self.scan_root)
        is_dir = os.path.isdir(target) and not os.path.islink(target)
        size = tree_size(target, on_progress=on_size)

        entry_id = f"{int(time.time()):010d}-{uuid.uuid4().hex[:8]}"
        entry_dir = os.path.join(self.dir_path, entry_id)
        os.makedirs(entry_dir, exist_ok=False)
        destination = os.path.join(entry_dir, os.path.basename(target))

        try:
            # Корзина заведомо на том же томе, что и объект: она лежит в одном
            # из его предков, а обход не пересекает границы файловых систем.
            # Значит, переименование не может не сработать из-за «разных
            # устройств», и копирования как запасного пути не нужно.
            os.rename(target, destination)
        except OSError as exc:
            shutil.rmtree(entry_dir, ignore_errors=True)
            raise TrashError(f"Не удалось переместить в корзину: {exc}") from exc

        entry = TrashEntry(
            id=entry_id,
            name=os.path.basename(target),
            original_path=target,
            trashed_path=destination,
            size=size,
            is_dir=is_dir,
            deleted_at=time.time(),
        )
        _write_meta(entry_dir, entry)
        return entry

    # --- просмотр и возврат -------------------------------------------------

    def list_entries(self) -> list[TrashEntry]:
        """Содержимое корзины, свежее первым."""
        if not self.available:
            return []
        return list_entries_in(self.dir_path)

    def total_size(self) -> int:
        return sum(entry.size for entry in self.list_entries())

    def restore(self, entry_id: str) -> str:
        """Вернуть объект на исходное место. Возвращает восстановленный путь."""
        entry = read_entry(self.dir_path, entry_id)
        if entry is None:
            raise TrashError(
                "Этой записи в корзине больше нет — возможно, корзину уже очистили."
            )
        if os.path.lexists(entry.original_path):
            raise TrashError(
                f"Вернуть не получится: по адресу {entry.original_path} уже что-то лежит. "
                "Переименуйте или уберите это, а потом повторите."
            )
        parent = os.path.dirname(entry.original_path)
        try:
            os.makedirs(parent, exist_ok=True)
            shutil.move(entry.trashed_path, entry.original_path)
        except (OSError, shutil.Error) as exc:
            raise TrashError(f"Не удалось вернуть объект на место: {exc}") from exc
        shutil.rmtree(os.path.join(self.dir_path, entry_id), ignore_errors=True)
        return entry.original_path

    def delete_entry(self, entry_id: str) -> None:
        """Стереть одну запись корзины окончательно."""
        entry_dir = os.path.join(self.dir_path, entry_id)
        if not os.path.isdir(entry_dir):
            raise TrashError("Этой записи в корзине больше нет.")
        shutil.rmtree(entry_dir, ignore_errors=True)

    def empty(self, older_than: float | None = None) -> tuple[int, int]:
        """Очистить корзину. Возвращает (сколько записей, сколько байт)."""
        return empty_dir(self.dir_path, older_than)


# --- работа с произвольным каталогом корзины -------------------------------
#
# Корзин на диске бывает несколько: сканировали ~/Dev — появилась
# ~/Dev/.freespace-trash, потом сканировали ~ — появилась ~/.freespace-trash.
# Пользователю нужен один список, поэтому функции ниже работают с любым
# каталогом, а сервис собирает записи из всех найденных.


def read_entry(trash_dir: str, entry_id: str) -> TrashEntry | None:
    try:
        with open(os.path.join(trash_dir, entry_id, META_NAME), encoding="utf-8") as fh:
            return TrashEntry(**json.load(fh))
    except (OSError, ValueError, TypeError):
        return None


def list_entries_in(trash_dir: str) -> list[TrashEntry]:
    """Записи одной корзины, свежие первыми."""
    entries: list[TrashEntry] = []
    try:
        names = os.listdir(trash_dir)
    except OSError:
        return entries
    for name in names:
        entry = read_entry(trash_dir, name)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda e: e.deleted_at, reverse=True)
    return entries


def empty_dir(trash_dir: str, older_than: float | None = None,
              on_entry: Callable[[TrashEntry], None] | None = None,
              on_removed: Callable[[TrashEntry], None] | None = None) -> tuple[int, int]:
    """Стереть содержимое одной корзины. Возвращает (записей, байт).

    Два обратных вызова, а не один: ``on_entry`` говорит, за что взялись
    (запись может стираться минуту), ``on_removed`` — что с ней покончено.
    Полосе хода нужно и то и другое: имя показывается до работы, а деление
    «сделано/всего» сдвигается после.
    """
    count = 0
    freed = 0
    for entry in list_entries_in(trash_dir):
        if older_than is not None and entry.deleted_at > older_than:
            continue
        if on_entry is not None:
            on_entry(entry)
        shutil.rmtree(os.path.join(trash_dir, entry.id), ignore_errors=True)
        count += 1
        freed += entry.size
        if on_removed is not None:
            on_removed(entry)
    return count, freed


def tree_size(path: str, on_progress: Callable[[int], None] | None = None,
              every: int = 2000) -> int:
    """Размер файла или суммарный размер поддерева.

    ``on_progress`` зовётся раз в ``every`` файлов и получает число уже
    пройденных. Это единственное, что можно показать, пока считается папка на
    сотню тысяч файлов: сам перенос потом мгновенный, а вот счёт занимает всё
    время ожидания, и без него интерфейс выглядит зависшим.
    """
    if os.path.islink(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    seen = 0
    reported = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=None):
        for filename in filenames:
            try:
                full = os.path.join(dirpath, filename)
                if not os.path.islink(full):
                    total += os.path.getsize(full)
            except OSError:
                pass
        seen += len(filenames)
        if on_progress is not None and seen - reported >= every:
            on_progress(seen)
            reported = seen
    return total


def _write_meta(entry_dir: str, entry: TrashEntry) -> None:
    """Записать метаданные атомарно: половинчатый meta.json нечитаем."""
    meta_path = os.path.join(entry_dir, META_NAME)
    tmp_path = meta_path + ".part"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(entry.__dict__, fh, ensure_ascii=False)
    os.replace(tmp_path, meta_path)
