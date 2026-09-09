"""Настройки, живущие между запусками: список сетевых папок.

Хранилищ два, и делятся они по тому, кто пишет. Список сетевых папок ведёт само
приложение — он лежит в JSON рядом с кэшем. PIN задаёт человек, поэтому он
живёт в ``.env`` на виду (``core/env.py``), а не здесь.

Json маленький и меняется от силы раз в месяц, поэтому ни базы, ни блокировок:
запись идёт во временный файл в том же каталоге и переезжает на место одним
``os.replace``, как снимки в ``core/cache.py``.

Чего этот PIN не делает. Он закрывает **кнопки в этом приложении**, а не файлы
на шаре: у кого есть доступ к сетевой папке, тот удалит оттуда что угодно
Проводником, не открывая FreeSpace. Смысл в другом — сетевые диски не мозолят
глаза и не удаляются в один клик по случайности, а тот, кто разбирает место,
делает это осознанно. Настоящее разграничение доступа — это права на самой
шаре, и подменять их эти двести строк не могут.
"""

from __future__ import annotations

import hmac
import json
import os
import tempfile
from dataclasses import dataclass, field

from . import env
from .platform_utils import cache_dir

CONFIG_NAME = "config.json"


def config_path() -> str:
    """Где лежит файл настроек."""
    override = os.environ.get("FREESPACE_CONFIG")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(cache_dir(), CONFIG_NAME)


@dataclass
class NetworkRoot:
    """Сетевая папка, добавленная руками."""

    path: str
    label: str = ""

    def as_dict(self) -> dict:
        return {"path": self.path, "label": self.label}


@dataclass
class Settings:
    """Содержимое файла настроек."""

    network_roots: list[NetworkRoot] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"network_roots": [r.as_dict() for r in self.network_roots]}


# --- чтение и запись -------------------------------------------------------


def load(path: str | None = None) -> Settings:
    """Прочитать настройки. Нет файла или он битый — пустые настройки.

    Падать тут нельзя: приложение обязано открываться и без конфига, показывая
    локальные диски, — иначе одна испорченная строка оставляет человека вообще
    без инструмента.
    """
    try:
        with open(path or config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()

    roots: list[NetworkRoot] = []
    for item in data.get("network_roots") or ():
        if isinstance(item, dict) and item.get("path"):
            roots.append(NetworkRoot(path=str(item["path"]),
                                     label=str(item.get("label") or "")))
    return Settings(network_roots=roots)


def save(settings: Settings, path: str | None = None) -> str:
    """Записать настройки атомарно. Возвращает путь к файлу."""
    target = path or config_path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(target) or ".", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings.as_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return target


# --- PIN --------------------------------------------------------------------


def configured_pin() -> str:
    """PIN на сетевые папки — из ``.env`` (или из окружения). Пустой — нечем открыть.

    Лежит он там открытым текстом, и это осознанно: замок закрывает кнопки в
    приложении, а не файлы на шаре, и человек должен видеть и менять комбинацию
    блокнотом, не запуская ничего в консоли. Хеш здесь создавал бы видимость
    защиты, которой всё равно нет: файлы на общем диске доступны Проводником
    всем, у кого есть на них права.
    """
    return env.pin()


def has_pin() -> bool:
    return bool(configured_pin())


def verify_pin(pin: str) -> bool:
    """Совпадает ли введённая комбинация с заданной."""
    expected = configured_pin()
    if not expected:
        return False
    # Сравнение за постоянное время: по скорости обычного == комбинацию можно
    # угадывать посимвольно.
    return hmac.compare_digest(pin.encode("utf-8"), expected.encode("utf-8"))


# --- что считать сетевым ----------------------------------------------------

# Тип диска на Windows. Вызов на отвалившейся шаре успевает подвиснуть, поэтому
# ответ запоминается — но только содержательный. «Буквы нет» (DRIVE_NO_ROOT_DIR)
# и «не знаю» кэшировать нельзя: именно из этого состояния буква и переходит в
# «сетевой диск», когда шару подключают уже после запуска приложения, — и
# запомненный отказ показал бы её потом как локальную.
_DRIVE_UNKNOWN = 0
_DRIVE_NO_ROOT = 1
_DRIVE_REMOTE = 4
_drive_kinds: dict[str, int] = {}


def _drive_type(drive: str) -> int:
    """``GetDriveTypeW`` для буквы диска. Вне Windows — 0."""
    if os.name != "nt":
        return 0
    key = drive.upper()
    kind = _drive_kinds.get(key)
    if kind is not None:
        return kind
    try:
        import ctypes

        kind = int(ctypes.windll.kernel32.GetDriveTypeW(key + "\\"))
    except Exception:  # noqa: BLE001 — нет kernel32 или экзотическая сборка
        kind = _DRIVE_UNKNOWN
    if kind not in (_DRIVE_UNKNOWN, _DRIVE_NO_ROOT):
        _drive_kinds[key] = kind
    return kind


def network_drive_letters() -> set[str]:
    """Буквы подключённых сетевых дисков, например ``{"Z:"}``.

    Нужны не для того, чтобы что-то добавить в список корней, — он ведётся
    руками, — а чтобы такие буквы не показывались в общем списке томов наравне
    с локальными до ввода PIN.
    """
    if os.name != "nt":
        return set()
    import string

    return {f"{letter}:" for letter in string.ascii_uppercase
            if _drive_type(f"{letter}:") == _DRIVE_REMOTE}


def is_unc(path: str) -> bool:
    """Путь вида ``\\\\сервер\\шара``."""
    return path.startswith("\\\\") or (os.name == "nt" and path.startswith("//"))


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def is_network_path(path: str, settings: Settings) -> bool:
    """Лежит ли путь на сетевом хранилище.

    Три признака, и все нужны. UNC — потому что вписать ``\\\\сервер\\шара`` в
    поле пути руками можно и не заглядывая в список корней, и обходить этим
    замок нельзя. Список корней — потому что его ведёт человек. Тип диска —
    потому что подключённая буква ``Z:`` внешне ничем не отличается от локальной.
    """
    if not path:
        return False
    if is_unc(path):
        return True

    target = _norm(path)
    for root in settings.network_roots:
        root_norm = _norm(root.path)
        if target == root_norm or target.startswith(root_norm.rstrip(os.sep) + os.sep):
            return True

    drive = os.path.splitdrive(target)[0]
    return bool(drive) and _drive_type(drive) == _DRIVE_REMOTE
