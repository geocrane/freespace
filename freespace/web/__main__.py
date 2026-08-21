"""Запуск веб-интерфейса: ``python -m freespace.web``."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import webbrowser

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def proxy_prefix() -> str:
    """Префикс URL за jupyter-server-proxy, если мы внутри Jupyter."""
    return os.environ.get("JUPYTERHUB_SERVICE_PREFIX") or os.environ.get("NB_PREFIX") or ""


def find_free_port(preferred: int = 8000, tries: int = 50) -> int:
    """Первый свободный порт начиная с ``preferred``.

    Занятый порт — самая частая причина «запустил, а ничего не открылось»,
    поэтому запускающие скрипты подбирают порт сами.
    """
    for offset in range(tries):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError(f"не нашлось свободного порта в диапазоне "
                  f"{preferred}..{preferred + tries - 1}")


def _print_urls(host: str, port: int, root_path: str, allow_delete: bool) -> None:
    shown_host = "localhost" if host in ("0.0.0.0", "::", "") else host
    prefix = proxy_prefix()

    print("=" * 64)
    print("  FreeSpace")
    print("=" * 64)
    print(f"  прямой порт   http://{shown_host}:{port}/")
    if prefix:
        print(f"  через прокси  {prefix.rstrip('/')}/proxy/{port}/")
        print("                (припишите к адресу вашего Jupyter)")
    if root_path:
        print(f"  root_path     {root_path!r}")
    print(f"  удаление      {'включено' if allow_delete else 'выключено'}")
    print("\n  Остановить: Ctrl+C")
    print("=" * 64, flush=True)


def _open_later(url: str, delay: float = 1.5) -> None:
    """Открыть браузер, когда сервер уже слушает порт."""
    def go() -> None:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — без браузера запуск всё равно валиден
            pass

    threading.Timer(delay, go).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m freespace.web", description="FreeSpace — анализатор места на диске."
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="адрес прослушивания (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", default="8000",
                        help="порт или auto — подобрать свободный (по умолчанию 8000)")
    parser.add_argument("--root-path", default=os.environ.get("FREESPACE_ROOT_PATH", ""),
                        help="префикс URL, если сервер работает за обратным прокси")
    parser.add_argument("--open-browser", action="store_true",
                        help="открыть страницу в браузере после запуска")
    delete_group = parser.add_mutually_exclusive_group()
    delete_group.add_argument("--allow-delete", action="store_true", default=None,
                              help="разрешить удаление файлов (по умолчанию — "
                                   "только при прослушивании localhost)")
    delete_group.add_argument("--no-delete", dest="allow_delete", action="store_false",
                              help="запретить удаление файлов")
    args = parser.parse_args(argv)

    if str(args.port).lower() == "auto":
        try:
            port = find_free_port()
        except OSError as exc:
            print(exc)
            return 1
    else:
        try:
            port = int(args.port)
        except ValueError:
            print(f"--port должен быть числом или auto, получено {args.port!r}")
            return 1

    # Удаление по HTTP доступно только тем, кто дотянулся до порта. Пока это
    # петлевой адрес — это сам пользователь; наружу открываем лишь по прямой
    # просьбе.
    listens_locally = args.host in LOOPBACK
    allow_delete = listens_locally if args.allow_delete is None else args.allow_delete
    # За прокси браузер работает не на той машине, где сервер: «показать в
    # проводнике» открыло бы окно не у того человека.
    local = listens_locally and not proxy_prefix() and not args.root_path

    try:
        import uvicorn
    except ImportError:
        print("Не установлен uvicorn. Установите зависимости:")
        print("  pip install -r requirements.txt")
        return 1

    from .api import create_app

    _print_urls(args.host, port, args.root_path, allow_delete)
    if args.open_browser:
        shown_host = "localhost" if args.host in ("0.0.0.0", "::", "") else args.host
        _open_later(f"http://{shown_host}:{port}/")
    try:
        uvicorn.run(
            create_app(args.root_path, allow_delete=allow_delete, local=local),
            host=args.host,
            port=port,
            root_path=args.root_path,
            log_level="info",
        )
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except OSError as exc:
        print(f"\nНе удалось занять {args.host}:{port} — {exc}")
        print("Попробуйте другой порт: --port auto")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
