"""Замок на сетевые пути: PIN, сессии разблокировки, список сетевых корней.

Сам PIN задаётся в ``.env`` (``core/env.py``), а список сетевых папок ведётся из
интерфейса и лежит в настройках (``core/settings.py``).

Токены живут только в памяти процесса, поэтому перезапуск сервера — это снова
запертая дверь. Так и задумано: приложение запускают на день, и «разблокировано
навсегда» здесь было бы просто выключенным замком.
"""

from __future__ import annotations

import threading
import time
import uuid

from ..core import env
from ..core import settings as settings_module
from ..core.settings import NetworkRoot, Settings

# Сколько живёт разблокировка и как быстро гаснет перебор.
DEFAULT_TTL = 30 * 60.0
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 30.0


class AccessError(Exception):
    """Общий предок отказов замка."""


class LockedError(AccessError):
    """Путь сетевой, а сессия не разблокирована."""


class PinNotSet(AccessError):
    """PIN ещё не задан — разблокировать нечем."""


class WrongPin(AccessError):
    """Комбинация не подошла."""


class TooManyAttempts(AccessError):
    """Слишком много неверных попыток подряд."""


class AccessGuard:
    """Кто и до какого момента может трогать сетевые пути.

    Потокобезопасен: маршруты FastAPI обслуживаются из пула потоков.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        ttl: float = DEFAULT_TTL,
        max_attempts: int = MAX_ATTEMPTS,
        lockout: float = LOCKOUT_SECONDS,
    ) -> None:
        self._settings = settings if settings is not None else settings_module.load()
        self.ttl = ttl
        self.max_attempts = max_attempts
        self.lockout = lockout
        self._lock = threading.Lock()
        self._tokens: dict[str, float] = {}
        self._failures = 0
        self._blocked_until = 0.0

    # --- настройки ---------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def has_pin(self) -> bool:
        """Задан ли PIN в ``.env``. Пустой — сетевые папки не открыть вовсе."""
        return settings_module.has_pin()

    def reload(self) -> None:
        """Перечитать файл настроек с диска."""
        with self._lock:
            self._settings = settings_module.load()

    def _save(self) -> None:
        settings_module.save(self._settings)

    # --- разблокировка -----------------------------------------------------

    def unlock(self, pin: str) -> tuple[str, float]:
        """Проверить PIN и выдать токен. Возвращает (токен, сколько живёт)."""
        with self._lock:
            if not settings_module.has_pin():
                raise PinNotSet(
                    "PIN не задан, поэтому сетевые папки недоступны. Впишите его в "
                    f"строку {env.PIN_VAR}= файла {env.env_path()} и перезапустите "
                    "приложение."
                )
            now = time.monotonic()
            if now < self._blocked_until:
                raise TooManyAttempts(
                    f"Слишком много неверных попыток. Подождите "
                    f"{int(self._blocked_until - now) + 1} с и попробуйте снова."
                )
            if not settings_module.verify_pin(pin):
                self._failures += 1
                if self._failures >= self.max_attempts:
                    self._failures = 0
                    self._blocked_until = now + self.lockout
                raise WrongPin("Неверный PIN.")

            self._failures = 0
            token = uuid.uuid4().hex
            self._tokens[token] = time.monotonic() + self.ttl
            self._sweep_locked()
            return token, self.ttl

    def is_unlocked(self, token: str | None) -> bool:
        """Живой ли токен. Обращение продлевает срок: работа не должна
        обрываться на середине разбора папки."""
        if not token:
            return False
        with self._lock:
            expires = self._tokens.get(token)
            now = time.monotonic()
            if expires is None:
                return False
            if expires < now:
                del self._tokens[token]
                return False
            self._tokens[token] = now + self.ttl
            return True

    def lock(self, token: str | None) -> bool:
        """Забыть токен. ``False``, если его и не было."""
        if not token:
            return False
        with self._lock:
            return self._tokens.pop(token, None) is not None

    def _sweep_locked(self) -> None:
        now = time.monotonic()
        for token, expires in list(self._tokens.items()):
            if expires < now:
                del self._tokens[token]

    # --- проверка пути -----------------------------------------------------

    def is_network(self, path: str) -> bool:
        return settings_module.is_network_path(path, self._settings)

    def check(self, path: str, token: str | None) -> None:
        """Пропустить действие над путём или отказать.

        Локальные пути не трогаем вовсе: замок стоит только на сетевых.
        """
        if not self.is_network(path):
            return
        if self.is_unlocked(token):
            return
        raise LockedError(
            "Это сетевая папка — общий диск. Работа с ней открывается по PIN: "
            "нажмите «Сетевые диски» в шапке и введите комбинацию."
        )

    # --- список сетевых корней ---------------------------------------------

    @property
    def roots(self) -> list[NetworkRoot]:
        return list(self._settings.network_roots)

    def add_root(self, path: str, label: str = "") -> NetworkRoot:
        """Добавить сетевой корень в список. Повтор просто обновляет подпись."""
        path = path.rstrip("/\\") or path
        with self._lock:
            for root in self._settings.network_roots:
                if root.path.lower() == path.lower():
                    root.label = label or root.label
                    self._save()
                    return root
            root = NetworkRoot(path=path, label=label)
            self._settings.network_roots.append(root)
            self._save()
            return root

    def remove_root(self, path: str) -> bool:
        """Убрать корень из списка. Файлы на шаре при этом не трогаются."""
        with self._lock:
            before = len(self._settings.network_roots)
            self._settings.network_roots = [
                r for r in self._settings.network_roots
                if r.path.lower() != path.rstrip("/\\").lower()
            ]
            if len(self._settings.network_roots) == before:
                return False
            self._save()
            return True
