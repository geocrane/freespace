"""Платформозависимые утилиты: пути кэша, размеры, открытие в проводнике."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass


def cache_dir() -> str:
    """Каталог для хранения кэша приложения, специфичный для ОС.

    ``FREESPACE_CACHE_DIR`` перекрывает выбор: в контейнере домашний каталог
    может быть смонтирован только на чтение или лежать не там, где ожидается.
    """
    override = os.environ.get("FREESPACE_CACHE_DIR")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        os.makedirs(path, exist_ok=True)
        return path
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "FreeSpace")
    elif sys.platform == "darwin":
        path = os.path.expanduser("~/Library/Application Support/FreeSpace")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        path = os.path.join(base, "freespace")
    os.makedirs(path, exist_ok=True)
    return path


_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(num: int | float) -> str:
    """Человекочитаемый размер, например 1.5 GB."""
    size = float(num)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


@dataclass
class VolumeInfo:
    """Том или каталог с местом на диске."""

    path: str
    label: str
    total: int
    used: int
    free: int
    # Домашний каталог: там документы, загрузки и рабочие файлы, и туда у
    # пользователя точно есть доступ. Обычно искать место нужно именно там, а не
    # по всему диску, где половина принадлежит системе.
    is_home: bool = False


def disk_usage(path: str) -> VolumeInfo | None:
    """Занятое и свободное место для пути. ``None``, если путь недоступен."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    label = os.path.basename(path.rstrip(os.sep)) or path
    return VolumeInfo(
        path=path, label=label, total=usage.total, used=usage.used, free=usage.free
    )


# Суффикс каталога с пользовательскими данными в целевом Linux-контейнере.
# Всё остальное, что там смонтировано, — системное и недоступно на чтение.
CONTAINER_ROOT_SUFFIX = "/nfs"


def list_volumes() -> list[VolumeInfo]:
    """Кандидаты в корни сканирования: тома и домашний каталог.

    В контейнере штатных «дисков» нет, поэтому список строится из того, что
    реально смонтировано и доступно на чтение.
    """
    override = os.environ.get("FREESPACE_ROOTS")
    if override:
        return _describe(override.split(os.pathsep))

    # Домашний каталог первым на всех системах: на Windows его в списке не было
    # вовсе, предлагались только буквы дисков — а начинать разбор почти всегда
    # нужно со своих файлов.
    candidates: list[str] = [os.path.expanduser("~")]
    if os.name == "nt":
        import string

        candidates += [f"{letter}:\\" for letter in string.ascii_uppercase
                       if os.path.exists(f"{letter}:\\")]
    else:
        if sys.platform == "darwin":
            try:
                candidates += [os.path.join("/Volumes", n)
                               for n in sorted(os.listdir("/Volumes"))]
            except OSError:
                pass
        else:
            mounts = _linux_mountpoints()
            # В контейнере пользовательские данные лежат на монтировании,
            # оканчивающемся на /nfs, а рядом висят десятки системных путей, до
            # которых доступа нет. Если такое монтирование найдено — предлагаем
            # только его; если нет (обычный Linux с графикой) — прежний список.
            nfs = [m for m in mounts if m.rstrip(os.sep).endswith(CONTAINER_ROOT_SUFFIX)]
            if nfs:
                return _describe(nfs)
            candidates += mounts
        candidates.append("/")

    return _describe(candidates)


def default_root() -> str:
    """Что предложить в поле пути сразу при открытии страницы.

    В целевом Linux-контейнере разбирать нужно монтирование, оканчивающееся на
    ``/nfs``: там лежат данные пользователя. Домашний каталог в контейнере
    служебный — несколько мегабайт настроек, в которых искать нечего, — и
    предлагать его значит заставлять человека каждый раз стирать подставленный
    путь и вписывать нужный руками.

    На остальных системах предлагается домашний каталог: там документы,
    загрузки и рабочие файлы, и туда доступ есть всегда.
    """
    override = os.environ.get("FREESPACE_ROOTS")
    if override:
        chosen = _describe(override.split(os.pathsep))
        if chosen:
            return chosen[0].path

    if os.name != "nt" and sys.platform != "darwin":
        for mount in _linux_mountpoints():
            if (mount.rstrip(os.sep).endswith(CONTAINER_ROOT_SUFFIX)
                    and os.access(mount, os.R_OK)):
                return mount

    return os.path.expanduser("~")


def _describe(paths: list[str]) -> list[VolumeInfo]:
    """Отбросить недоступное и дубли, добрать сведения о свободном месте.

    Дубли ищутся по inode, а не по строке пути: на macOS ``/`` и
    ``/Volumes/Macintosh HD`` — это один и тот же каталог, и предлагать его
    дважды значит звать пользователя сканировать одно и то же.
    """
    home = os.path.realpath(os.path.expanduser("~"))
    volumes: list[VolumeInfo] = []
    seen: set[tuple[int, int]] = set()
    for path in paths:
        norm = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
        if not os.path.isdir(norm) or not os.access(norm, os.R_OK):
            continue
        try:
            stat = os.stat(norm)
        except OSError:
            continue
        key = (stat.st_dev, stat.st_ino)
        if key in seen:
            continue
        seen.add(key)
        info = disk_usage(norm)
        if info is not None and info.total > 0:
            info.is_home = norm == home
            volumes.append(info)
    return volumes



def _linux_mountpoints() -> list[str]:
    """Точки монтирования с настоящими данными, без служебных ФС."""
    pseudo = {
        "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
        "securityfs", "pstore", "bpf", "debugfs", "tracefs", "configfs",
        "fusectl", "hugetlbfs", "mqueue", "binfmt_misc", "autofs", "ramfs",
        "nsfs", "overlay", "squashfs",
    }
    points: list[str] = []
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3 or parts[2] in pseudo:
                    continue
                mountpoint = parts[1].replace("\\040", " ")
                # Отдельные файлы, подмонтированные поверх конфигов, нам не нужны.
                if os.path.isdir(mountpoint):
                    points.append(mountpoint)
    except OSError:
        pass
    return points


def reveal_in_explorer(path: str) -> None:
    """Открыть файл/папку в проводнике ОС, выделив элемент."""
    path = os.path.abspath(path)
    try:
        if os.name == "nt":
            if os.path.isdir(path):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.run(["explorer", "/select,", path], check=False)
        elif sys.platform == "darwin":
            subprocess.run(["open", "-R", path], check=False)
        else:
            target = path if os.path.isdir(path) else os.path.dirname(path)
            subprocess.run(["xdg-open", target], check=False)
    except OSError:
        pass


def open_path(path: str) -> None:
    """Открыть папку/файл штатной программой ОС."""
    path = os.path.abspath(path)
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except OSError:
        pass
