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
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
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


def install(packages: list[str], token: str) -> None:
    """Поставить пакеты из индекса портала.

    Вывод pip не глушится: установка идёт минуту-другую, и молчащая ячейка в
    это время неотличима от зависшей. Команда печатается целиком — если
    установка не удалась, её можно повторить руками в терминале.
    """
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    # В контейнере ядро работает от системного питона, куда без --user не
    # записать; внутри venv тот же флаг, наоборот, ломает установку.
    if sys.prefix == sys.base_prefix:
        command.append("--user")
    if token:
        command += [
            f"--index-url=https://token:{token}@{INDEX_HOST}{INDEX_PATH}",
            f"--trusted-host={INDEX_HOST}",
        ]
    command += packages

    # Токен в напечатанной команде затирается: вывод ячейки сохраняется в самом
    # .ipynb и уезжает в репозиторий вместе с ним.
    printable = " ".join(command).replace(token, "***") if token else " ".join(command)
    print(f"Ставлю зависимости портала: {', '.join(packages)}")
    print(f"$ {printable}")
    if subprocess.run(command).returncode != 0:
        raise RuntimeError(
            "не удалось установить зависимости портала. Проверьте токен SberOSC и "
            "доступность индекса; полная команда напечатана выше — её можно "
            "выполнить вручную в терминале."
        )


def ensure_deps(token: str = "") -> str:
    """Поставить fastapi и uvicorn, если их нет."""
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        return "уже стоят"
    except ImportError:
        pass
    if not token:
        raise RuntimeError(
            "fastapi и uvicorn не установлены, а токен пуст. Впишите токен SberOSC "
            "в ячейку «Шаг 1» и выполните её: без токена индекс пакетов портала "
            "недоступен, а с PyPI из контейнера связи нет."
        )
    install(["fastapi>=0.110", "uvicorn>=0.27"], token)
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
