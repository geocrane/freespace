"""Ход долгих операций: удаление и очистка корзины.

Перенос в корзину мгновенным только выглядит: сам ``os.rename`` занимает
миллисекунды, но перед ним нужно посчитать размер поддерева, а это обход всех
файлов внутри. На папке с сотней тысяч мелочи ожидание измеряется десятками
секунд, и всё это время интерфейс молчал — пользователь успевал уйти в другую
папку, нажать удаление ещё раз и решить, что ничего не работает. Очистка
корзины и вовсе стирает файлы по-настоящему, тут ждать приходится всегда.

Сами операции остаются синхронными: POST возвращает готовый результат со всеми
подсчётами. Ход работы публикуется отдельно, под токеном, который браузер
придумывает сам и кладёт в тело запроса. Тогда страница опрашивает
``/api/progress/<токен>`` параллельно с ещё висящим запросом — и не нужно ни
очереди задач, ни отдельного маршрута «запустить и забыть», ни разбора того,
кому принадлежит незавершённая задача после перезагрузки страницы.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

RUNNING = "running"
DONE = "done"
ERROR = "error"
# Токена никто не регистрировал — либо опрос обогнал запрос, либо операция уже
# забыта. Для страницы это не ошибка: она просто спросит ещё раз.
UNKNOWN = "unknown"


@dataclass
class Operation:
    """Что происходит прямо сейчас с одной долгой операцией."""

    id: str
    kind: str
    state: str = RUNNING
    # Сколько объектов всего и сколько уже пройдено. 0 в ``total`` — счёт ещё
    # не составлен, полосе нечего показывать, кроме самого факта работы.
    total: int = 0
    done: int = 0
    # Байты известны заранее только там, где размеры уже записаны (корзина).
    total_bytes: int = 0
    freed: int = 0
    current: str = ""
    # Файлов пройдено внутри текущего объекта. Единственный признак жизни на
    # одной большой папке: объект один, а обход его содержимого — долгий.
    counted: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str = ""

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at


class Progress:
    """Приёмник сообщений о ходе работы. Этот — молчит.

    Служебный код зовёт методы, не выясняя, следит ли кто-нибудь за операцией:
    пустой объект дешевле, чем ``if progress is not None`` в семи местах.
    """

    def plan(self, total: int, total_bytes: int = 0) -> None:
        """Объявить объём работы."""

    def step(self, path: str) -> None:
        """Взялись за очередной объект."""

    def count(self, files: int) -> None:
        """Внутри текущего объекта пройдено столько файлов."""

    def advance(self, freed: int = 0) -> None:
        """Объект закончен — удачно или нет."""

    def finish(self, state: str = DONE, error: str = "") -> None:
        """Операция закончилась."""


SILENT = Progress()


class Reporter(Progress):
    """Progress, который действительно пишет в общую запись операции."""

    def __init__(self, operation: Operation, lock: threading.Lock) -> None:
        self._op = operation
        self._lock = lock

    def plan(self, total: int, total_bytes: int = 0) -> None:
        with self._lock:
            self._op.total = total
            self._op.total_bytes = total_bytes

    def step(self, path: str) -> None:
        with self._lock:
            self._op.current = path
            self._op.counted = 0

    def count(self, files: int) -> None:
        with self._lock:
            self._op.counted = files

    def advance(self, freed: int = 0) -> None:
        with self._lock:
            self._op.done += 1
            self._op.freed += freed
            self._op.counted = 0

    def finish(self, state: str = DONE, error: str = "") -> None:
        with self._lock:
            # Повторный вызов ничего не портит: обработчик завершает операцию
            # сам, а исключение по дороге — ещё раз, уже с причиной.
            if self._op.state != RUNNING:
                return
            self._op.state = state
            self._op.error = error
            self._op.finished_at = time.time()
            self._op.current = ""


class Operations:
    """Реестр операций, за которыми можно наблюдать по токену.

    Записи живут недолго и только ради опроса: последний ответ должен успеть
    дойти до страницы, дальше запись — мусор. Поэтому храним последние
    ``keep`` штук и выбрасываем самые старые завершённые.
    """

    def __init__(self, keep: int = 32) -> None:
        self._ops: dict[str, Operation] = {}
        self._lock = threading.Lock()
        self._keep = keep

    def open(self, token: str, kind: str) -> Progress:
        """Начать наблюдаемую операцию. Без токена — молчаливая заглушка."""
        token = (token or "").strip()[:64]
        if not token:
            return SILENT
        operation = Operation(id=token, kind=kind)
        with self._lock:
            self._evict_locked()
            self._ops[token] = operation
        return Reporter(operation, self._lock)

    def get(self, token: str) -> Operation | None:
        with self._lock:
            return self._ops.get(token)

    def snapshot(self, token: str) -> dict:
        """Состояние в виде, пригодном для опроса из браузера."""
        operation = self.get(token)
        if operation is None:
            return {"id": token, "state": UNKNOWN}
        with self._lock:
            return {
                "id": operation.id,
                "kind": operation.kind,
                "state": operation.state,
                "total": operation.total,
                "done": operation.done,
                "total_bytes": operation.total_bytes,
                "freed": operation.freed,
                "current": operation.current,
                "counted": operation.counted,
                "elapsed": round(operation.elapsed, 2),
                "error": operation.error,
            }

    def _evict_locked(self) -> None:
        if len(self._ops) < self._keep:
            return
        finished = [op for op in self._ops.values() if op.state != RUNNING]
        finished.sort(key=lambda op: op.finished_at or 0)
        while finished and len(self._ops) >= self._keep:
            self._ops.pop(finished.pop(0).id, None)


@contextmanager
def track(operations: Operations, token: str, kind: str) -> Iterator[Progress]:
    """Провести операцию под наблюдением, чем бы она ни кончилась.

    Незакрытая операция — это полоса, которая крутится вечно: страница видит
    ``running`` и ждёт. Поэтому состояние выставляется и на исключении тоже,
    причём до того, как оно уйдёт наверх и превратится в HTTP-ответ.
    """
    progress = operations.open(token, kind)
    try:
        yield progress
    except Exception as exc:  # noqa: BLE001 — причина уходит наблюдателю как есть
        progress.finish(ERROR, str(exc))
        raise
    progress.finish()


__all__ = ["DONE", "ERROR", "RUNNING", "SILENT", "UNKNOWN", "Operation",
           "Operations", "Progress", "Reporter", "track"]
