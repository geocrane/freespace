"""Тесты защиты от повторного входа в один и тот же каталог.

Каталог бывает виден под несколькими путями: firmlinks на macOS
(``/opt/homebrew`` и ``/System/Volumes/Data/opt/homebrew`` — один inode),
bind-mount на Linux, junction на Windows. Проверка по ``st_dev`` тут бесполезна:
устройство у них одно. Без защиты дерево обходится по нескольку раз, размеры
удваиваются, а каждая папка появляется в поиске дважды.
"""

from __future__ import annotations

import os

from freespace.core.scanner import SIZE_APPARENT, Scanner, scan


def test_same_directory_under_two_names_counted_once(tmp_path, monkeypatch):
    """Второй путь к тому же inode пропускается, а не обходится заново."""
    root = tmp_path / "root"
    (root / "настоящая").mkdir(parents=True)
    (root / "двойник").mkdir()
    (root / "настоящая" / "data.bin").write_bytes(b"x" * 1000)
    (root / "двойник" / "data.bin").write_bytes(b"x" * 1000)

    real_stat = os.DirEntry.stat
    twin = str(root / "двойник")
    original = os.stat(root / "настоящая")

    def fake_stat(self, *, follow_symlinks=True):
        info = real_stat(self, follow_symlinks=follow_symlinks)
        if self.path == twin:
            # Тот же inode, что и у «настоящей»: так выглядит firmlink.
            return type("S", (), {
                **{k: getattr(info, k) for k in dir(info) if k.startswith("st_")},
                "st_ino": original.st_ino,
                "st_dev": original.st_dev,
            })()
        return info

    monkeypatch.setattr(os.DirEntry, "stat", fake_stat, raising=False)
    result = scan(str(root), size_mode=SIZE_APPARENT)

    assert result.root.size == 1000, "содержимое посчитано один раз, а не дважды"
    # Какой из двух путей окажется первым, зависит от порядка os.scandir;
    # важно, что ровно один из них обойдён, а второй отмечен как повтор.
    assert len(result.aliases) == 1
    assert result.aliases[0] in {twin, str(root / "настоящая")}


def test_without_inode_protection_is_off(tmp_path):
    """Где inode не сообщается, лучше посчитать дважды, чем пропустить своё."""
    scanner = Scanner()

    class NoInode:
        st_dev, st_ino = 1, 0

    assert scanner._mark_visited(NoInode()) is True
    assert scanner._mark_visited(NoInode()) is True


def test_root_is_canonical(tmp_path):
    """Корень приводится к единственному написанию.

    Иначе одно дерево открывается под разными именами и даёт два несовместимых
    набора путей — именно из-за этого удаление находило половину объектов
    отсутствующими.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "f.bin").write_bytes(b"x" * 10)
    link = tmp_path / "ссылка"
    os.symlink(root, link)

    result = scan(str(link), size_mode=SIZE_APPARENT)

    assert result.root.path == os.path.realpath(str(root))


def test_visited_marks_are_reset_between_scans(tmp_path):
    """Второй скан тем же сканером не должен считать всё повтором."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "sub" / "f.bin").write_bytes(b"x" * 500)

    scanner = Scanner(size_mode=SIZE_APPARENT)
    first = scanner.scan(str(root))
    second = scanner.scan(str(root))

    assert first.root.size == second.root.size == 500
    assert second.aliases == []
