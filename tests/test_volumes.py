"""Тесты списка корней для скана, в том числе поведения в Linux-контейнере."""

from __future__ import annotations

import os
import sys

import pytest

from freespace.core import platform_utils
from freespace.core.platform_utils import cache_dir, default_root, list_volumes


@pytest.fixture
def linux(monkeypatch):
    """Притвориться Linux: ветка контейнера иначе недостижима на macOS."""
    monkeypatch.setattr(platform_utils.sys, "platform", "linux")


def _mounts(tmp_path, *names):
    paths = []
    for name in names:
        path = tmp_path / name.lstrip("/")
        path.mkdir(parents=True, exist_ok=True)
        paths.append(str(path))
    return paths


def test_container_keeps_only_nfs_mount(tmp_path, monkeypatch, linux):
    """В контейнере предлагается только монтирование, оканчивающееся на /nfs."""
    system, data = _mounts(tmp_path, "var/lib/kubelet", "home/user/nfs")
    monkeypatch.setattr(platform_utils, "_linux_mountpoints", lambda: [system, data])

    paths = [v.path for v in list_volumes()]
    assert paths == [data]


def test_plain_linux_keeps_full_list(tmp_path, monkeypatch, linux):
    """Без /nfs список прежний — обычный Linux не должен остаться без вариантов."""
    first, second = _mounts(tmp_path, "mnt/disk1", "mnt/disk2")
    monkeypatch.setattr(platform_utils, "_linux_mountpoints", lambda: [first, second])

    paths = [v.path for v in list_volumes()]
    assert first in paths and second in paths
    assert os.path.expanduser("~") in paths


def test_env_override_wins(tmp_path, monkeypatch):
    """FREESPACE_ROOTS перекрывает автоопределение на любой платформе."""
    first, second = _mounts(tmp_path, "roots/one", "roots/two")
    monkeypatch.setenv("FREESPACE_ROOTS", os.pathsep.join([first, second]))

    assert [v.path for v in list_volumes()] == [first, second]


def test_env_override_skips_unreadable(tmp_path, monkeypatch):
    real, = _mounts(tmp_path, "roots/real")
    monkeypatch.setenv("FREESPACE_ROOTS",
                       os.pathsep.join([str(tmp_path / "нет-такого"), real]))

    assert [v.path for v in list_volumes()] == [real]


def test_container_scans_nfs_by_default(tmp_path, monkeypatch, linux):
    """В контейнере поле пути должно быть заполнено данными, а не ~."""
    system, data = _mounts(tmp_path, "var/lib/kubelet", "home/user/nfs")
    monkeypatch.setattr(platform_utils, "_linux_mountpoints", lambda: [system, data])

    assert default_root() == data


def test_plain_linux_defaults_to_home(tmp_path, monkeypatch, linux):
    first, = _mounts(tmp_path, "mnt/disk1")
    monkeypatch.setattr(platform_utils, "_linux_mountpoints", lambda: [first])

    assert default_root() == os.path.expanduser("~")


def test_default_root_follows_env_override(tmp_path, monkeypatch):
    first, second = _mounts(tmp_path, "roots/one", "roots/two")
    monkeypatch.setenv("FREESPACE_ROOTS", os.pathsep.join([first, second]))

    assert default_root() == first


def test_cache_dir_honours_override(tmp_path, monkeypatch):
    target = tmp_path / "своё" / "место"
    monkeypatch.setenv("FREESPACE_CACHE_DIR", str(target))

    assert cache_dir() == str(target)
    assert target.is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="macOS/Linux")
def test_volumes_are_readable_directories():
    """Что бы ни попало в список, оно существует и читается."""
    for volume in list_volumes():
        assert os.path.isdir(volume.path)
        assert os.access(volume.path, os.R_OK)
        assert volume.total > 0
