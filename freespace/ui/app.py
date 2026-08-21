"""Точка входа GUI-приложения FreeSpace."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("FreeSpace")
    window = MainWindow()
    window.show()
    # Если путь передан аргументом — сразу открыть его.
    if len(sys.argv) > 1:
        window.open_folder(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
