"""Запуск FreeSpace из тетрадки Jupyter.

Тетрадка `start.ipynb` тиражируется на множество пользователей, и код в её
ячейках им только мешает: читать его никто не будет, а испортить при
выполнении — легко. Поэтому вся работа живёт здесь, а тетрадке остаются две
строки: ``start(TOKEN)`` и ``stop()``.

    from freespace.notebook import start, stop

    start(TOKEN)   # поднять сервер и показать приложение прямо под ячейкой
    stop()         # погасить его

Функции рассчитаны на контейнер DataLab: наружу, на PyPI, оттуда хода нет,
зависимости ставятся из индекса портала по токену SberOSC, а браузер видит
сервер не напрямую, а через ``jupyter-server-proxy``.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Iterable
from pathlib import Path

# Внешний адрес контейнера: именно на него смотрит браузер, тогда как сервер
# слушает петлевой адрес внутри пода.
DOMAIN = "https://jupyterhub-datalab.apps.prom-datalab.ca.sbrf.ru"

# Индекс пакетов портала. Обычный `pip install` из контейнера не работает:
# наружу, на PyPI, хода нет, и ставить можно только отсюда — по токену.
INDEX_HOST = "sberosc.ca.sbrf.ru"
INDEX_PATH = "/repo/pypi/simple"

STATE_NAME = ".freespace-server.json"
LOG_NAME = ".freespace-server.log"
PIP_LOG_NAME = ".freespace-pip.log"

# Сколько ждать pip целиком. Индекс портала иногда отвечает по капле, и ячейка,
# висящая до конца сессии, — худший из исходов: лучше внятная ошибка.
PIP_TIMEOUT = 1800

ACCENT = "#4F86C6"


# --- окружение ------------------------------------------------------------


def find_project_root(start: Path | None = None) -> Path:
    """Каталог, в котором лежит пакет freespace: тетрадку могли открыть откуда угодно."""
    start = (start or Path.cwd()).resolve()
    for candidate in [start, *start.parents]:
        if (candidate / "freespace" / "web" / "api.py").exists():
            return candidate
    # Пакет импортировался, значит он где-то есть: freespace/notebook.py → корень.
    return Path(__file__).resolve().parent.parent


def state_path(root: Path | None = None) -> Path:
    return (root or find_project_root()) / STATE_NAME


def free_port(preferred: int = 8000, tries: int = 50) -> int:
    for port in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError(f"нет свободного порта в диапазоне {preferred}..{preferred + tries - 1}")


def proxy_prefix() -> str:
    """Префикс URL, который jupyter-server-proxy ожидает увидеть."""
    prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX") or os.environ.get("NB_PREFIX")
    if prefix:
        return prefix if prefix.endswith("/") else prefix + "/"
    user = os.environ.get("JUPYTERHUB_USER")
    return f"/user/{user}/" if user else ""


# --- зависимости ----------------------------------------------------------

#: Зависимости витрины: спецификация для pip → модуль, по которому видно, что
#: пакет на месте. Проверять надо именно импортом: pip случается, что возвращает
#: ноль, положив пакет туда, откуда сервер его не увидит.
PORTAL_DEPS = {"fastapi>=0.110": "fastapi", "uvicorn>=0.27": "uvicorn"}

# Строки pip, по которым видно движение. Всё остальное в его выводе — шум,
# ради которого никто ячейку листать не станет.
_RE_COLLECTING = re.compile(r"^\s*Collecting\s+([A-Za-z0-9._-]+)")
_RE_SATISFIED = re.compile(r"^\s*Requirement already satisfied:\s+([A-Za-z0-9._-]+)")
# `Using cached fastapi-0.141.1-py3-none-any.whl (131 kB)` — колесо приехало.
# Строку `...whl.metadata (27 kB)` считать нельзя: это разведка перед скачиванием,
# и она приходит на каждый пакет отдельно.
_RE_FETCHED = re.compile(
    r"^\s*(?:Downloading|Using cached)\s+([A-Za-z0-9._-]+?)-\d\S*?"
    r"\.(?:whl|tar\.gz|zip)(?=\s|$)"
)
_RE_INSTALLING = re.compile(r"^\s*Installing collected packages:\s*(.+)")
_RE_INSTALLED = re.compile(r"^\s*Successfully installed\s+(.+)")

#: Частые причины отказа. Вывод pip длинный и английский, а в тетрадке нужна
#: одна русская фраза о том, что делать дальше.
_PIP_HINTS = (
    (("401 client error", "403 client error", "401 unauthorized", "403 forbidden",
      "authentication failed", "invalid credentials", "bad credentials"),
     "Похоже на отказ по токену: выпустите новый в SberOSC и впишите его в «Шаг 1»."),
    (("externally-managed-environment",),
     "Интерпретатор закрыт для установки пакетов (PEP 668): повторите команду вручную "
     "с флагом --break-system-packages или соберите venv."),
    (("no matching distribution", "could not find a version"),
     "Индекс портала не отдал пакет нужной версии — проверьте имя и ограничение версии."),
    (("temporary failure in name resolution", "failed to establish a new connection",
      "connection refused", "connection reset", "read timed out", "proxyerror",
      "network is unreachable", "certificate verify failed", "sslerror"),
     f"Похоже на обрыв связи с индексом: проверьте доступность {INDEX_HOST} из контейнера."),
    (("no space left on device", "disk quota exceeded"),
     "На диске кончилось место — освободите его и повторите."),
)


def _canonical(name: str) -> str:
    """`typing_extensions` и `typing-extensions` — один пакет, а пишет их pip по-разному."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _pip_env(token: str) -> dict:
    """Окружение для pip: там же едет и токен.

    В argv токену не место — командную строку чужого процесса в контейнере
    видно через ``ps``, а вывод ячейки вдобавок сохраняется в самом .ipynb.
    """
    env = dict(os.environ)
    env.update({
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        # Индекс портала бывает капризен: пять попыток на пакет вместо отказа
        # с первого обрыва, но и висеть на одном соединении дольше минуты незачем.
        "PIP_RETRIES": "5",
        "PIP_TIMEOUT": "60",
        # Буферизованный вывод дошёл бы до нас разом в конце, и прогресс-бар
        # прыгнул бы с нуля сразу к сотне.
        "PYTHONUNBUFFERED": "1",
    })
    if token:
        env["PIP_INDEX_URL"] = f"https://token:{token}@{INDEX_HOST}{INDEX_PATH}"
        env["PIP_TRUSTED_HOST"] = INDEX_HOST
    return env


def missing_modules(modules: Iterable[str]) -> list[str]:
    """Каких модулей не хватает интерпретатору, которым будет запущен сервер.

    Спрашиваем отдельный процесс, а не свой ``import``: сервер живёт своим
    процессом, и важно, что видит он, а не прогретые пути импорта ядра, куда
    свежепоставленный пакет мог и не попасть.
    """
    modules = list(modules)
    if not modules:
        return []
    try:
        probe = subprocess.run([sys.executable, "-c", "import " + ", ".join(modules)],
                               capture_output=True, text=True, timeout=120)
        if probe.returncode == 0:
            return []
        missing = []
        for module in modules:
            one = subprocess.run([sys.executable, "-c", f"import {module}"],
                                 capture_output=True, text=True, timeout=120)
            if one.returncode != 0:
                missing.append(module)
        return missing
    except (OSError, subprocess.SubprocessError):
        # Дочерний питон не запустился — не повод разваливаться: смотрим сами,
        # пусть и глазами уже загруженного процесса.
        return [module for module in modules if not _importable(module)]


def _importable(module: str) -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec(module) is not None
    except Exception:      # битые пакеты умеют ронять сам поиск
        return False


def _refresh_user_site() -> None:
    """Показать ядру то, что pip только что положил в ``--user``.

    Пути импорта прогреты при старте ядра, и без этого свежий пакет виден
    только после перезапуска — а перезапускать ядро посреди работы никто не
    станет.
    """
    try:
        import importlib
        import site
        path = site.getusersitepackages()
        if isinstance(path, str) and os.path.isdir(path) and path not in sys.path:
            sys.path.append(path)
        importlib.invalidate_caches()
    except Exception:
        pass


def _hint(log: list[str]) -> str:
    """Короткая подсказка по хвосту вывода pip — или пусто, если причина незнакомая."""
    tail = "\n".join(log[-40:]).lower()
    for markers, advice in _PIP_HINTS:
        if any(marker in tail for marker in markers):
            return advice
    return ""


class _Progress:
    """Одна живая строка вместо простыни вывода pip.

    Берёт tqdm, если он в окружении есть: в тетрадке это привычный вид
    прогресса. Контейнер портала бывает голым, поэтому есть и запасной путь —
    та же строка, нарисованная руками; `\\r` перерисовывает её и в тетрадке,
    и в терминале.
    """

    WIDTH = 24

    def __init__(self, title: str) -> None:
        self.title = title
        self.done = 0
        self.total = 0
        self.note = ""
        self._erase = 0          # длина прошлой строки: её нужно затереть
        self._bar = None
        try:
            # Без ipywidgets tqdm ворчит про откат на текстовый вид — он-то нам
            # и нужен, так что предупреждение только пугает пользователя.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from tqdm.auto import tqdm
                self._bar = tqdm(desc=title, unit=" пакет", leave=True)
        except Exception:        # нет tqdm, сломанный ipywidgets — рисуем сами
            self._bar = None
        self._draw()

    def update(self, *, done: int | None = None, total: int | None = None,
               note: str | None = None) -> None:
        if done is not None:
            self.done = done
        if total is not None:
            self.total = max(total, self.done)
        if note is not None:
            self.note = note
        self._draw()

    def close(self) -> None:
        try:
            if self._bar is not None:
                self._bar.close()
            elif self._erase:
                sys.stdout.write("\n")
                sys.stdout.flush()
        except Exception:        # закрытый stdout не должен ронять установку
            pass
        self._erase = 0

    def _draw(self) -> None:
        try:
            if self._bar is not None:
                self._bar.total = self.total or None
                self._bar.n = self.done
                self._bar.set_postfix_str(self.note, refresh=False)
                self._bar.refresh()
                return
            if self.total:
                filled = round(self.WIDTH * self.done / self.total)
                gauge = "█" * filled + "░" * (self.WIDTH - filled)
                line = f"{self.title}: |{gauge}| {self.done}/{self.total} {self.note}"
            else:
                line = f"{self.title}: {self.note}"
            sys.stdout.write("\r" + line.ljust(self._erase))
            sys.stdout.flush()
            self._erase = len(line)
        except Exception:        # рисование — не то, ради чего стоит падать
            self._bar = None


def _follow_pip(lines: Iterable[str], progress: _Progress, sink=None) -> list[str]:
    """Свернуть вывод pip в прогресс-бар, вернув его целиком — для разбора ошибок.

    Сколько всего будет пакетов, pip заранее не знает и не говорит: зависимости
    он выясняет по ходу дела. Поэтому знаменатель растёт вместе с числом
    найденных пакетов, а числитель считает приехавшие.
    """
    log: list[str] = []
    known: set[str] = set()
    ready: set[str] = set()

    for raw in lines:
        line = raw.rstrip("\n")
        log.append(line)
        if sink is not None:
            try:
                sink.write(raw if raw.endswith("\n") else raw + "\n")
                sink.flush()     # чтобы лог был полезен и на зависшей установке
            except OSError:
                sink = None

        if match := _RE_COLLECTING.match(line):
            name = _canonical(match.group(1))
            known.add(name)
            progress.update(total=len(known), note=name)
        elif match := _RE_SATISFIED.match(line):
            name = _canonical(match.group(1))
            known.add(name)
            ready.add(name)
            progress.update(done=len(ready), total=len(known), note=f"{name} уже есть")
        elif match := _RE_FETCHED.match(line):
            name = _canonical(match.group(1))
            known.add(name)
            ready.add(name)
            progress.update(done=len(ready), total=len(known), note=name)
        elif match := _RE_INSTALLING.match(line):
            names = {_canonical(part) for part in match.group(1).split(",") if part.strip()}
            known |= names
            progress.update(total=len(known), note="устанавливаю")
        elif match := _RE_INSTALLED.match(line):
            # Здесь pip перечисляет всё поставленное с версиями — это и есть
            # итог, по нему и выравниваем счётчик.
            names = match.group(1).split()
            progress.update(done=max(len(names), len(ready)),
                            total=max(len(names), len(known)), note="готово")

    return log


def _terminate(process: subprocess.Popen) -> None:
    """Погасить процесс: сначала вежливо, через пять секунд — насмерть."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
    except OSError:
        pass


def _run_pip(command: list[str], env: dict, progress: _Progress, sink=None,
             timeout: float = PIP_TIMEOUT) -> tuple[int, list[str], bool]:
    """Выполнить pip. Вернуть код возврата, полный вывод и признак таймаута.

    Дедлайн сторожит отдельный поток: чтение вывода блокирует, и молчащий pip
    иначе держал бы ячейку столько, сколько ему вздумается, — а ячейка, висящая
    до конца сессии, хуже внятной ошибки.
    """
    pip = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace", bufsize=1,
                           env=env)
    expired = threading.Event()

    def _expire() -> None:
        expired.set()
        _terminate(pip)

    watchdog = threading.Timer(timeout, _expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        log = _follow_pip(pip.stdout, progress, sink)
        code = pip.wait()
    except BaseException:
        # Ячейку прервали — pip не должен пережить её и дальше грызть индекс.
        _terminate(pip)
        raise
    finally:
        watchdog.cancel()
        if pip.stdout is not None:
            try:
                pip.stdout.close()
            except OSError:
                pass
    return code, log, expired.is_set()


def _open_pip_log(header: str):
    """Файл для полного вывода pip. Вернёт (путь, файл) или (None, None)."""
    try:
        path = find_project_root() / PIP_LOG_NAME
        # Лог копится от запуска к запуску; мегабайта истории хватит с избытком.
        if path.exists() and path.stat().st_size > 1_000_000:
            path.unlink()
        sink = path.open("a", encoding="utf-8")
        sink.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {header} ===\n")
        return path, sink
    except OSError:
        return None, None       # каталог только на чтение — обойдёмся без лога


def install(packages: list[str], token: str, *, modules: Iterable[str] = (),
            timeout: float = PIP_TIMEOUT) -> None:
    """Поставить пакеты из индекса портала.

    Вывод pip не глушится совсем — установка идёт минуту-другую, и молчащая
    ячейка в это время неотличима от зависшей, — но и полусотней строк
    «Collecting …» тетрадку не заваливает: они сворачиваются в один
    прогресс-бар, а целиком уходят в ``.freespace-pip.log``.

    Успех проверяется не кодом возврата, а тем, импортируются ли ``modules``:
    pip умеет и завершиться нулём, положив пакет мимо, и вернуть ошибку из-за
    постороннего конфликта, поставив всё нужное.
    """
    if not packages:
        return

    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
               # Свой бар pip рисует каретками поверх строки — в перехваченном
               # выводе от него одна каша. Спрашивать пароль в тетрадке тоже
               # некому: без --no-input pip будет ждать ответа молча и вечно.
               "--progress-bar", "off", "--no-input"]
    # В контейнере ядро работает от системного питона, куда без --user не
    # записать; внутри venv тот же флаг, наоборот, ломает установку.
    if sys.prefix == sys.base_prefix:
        command.append("--user")
    command += packages

    # Индекс с токеном едет в окружении, но в напечатанной команде показан:
    # так её можно повторить в терминале, подставив токен вместо звёздочек.
    printable = " ".join(command)
    if token:
        printable = (f"PIP_INDEX_URL=https://token:***@{INDEX_HOST}{INDEX_PATH} "
                     f"PIP_TRUSTED_HOST={INDEX_HOST} {printable}").replace(token, "***")
    print(f"Ставлю зависимости портала: {', '.join(packages)}")
    print(f"$ {printable}")

    log_path, sink = _open_pip_log(printable)
    progress = _Progress("Установка")
    try:
        code, log, expired = _run_pip(command, _pip_env(token), progress, sink, timeout)
    finally:
        progress.close()
        if sink is not None:
            try:
                sink.close()
            except OSError:
                pass

    _refresh_user_site()
    still = missing_modules(modules)
    if not still and (code == 0 or modules):
        if code != 0:
            print(f"pip завершился с кодом {code}, но нужные модули на месте — продолжаю.")
        return

    # Прогресс-бар съел вывод — при ошибке он-то и нужен, показываем хвост.
    print("\n".join(log[-25:]))
    if expired:
        cause = f"pip не уложился в {int(timeout // 60)} мин и был остановлен"
    else:
        cause = f"pip завершился с кодом {code}"
    if still:
        cause += f"; не импортируются: {', '.join(still)}"
    where = f" Полный вывод pip: {log_path}." if log_path else ""
    raise RuntimeError(
        f"не удалось установить зависимости портала ({cause}). {_hint(log)}"
        f" Полная команда напечатана выше — её можно выполнить вручную в терминале."
        f"{where}".replace("  ", " ")
    )


def ensure_deps(token: str = "") -> str:
    """Поставить fastapi и uvicorn, если их нет."""
    missing = missing_modules(PORTAL_DEPS.values())
    if not missing:
        return "уже стоят"
    if not token:
        raise RuntimeError(
            f"не хватает модулей ({', '.join(missing)}), а токен пуст. Впишите токен "
            "SberOSC в ячейку «Шаг 1» и выполните её: без токена индекс пакетов "
            "портала недоступен, а с PyPI из контейнера связи нет."
        )
    install([spec for spec, module in PORTAL_DEPS.items() if module in missing],
            token, modules=missing)
    return "поставлены из индекса портала"


# --- оформление -----------------------------------------------------------

_LOGO = f"""
<svg class="fs-logo" viewBox="0 0 48 48" fill="none" role="img" aria-label="FreeSpace">
  <circle cx="24" cy="24" r="17" stroke="{ACCENT}" stroke-opacity=".22" stroke-width="9"/>
  <circle cx="24" cy="24" r="17" stroke="{ACCENT}" stroke-width="9" stroke-linecap="round"
          stroke-dasharray="66 107" transform="rotate(-90 24 24)"/>
  <circle cx="24" cy="24" r="3.2" fill="{ACCENT}"/>
</svg>"""

# Стили держатся на currentColor и полупрозрачных заливках: тетрадку открывают
# и в светлой, и в тёмной теме Jupyter, а угадывать фон — верный способ
# получить серый текст на сером.
_STYLE = f"""
<style>
.fs-card {{ font: 14px/1.55 -apple-system, "Segoe UI", system-ui, sans-serif;
  color: inherit; border: 1px solid rgba(128,128,128,.32); border-radius: 12px;
  padding: 14px 16px; margin: 4px 0 2px; }}
.fs-head {{ display: flex; align-items: center; gap: 13px; flex-wrap: wrap; }}
.fs-logo {{ width: 42px; height: 42px; flex: none; }}
.fs-name {{ font-size: 19px; font-weight: 650; letter-spacing: .2px; }}
.fs-sub {{ font-size: 13px; opacity: .65; }}
.fs-btn {{ margin-left: auto; display: inline-block; padding: 9px 18px;
  border-radius: 8px; background: {ACCENT}; color: #fff !important;
  text-decoration: none; font-weight: 600; white-space: nowrap; }}
.fs-btn:hover {{ filter: brightness(1.08); }}
.fs-meta {{ margin-top: 12px; font-size: 12.5px; opacity: .7;
  display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
.fs-meta code {{ font-size: 12px; opacity: .85; }}
.fs-dot {{ width: 8px; height: 8px; border-radius: 50%; background: #1a7f37;
  display: inline-block; }}
.fs-warn {{ margin-top: 10px; font-size: 13px; padding: 9px 12px; border-radius: 8px;
  background: rgba(210,153,34,.14); border: 1px solid rgba(210,153,34,.45); }}
.fs-err {{ margin-top: 10px; font-size: 13px; padding: 9px 12px; border-radius: 8px;
  background: rgba(207,34,46,.12); border: 1px solid rgba(207,34,46,.45); }}
.fs-log {{ margin: 8px 0 0; padding: 10px 12px; border-radius: 8px; max-height: 260px;
  overflow: auto; font-size: 12px; line-height: 1.45; white-space: pre-wrap;
  background: rgba(128,128,128,.12); }}
.fs-frame {{ width: 100%; border: 1px solid rgba(128,128,128,.32);
  border-radius: 10px; margin-top: 12px; display: block; }}
</style>"""


def _head(button: str = "") -> str:
    return f"""<div class="fs-head">{_LOGO}
    <div><div class="fs-name">FreeSpace</div>
    <div class="fs-sub">Анализатор дискового пространства</div></div>{button}</div>"""


def _card(body: str, button: str = "") -> str:
    return f'{_STYLE}<div class="fs-card">{_head(button)}{body}</div>'


def _show(html: str) -> None:
    try:
        from IPython.display import HTML, display
    except ImportError:  # запустили не из тетрадки — печатать HTML незачем
        return
    display(HTML(html))


# --- запуск и остановка ---------------------------------------------------


def stop(quiet: bool = False) -> bool:
    """Погасить сервер, поднятый прошлым ``start()``. Вернёт True, если было что гасить."""
    state = state_path()
    if not state.exists():
        if not quiet:
            print("Запущенного сервера не найдено.")
        return False
    try:
        pid = json.loads(state.read_text())["pid"]
    except (OSError, ValueError, KeyError):
        state.unlink(missing_ok=True)
        return False
    try:
        os.kill(pid, 15)
        for _ in range(30):
            time.sleep(0.1)
            os.kill(pid, 0)          # бросит OSError, когда процесс исчезнет
        os.kill(pid, 9)              # не отреагировал на TERM — добиваем
    except OSError:
        pass
    state.unlink(missing_ok=True)
    if not quiet:
        print(f"Сервер остановлен (pid {pid}).")
    return True


def start(token: str = "", *, height: int = 760, open_tab: bool = True,
          domain: str = DOMAIN) -> str:
    """Поднять сервер FreeSpace и показать приложение под ячейкой.

    Повторный вызов гасит предыдущий сервер, а не плодит новые. Возвращает
    адрес приложения.
    """
    project = find_project_root()
    state, log = project / STATE_NAME, project / LOG_NAME
    os.chdir(project)

    if stop(quiet=True):
        print("Остановлен прежний сервер.")
    # start() нередко выполняют первым, не тронув ячейку с токеном. Пустой
    # токен — не беда, если зависимости уже стоят; ensure_deps скажет, если нет.
    print("Зависимости:", ensure_deps(token))

    try:
        import jupyter_server_proxy  # noqa: F401
        proxy_ok = True
    except ImportError:
        proxy_ok = False

    port = free_port()
    prefix = proxy_prefix()
    # За прокси приложение живёт по адресу <префикс>proxy/<порт>/. Тот же
    # префикс уходит серверу как root_path, иначе FastAPI сгенерирует ссылки
    # от корня.
    root_path = f"{prefix}proxy/{port}" if prefix else ""
    url = f"{domain}{root_path}/" if prefix else f"http://127.0.0.1:{port}/"

    command = [sys.executable, "-m", "freespace.web",
               "--host", "127.0.0.1", "--port", str(port)]
    if root_path:
        command += ["--root-path", root_path]

    with open(log, "wb") as handle:
        server = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT,
                                  cwd=project)
    state.write_text(json.dumps({"pid": server.pid, "port": port, "url": url}))

    # Ждём, пока порт начнёт отвечать: сразу открывать страницу нельзя, иначе
    # браузер увидит «connection refused» и пользователь решит, что не работает.
    ready = False
    for _ in range(150):
        if server.poll() is not None:
            break
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=1)
            ready = True
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)

    if not ready:
        tail = log.read_text(errors="replace")[-3000:] if log.exists() else ""
        _show(_card(
            '<div class="fs-err">Сервер не поднялся. Последние строки лога '
            f'<code>{LOG_NAME}</code>:</div><pre class="fs-log">{_escape(tail)}</pre>'
        ))
        return url

    warning = ""
    if prefix and not proxy_ok:
        warning = ('<div class="fs-warn">В окружении нет <code>jupyter-server-proxy</code>: '
                   'ссылка через прокси работать не будет. Поставьте его '
                   '(<code>pip install jupyter-server-proxy</code>) и перезапустите '
                   'Jupyter.</div>')

    button = f'<a class="fs-btn" href="{url}" target="_blank" rel="noopener">Открыть ↗</a>'
    body = (
        warning
        + f'<div class="fs-meta"><span class="fs-dot"></span>Сервер работает · '
          f'pid {server.pid} · порт {port} · <code>{url}</code></div>'
        + f'<iframe class="fs-frame" src="{url}" style="height:{height}px" '
          f'title="FreeSpace"></iframe>'
    )
    _show(_card(body, button))

    # Открыть вкладку сама может только страница в браузере пользователя —
    # сервер тут ни при чём. Блокировщик всплывающих окон это может отменить,
    # поэтому выше уже нарисованы и ссылка, и рабочий iframe.
    if open_tab:
        try:
            from IPython.display import Javascript, display
            display(Javascript(f"window.open({json.dumps(url)}, '_blank');"))
        except ImportError:
            pass
    return url


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
