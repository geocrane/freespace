"""Фоновое сканирование в отдельном потоке Qt."""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal

from ..core.scanner import ScanCancelled, Scanner


class ScanSignals(QObject):
    progress = Signal(int, str)          # (кол-во обработанных, текущий путь)
    finished = Signal(object, list)      # (корневой FileNode, список пропущенных)
    error = Signal(str)
    cancelled = Signal()


class ScanWorker(QRunnable):
    """Запускает сканирование пути в пуле потоков Qt."""

    def __init__(self, root_path: str) -> None:
        super().__init__()
        self.root_path = root_path
        self.signals = ScanSignals()
        # Прогресс прокидываем через сигнал (потокобезопасно для Qt).
        self._scanner = Scanner(on_progress=self._on_progress)

    def _on_progress(self, count: int, path: str) -> None:
        self.signals.progress.emit(count, path)

    def cancel(self) -> None:
        self._scanner.cancel()

    def run(self) -> None:
        try:
            result = self._scanner.scan(self.root_path)
            if self._scanner.cancelled:
                self.signals.cancelled.emit()
            else:
                self.signals.finished.emit(result.root, result.skipped)
        except ScanCancelled:
            self.signals.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 — показываем пользователю любую ошибку
            self.signals.error.emit(str(exc))
