"""Тесты защиты обхода: границы файловых систем и служебные каталоги."""

from __future__ import annotations

import os

import pytest

from freespace.core import scanner as scanner_module
from freespace.core.scanner import SIZE_APPARENT, Scanner, scan


def test_pseudo_dirs_are_not_entered(tmp_path, monkeypatch):
    """Каталог из чёрного списка не обходится и попадает в boundaries."""
    root = tmp_path / "root"
    fake_proc = root / "proc"
    fake_proc.mkdir(parents=True)
    (fake_proc / "huge").write_bytes(b"x" * 9999)
    (root / "real.txt").write_bytes(b"x" * 100)

    monkeypatch.setattr(scanner_module, "PSEUDO_DIRS", frozenset({str(fake_proc)}))

    result = scan(str(root), size_mode=SIZE_APPARENT)
    assert result.root.size == 100, "содержимое псевдо-ФС не должно попадать в размер"
    assert str(fake_proc) in result.boundaries


def test_boundary_is_reported_not_skipped(tmp_path, monkeypatch):
    """Граница — это не ошибка чтения, она учитывается отдельно от skipped."""
    root = tmp_path / "root"
    (root / "sys").mkdir(parents=True)
    monkeypatch.setattr(scanner_module, "PSEUDO_DIRS", frozenset({str(root / "sys")}))

    result = scan(str(root), size_mode=SIZE_APPARENT)
    assert result.boundaries == [str(root / "sys")]
    assert result.skipped == []


def test_other_filesystem_is_not_crossed(tmp_path, monkeypatch):
    """Каталог на другом устройстве пропускается: st_dev не совпадает с корнем."""
    root = tmp_path / "root"
    mounted = root / "mounted"
    mounted.mkdir(parents=True)
    (mounted / "data.bin").write_bytes(b"x" * 5000)
    (root / "own.txt").write_bytes(b"x" * 70)

    real_stat = os.DirEntry.stat

    def fake_stat(self, *, follow_symlinks=True):
        info = real_stat(self, follow_symlinks=follow_symlinks)
        if self.path == str(mounted):
            # Подменяем только устройство, остальное настоящее.
            return type("S", (), {**{k: getattr(info, k) for k in dir(info)
                                     if k.startswith("st_")}, "st_dev": info.st_dev + 1})()
        return info

    monkeypatch.setattr(os.DirEntry, "stat", fake_stat, raising=False)

    result = scan(str(root), size_mode=SIZE_APPARENT)
    assert result.root.size == 70
    assert str(mounted) in result.boundaries


def test_cross_filesystems_flag_allows_crossing(tmp_path):
    """С явным флагом обход границы разрешён."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.bin").write_bytes(b"x" * 300)

    result = Scanner(cross_filesystems=True, size_mode=SIZE_APPARENT).scan(str(root))
    assert result.root.size == 300
    assert result.boundaries == []


def test_cancel_returns_partial_tree(tmp_path):
    """Отмена отдаёт то, что успели собрать, а не пустоту.

    Флаг отмены сбрасывается в начале ``scan()``, поэтому отменяем изнутри —
    из колбэка прогресса, как это делает интерфейс.
    """
    root = tmp_path / "root"
    for i in range(40):
        sub = root / f"dir{i:02d}"
        sub.mkdir(parents=True)
        (sub / "f.bin").write_bytes(b"x" * 100)

    scanner = Scanner(progress_every=1, size_mode=SIZE_APPARENT)
    scanner.on_progress = lambda count, path: scanner.cancel() if count >= 3 else None

    result = scanner.scan(str(root))

    assert scanner.cancelled
    assert result.root is not None, "дерево должно вернуться даже при отмене"
    assert result.root.size < 4000, "обход должен был прерваться, а не дойти до конца"


@pytest.mark.skipif(os.name == "nt", reason="права POSIX")
def test_unreadable_dir_is_skipped_not_fatal(tmp_path):
    root = tmp_path / "root"
    locked = root / "locked"
    locked.mkdir(parents=True)
    (locked / "f.bin").write_bytes(b"x" * 100)
    (root / "ok.txt").write_bytes(b"x" * 40)
    os.chmod(locked, 0o000)
    try:
        result = scan(str(root), size_mode=SIZE_APPARENT)
        assert result.root.size == 40
        assert any("locked" in p for p in result.skipped)
    finally:
        os.chmod(locked, 0o755)


def test_missing_device_info_is_not_a_boundary(tmp_path, monkeypatch):
    """Ноль в st_dev значит «сведений нет», а не «другое устройство».

    Так ведёт себя Windows: перечисление каталога не приносит ни st_dev, ни
    st_ino. Пока это считалось признаком чужого тома, скан C: обрывался на
    первом уровне — находились только файлы в корне вроде pagefile.sys, а все
    папки объявлялись границей.
    """
    root = tmp_path / "root"
    (root / "папка" / "глубже").mkdir(parents=True)
    (root / "папка" / "глубже" / "data.bin").write_bytes(b"x" * 3000)
    (root / "в-корне.bin").write_bytes(b"x" * 100)

    real_stat = os.DirEntry.stat

    def windows_like_stat(self, *, follow_symlinks=True):
        info = real_stat(self, follow_symlinks=follow_symlinks)
        fields = {k: getattr(info, k) for k in dir(info) if k.startswith("st_")}
        # Как на Windows: устройство и inode из перечисления не приходят.
        fields["st_dev"] = 0
        fields["st_ino"] = 0
        return type("S", (), fields)()

    monkeypatch.setattr(os.DirEntry, "stat", windows_like_stat, raising=False)
    result = scan(str(root), size_mode=SIZE_APPARENT)

    assert result.root.size == 3100, "обход должен зайти во вложенные папки"
    assert result.boundaries == []


def test_real_other_device_is_still_a_boundary(tmp_path, monkeypatch):
    """Настоящий чужой том по-прежнему отсекается."""
    root = tmp_path / "root"
    mounted = root / "чужой-том"
    mounted.mkdir(parents=True)
    (mounted / "data.bin").write_bytes(b"x" * 5000)
    (root / "свой.bin").write_bytes(b"x" * 70)

    real_stat = os.DirEntry.stat

    def fake_stat(self, *, follow_symlinks=True):
        info = real_stat(self, follow_symlinks=follow_symlinks)
        if self.path == str(mounted):
            return type("S", (), {**{k: getattr(info, k) for k in dir(info)
                                     if k.startswith("st_")},
                                  "st_dev": info.st_dev + 1})()
        return info

    monkeypatch.setattr(os.DirEntry, "stat", fake_stat, raising=False)
    result = scan(str(root), size_mode=SIZE_APPARENT)

    assert result.root.size == 70
    assert result.boundaries == [str(mounted)]
