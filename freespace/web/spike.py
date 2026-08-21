"""Спайк веб-бэкенда: проверка запускаемости и разведка путей.

Задача одна и узкая: выяснить, поднимается ли HTTP-сервер там, где нет
графики (Linux-контейнер с Jupyter), достижим ли он из браузера, и что в
этом окружении с путями — где лежат реальные данные, а где overlayfs.

Запуск::

    python -m freespace.web.spike
    python -m freespace.web.spike --port 8000
    python -m freespace.web.spike --root-path /user/me/proxy/8000

Сервер поднимается на FastAPI, если он установлен, иначе на stdlib
``http.server``. Второй режим нужен затем, чтобы вопрос «достижим ли порт
вообще» можно было проверить до установки любых зависимостей — если не
работает и он, дело не в FastAPI.

Все запросы со страницы идут относительными URL, поэтому спайк одинаково
работает и на прямом порту, и за jupyter-server-proxy.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import time

# Файловые системы, которые не имеют отношения к занятому месту: реальный
# сканер обязан их пропускать, здесь мы их просто помечаем в отчёте.
PSEUDO_FSTYPES = frozenset(
    {
        "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
        "securityfs", "pstore", "bpf", "debugfs", "tracefs", "configfs",
        "fusectl", "hugetlbfs", "mqueue", "binfmt_misc", "autofs", "ramfs",
        "nsfs", "overlay", "squashfs",
    }
)

# Куда в типичном контейнере монтируют реальные данные.
CONTAINER_ROOTS = (
    "/workspace", "/data", "/mnt", "/media", "/srv",
    "/home/jovyan", "/home/jovyan/work", "/opt/app",
)


# --- сбор диагностики -----------------------------------------------------


def _platform_info() -> dict:
    import platform

    return {
        "sys_platform": sys.platform,
        "os_name": os.name,
        "machine": platform.machine(),
        "release": platform.release(),
        "python": sys.version.split()[0],
        "hostname": socket.gethostname(),
        "cpu_count": os.cpu_count(),
    }


def _process_info() -> dict:
    info: dict = {
        "cwd": os.getcwd(),
        "home": os.path.expanduser("~"),
        "executable": sys.executable,
        "argv0": sys.argv[0],
    }
    # uid/gid есть только на POSIX.
    for attr in ("getuid", "getgid"):
        fn = getattr(os, attr, None)
        info[attr[3:]] = fn() if fn else None
    try:
        import getpass

        info["user"] = getpass.getuser()
    except Exception:
        info["user"] = None
    return info


def _container_info() -> dict:
    """Признаки того, что мы внутри контейнера."""
    signals: list[str] = []
    if os.path.exists("/.dockerenv"):
        signals.append("/.dockerenv существует")
    try:
        with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as fh:
            cgroup = fh.read(4000)
        for marker in ("docker", "kubepods", "containerd", "lxc", "podman"):
            if marker in cgroup:
                signals.append(f"/proc/1/cgroup содержит {marker!r}")
    except OSError:
        pass
    try:
        with open("/proc/1/comm", encoding="utf-8", errors="replace") as fh:
            comm = fh.read(200).strip()
        if comm and comm != "systemd":
            signals.append(f"PID 1 = {comm!r}")
    except OSError:
        pass
    return {"likely_container": bool(signals), "signals": signals}


def _jupyter_info() -> dict:
    """Переменные, задающие префикс URL за jupyter-server-proxy.

    ``JUPYTERHUB_SERVICE_PREFIX`` — главная: именно она даёт префикс вида
    ``/user/<имя>/``, к которому дописывается ``proxy/<порт>/``.
    """
    env_keys = (
        "JUPYTERHUB_SERVICE_PREFIX", "JUPYTERHUB_USER", "JUPYTER_SERVER_ROOT",
        "NB_PREFIX", "NB_USER", "JPY_API_TOKEN", "BINDER_SERVICE_HOST",
    )
    # Отчёт пересылают в переписке и в тикеты, поэтому значения секретов в него
    # не попадают — достаточно знать, что переменная задана.
    secret_keys = {"JPY_API_TOKEN"}
    env = {}
    for key in env_keys:
        value = os.environ.get(key)
        if value:
            env[key] = "<задан, скрыт>" if key in secret_keys else value
    try:
        import jupyter_server_proxy  # noqa: F401

        proxy_installed = True
    except Exception:
        proxy_installed = False
    return {"env": env, "jupyter_server_proxy_installed": proxy_installed}


def _mounts() -> list[dict]:
    """Точки монтирования. На Linux — из /proc/mounts, иначе — по томам."""
    result: list[dict] = []

    if os.path.exists("/proc/mounts"):
        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        seen: set[str] = set()
        for line in lines:
            parts = line.split()
            if len(parts) < 3:
                continue
            device, mountpoint, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
            if mountpoint in seen:
                continue
            seen.add(mountpoint)
            result.append(
                {
                    "device": device,
                    "mountpoint": mountpoint,
                    "fstype": fstype,
                    "pseudo": fstype in PSEUDO_FSTYPES,
                    **_space_and_dev(mountpoint),
                }
            )
        return result

    if sys.platform == "darwin":
        points = ["/"]
        try:
            points += [os.path.join("/Volumes", n) for n in sorted(os.listdir("/Volumes"))]
        except OSError:
            pass
    elif os.name == "nt":
        import string

        points = [f"{letter}:\\" for letter in string.ascii_uppercase
                  if os.path.exists(f"{letter}:\\")]
    else:
        points = ["/"]

    for point in points:
        result.append(
            {
                "device": None,
                "mountpoint": point,
                "fstype": None,
                "pseudo": False,
                **_space_and_dev(point),
            }
        )
    return result


def _space_and_dev(path: str) -> dict:
    """Свободное/общее место и идентификатор устройства для пути."""
    out: dict = {"total": None, "used": None, "free": None, "st_dev": None, "error": None}
    try:
        usage = shutil.disk_usage(path)
        out["total"], out["used"], out["free"] = usage.total, usage.used, usage.free
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    try:
        out["st_dev"] = os.stat(path).st_dev
    except OSError:
        pass
    return out


def _root_candidates() -> list[str]:
    candidates = ["/" if os.name != "nt" else os.path.abspath(os.sep)]
    candidates.append(os.path.expanduser("~"))
    candidates.append(os.getcwd())
    candidates.extend(CONTAINER_ROOTS)
    if sys.platform == "darwin":
        candidates.append("/Volumes")
    for key in ("JUPYTER_SERVER_ROOT",):
        value = os.environ.get(key)
        if value:
            candidates.append(value)

    seen: set[str] = set()
    unique: list[str] = []
    for path in candidates:
        norm = os.path.abspath(path)
        if norm not in seen:
            seen.add(norm)
            unique.append(norm)
    return unique


def _probe_root(path: str) -> dict:
    """Пригодность каталога как корня сканирования."""
    info: dict = {
        "path": path,
        "exists": os.path.exists(path),
        "is_dir": os.path.isdir(path),
        "readable": os.access(path, os.R_OK | os.X_OK),
        "writable": os.access(path, os.W_OK),
        "entries": None,
        "sample": [],
        "error": None,
    }
    info.update(_space_and_dev(path))
    if not (info["is_dir"] and info["readable"]):
        return info
    try:
        names: list[str] = []
        count = 0
        with os.scandir(path) as it:
            for entry in it:
                count += 1
                if len(names) < 12:
                    names.append(entry.name)
        info["entries"] = count
        info["sample"] = sorted(names)
    except OSError as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def collect_env() -> dict:
    """Полный отчёт об окружении."""
    return {
        "platform": _platform_info(),
        "process": _process_info(),
        "container": _container_info(),
        "jupyter": _jupyter_info(),
        "mounts": _mounts(),
        "roots": [_probe_root(p) for p in _root_candidates()],
    }


# --- пробный обход --------------------------------------------------------


def _core_scan():
    """Функция полного обхода из ядра, если оно доступно.

    Спайк должен запускаться и как часть пакета, и как одиночный файл,
    скопированный на машину без установки. Поэтому ядро подключается мягко:
    нет — обойдёмся встроенной реализацией ниже.
    """
    try:
        from ..core.scanner import scan  # запуск как модуль пакета

        return scan
    except ImportError:
        pass
    try:
        from freespace.core.scanner import scan  # пакет установлен или лежит в cwd

        return scan
    except ImportError:
        pass
    # Файл лежит внутри дерева репозитория, но пакет не установлен.
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
        try:
            from freespace.core.scanner import scan

            return scan
        except ImportError:
            sys.path.remove(repo_root)
    return None


def _standalone_full_scan(path: str) -> tuple[int, int, int, list[str]]:
    """Резервный полный обход без ядра: размер, файлы, узлы, пропущенное.

    Итеративный, чтобы не упереться в предел рекурсии, и без перехода по
    симлинкам, чтобы не зациклиться.
    """
    total = files = nodes = 0
    skipped: list[str] = []
    stack = [path]
    while stack:
        current = stack.pop()
        nodes += 1
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                            nodes += 1
                    except OSError:
                        skipped.append(entry.path)
        except OSError:
            skipped.append(current)
    return total, files, nodes, skipped


def probe_scan(path: str, full: bool = False) -> dict:
    """Замер скорости обхода.

    ``full=False`` — только непосредственные дети (безопасно для любого пути).
    ``full=True`` — полный обход; вызывать осознанно и на небольшом каталоге.
    """
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return {"path": path, "error": "не каталог или недоступен"}

    if full:
        scan = _core_scan()
        started = time.perf_counter()
        try:
            if scan is not None:
                result = scan(path)
                total_size = result.root.size
                file_count = result.root.file_count
                nodes = sum(1 for _ in result.root.iter_subtree())
                skipped = result.skipped
            else:
                total_size, file_count, nodes, skipped = _standalone_full_scan(path)
        except Exception as exc:
            return {"path": path, "full": True, "error": f"{type(exc).__name__}: {exc}"}
        elapsed = time.perf_counter() - started
        return {
            "path": path,
            "full": True,
            "engine": "core.scanner" if scan is not None else "встроенный (ядро недоступно)",
            "elapsed_sec": round(elapsed, 3),
            "total_size": total_size,
            "file_count": file_count,
            "nodes": nodes,
            "nodes_per_sec": int(nodes / elapsed) if elapsed > 0 else None,
            "skipped": len(skipped),
            "skipped_sample": skipped[:10],
        }

    started = time.perf_counter()
    children: list[dict] = []
    errors = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    stat = entry.stat(follow_symlinks=False)
                    children.append(
                        {
                            "name": entry.name,
                            "is_dir": entry.is_dir(follow_symlinks=False),
                            "is_symlink": entry.is_symlink(),
                            "size": stat.st_size,
                            "st_dev": stat.st_dev,
                            "st_nlink": stat.st_nlink,
                        }
                    )
                except OSError:
                    errors += 1
    except OSError as exc:
        return {"path": path, "error": f"{type(exc).__name__}: {exc}"}
    elapsed = time.perf_counter() - started

    root_dev = None
    try:
        root_dev = os.stat(path).st_dev
    except OSError:
        pass
    children.sort(key=lambda c: c["size"], reverse=True)
    return {
        "path": path,
        "full": False,
        "elapsed_sec": round(elapsed, 4),
        "count": len(children),
        "errors": errors,
        "st_dev": root_dev,
        # Дети на другом устройстве — это точки монтирования: реальный
        # сканер по ним ходить не должен.
        "crossings": [c["name"] for c in children
                      if c["is_dir"] and root_dev is not None and c["st_dev"] != root_dev],
        "children": children[:200],
    }


# --- HTML -----------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FreeSpace — спайк</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif;
         margin: 0; padding: 24px; max-width: 1100px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2 { font-size: 15px; margin: 28px 0 8px; }
  .sub { opacity: .65; margin: 0 0 20px; }
  .ok { color: #1a7f37; } .bad { color: #cf222e; } .muted { opacity: .55; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 5px 10px 5px 0;
           border-bottom: 1px solid rgba(128,128,128,.25); }
  th { font-weight: 600; opacity: .7; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  code { font: 12px ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
  .wrap { overflow-x: auto; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 8px 0 4px; }
  input { font: inherit; padding: 5px 8px; min-width: 320px; flex: 1;
          border: 1px solid rgba(128,128,128,.45); border-radius: 5px;
          background: transparent; color: inherit; }
  button { font: inherit; padding: 5px 12px; border-radius: 5px; cursor: pointer;
           border: 1px solid rgba(128,128,128,.45); background: transparent; color: inherit; }
  button:hover { border-color: currentColor; }
  pre { background: rgba(128,128,128,.1); padding: 10px 12px; border-radius: 6px;
        overflow-x: auto; font-size: 12px; }
</style>
</head>
<body>
<h1>FreeSpace — спайк веб-бэкенда</h1>
<p class="sub">Если вы это видите, HTTP-сервер поднялся и достижим из браузера.
Сервер: <b id="backend">…</b> · URL страницы: <code id="here"></code></p>

<div id="env">Загрузка диагностики…</div>

<h2>Пробный обход</h2>
<div class="row">
  <input id="path" placeholder="путь для проверки">
  <button onclick="probe(false)">Верхний уровень</button>
  <button onclick="probe(true)">Полный обход</button>
</div>
<p class="sub" style="margin:0">«Полный обход» рекурсивно читает весь каталог —
запускайте на небольшом, не на корне.</p>
<div id="scan"></div>

<script>
const $ = id => document.getElementById(id);
document.getElementById('here').textContent = location.pathname;

function human(n) {
  if (n === null || n === undefined) return '—';
  const u = ['B','KB','MB','GB','TB','PB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i];
}
const esc = s => String(s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const yn = b => b ? '<span class="ok">да</span>' : '<span class="bad">нет</span>';

// Запросы строго относительными URL — иначе за jupyter-server-proxy отвалится.
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
}

function table(head, rows) {
  return '<div class="wrap"><table><tr>' + head.map(h => '<th>' + h + '</th>').join('') +
    '</tr>' + rows.map(r => '<tr>' + r.join('') + '</tr>').join('') + '</table></div>';
}

api('api/env').then(d => {
  $('backend').textContent = d.backend || '?';
  const p = d.platform, pr = d.process, c = d.container, j = d.jupyter;
  let html = '<h2>Окружение</h2><pre>' +
    esc(`${p.sys_platform} · ${p.machine} · python ${p.python} · хост ${p.hostname} · ядер ${p.cpu_count}
cwd:  ${pr.cwd}
home: ${pr.home}
пользователь: ${pr.user} (uid ${pr.uid}, gid ${pr.gid})
контейнер: ${c.likely_container ? 'да — ' + c.signals.join('; ') : 'признаков нет'}
jupyter-server-proxy: ${j.jupyter_server_proxy_installed ? 'установлен' : 'не установлен'}
${Object.keys(j.env).length ? Object.entries(j.env).map(([k,v]) => k + '=' + v).join('\\n') : 'переменных JupyterHub нет'}`) +
    '</pre>';

  const real = d.mounts.filter(m => !m.pseudo), pseudo = d.mounts.filter(m => m.pseudo);
  html += '<h2>Точки монтирования — реальные (' + real.length + ')</h2>';
  html += table(['Точка', 'ФС', 'Устройство', 'Всего', 'Свободно', 'st_dev'],
    real.map(m => ['<td><code>' + esc(m.mountpoint) + '</code></td>',
      '<td>' + esc(m.fstype || '—') + '</td>',
      '<td class="muted"><code>' + esc(m.device || '—') + '</code></td>',
      '<td class="num">' + human(m.total) + '</td>',
      '<td class="num">' + human(m.free) + '</td>',
      '<td class="num muted">' + esc(m.st_dev ?? '—') + '</td>']));
  if (pseudo.length) {
    html += '<h2>Псевдо-ФС — сканер обязан их пропускать (' + pseudo.length + ')</h2><p class="muted"><code>' +
      pseudo.map(m => esc(m.mountpoint)).join('</code>, <code>') + '</code></p>';
  }

  html += '<h2>Кандидаты в корни сканирования</h2>';
  html += table(['Путь', 'Есть', 'Чтение', 'Запись', 'Элементов', 'Свободно', 'st_dev', 'Содержимое'],
    d.roots.map(r => ['<td><code>' + esc(r.path) + '</code></td>',
      '<td>' + yn(r.exists) + '</td>', '<td>' + yn(r.readable) + '</td>',
      '<td>' + yn(r.writable) + '</td>',
      '<td class="num">' + (r.entries ?? '—') + '</td>',
      '<td class="num">' + human(r.free) + '</td>',
      '<td class="num muted">' + esc(r.st_dev ?? '—') + '</td>',
      '<td class="muted"><code>' + esc(r.sample.slice(0, 6).join(' ')) + '</code></td>']));
  $('env').innerHTML = html;

  const best = d.roots.find(r => r.readable && r.entries) || d.roots[0];
  if (best) $('path').value = best.path;
}).catch(e => { $('env').innerHTML = '<p class="bad">Ошибка: ' + esc(e.message) + '</p>'; });

async function probe(full) {
  $('scan').innerHTML = '<p class="muted">Обход…</p>';
  try {
    const q = 'api/scan?path=' + encodeURIComponent($('path').value) + (full ? '&full=1' : '');
    const d = await api(q);
    if (d.error) { $('scan').innerHTML = '<p class="bad">' + esc(d.error) + '</p>'; return; }
    if (d.full) {
      $('scan').innerHTML = '<pre>' + esc(
`путь:      ${d.path}
движок:    ${d.engine}
время:     ${d.elapsed_sec} с
размер:    ${human(d.total_size)}
файлов:    ${d.file_count}
узлов:     ${d.nodes} (${d.nodes_per_sec}/с)
пропущено: ${d.skipped}${d.skipped_sample.length ? '\\n  ' + d.skipped_sample.join('\\n  ') : ''}`) + '</pre>';
      return;
    }
    let html = '<p class="muted">' + d.count + ' элементов за ' + d.elapsed_sec + ' с' +
      (d.errors ? ' · ошибок доступа: ' + d.errors : '') +
      (d.crossings.length ? ' · <span class="bad">точки монтирования: ' +
        esc(d.crossings.join(', ')) + '</span>' : '') + '</p>';
    html += table(['Имя', 'Тип', 'Размер', 'nlink', 'st_dev'],
      d.children.map(c => ['<td><code>' + esc(c.name) + '</code></td>',
        '<td class="muted">' + (c.is_symlink ? 'симлинк' : c.is_dir ? 'папка' : 'файл') + '</td>',
        '<td class="num">' + (c.is_dir ? '—' : human(c.size)) + '</td>',
        '<td class="num' + (c.st_nlink > 1 ? ' bad' : ' muted') + '">' + c.st_nlink + '</td>',
        '<td class="num muted">' + c.st_dev + '</td>']));
    $('scan').innerHTML = html;
  } catch (e) { $('scan').innerHTML = '<p class="bad">Ошибка: ' + esc(e.message) + '</p>'; }
}
</script>
</body>
</html>
"""


# --- серверы --------------------------------------------------------------


def _build_fastapi(backend_name: str, root_path: str):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="FreeSpace spike", root_path=root_path)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE

    @app.get("/api/env")
    def env() -> JSONResponse:
        return JSONResponse({**collect_env(), "backend": backend_name})

    @app.get("/api/scan")
    def scan_endpoint(path: str, full: int = 0) -> JSONResponse:
        return JSONResponse(probe_scan(path, full=bool(full)))

    return app


def _run_fastapi(host: str, port: int, root_path: str) -> None:
    import uvicorn

    app = _build_fastapi(f"FastAPI + uvicorn {uvicorn.__version__}", root_path)
    uvicorn.run(app, host=host, port=port, root_path=root_path, log_level="info")


def _run_stdlib(host: str, port: int) -> None:
    """Запасной сервер на stdlib — без единой внешней зависимости."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    class Handler(BaseHTTPRequestHandler):
        server_version = "FreeSpaceSpike/0.1"

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict) -> None:
            self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
            parsed = urlparse(self.path)
            # Хвост пути: за прокси перед ним может быть произвольный префикс.
            route = parsed.path.rstrip("/").rsplit("/api/", 1)
            try:
                if len(route) == 2 and route[1] == "env":
                    self._json({**collect_env(), "backend": "stdlib http.server"})
                elif len(route) == 2 and route[1] == "scan":
                    query = parse_qs(parsed.query)
                    self._json(
                        probe_scan(
                            query.get("path", [os.getcwd()])[0],
                            full=query.get("full", ["0"])[0] not in ("", "0"),
                        )
                    )
                else:
                    self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            except Exception as exc:  # диагностика важнее аккуратности
                self._json({"error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def _print_urls(host: str, port: int, backend: str, root_path: str) -> None:
    shown_host = "localhost" if host in ("0.0.0.0", "::", "") else host
    prefix = os.environ.get("JUPYTERHUB_SERVICE_PREFIX") or os.environ.get("NB_PREFIX")

    print("=" * 68)
    print(f"  FreeSpace — спайк веб-бэкенда   ({backend})")
    print("=" * 68)
    print("  Пробуйте по очереди, пока какой-нибудь не откроется:")
    print()
    print(f"  1. прямой порт   http://{shown_host}:{port}/")
    print(f"                   http://{socket.gethostname()}:{port}/")
    if prefix:
        print(f"  2. через прокси  {prefix.rstrip('/')}/proxy/{port}/")
        print("     (префикс взят из переменной окружения JupyterHub)")
    else:
        print(f"  2. через прокси  /proxy/{port}/  — допишите к адресу Jupyter,")
        print("     например  https://<хост>/user/<вы>/proxy/{}/".format(port))
    if root_path:
        print(f"\n  root_path = {root_path!r}")
    print()
    print("  Открылась страница со списком точек монтирования — спайк пройден.")
    print("  Остановить: Ctrl+C")
    print("=" * 68, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m freespace.web.spike",
        description="Спайк: проверка запускаемости веб-бэкенда и разведка путей.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="адрес прослушивания (по умолчанию 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="порт (по умолчанию 8000)")
    parser.add_argument(
        "--root-path",
        default=os.environ.get("FREESPACE_ROOT_PATH", ""),
        help="префикс URL, если сервер работает за обратным прокси",
    )
    parser.add_argument("--stdlib", action="store_true",
                        help="принудительно использовать stdlib-сервер вместо FastAPI")
    parser.add_argument("--report", action="store_true",
                        help="не поднимать сервер: напечатать диагностику в JSON и выйти")
    args = parser.parse_args(argv)

    if args.report:
        print(json.dumps(collect_env(), ensure_ascii=False, indent=2))
        return 0

    use_fastapi = not args.stdlib
    if use_fastapi:
        try:
            import fastapi  # noqa: F401
            import uvicorn  # noqa: F401
        except ImportError as exc:
            print(f"FastAPI/uvicorn недоступны ({exc.name}) — поднимаю stdlib-сервер.")
            print("Установить:  pip install fastapi uvicorn\n")
            use_fastapi = False

    backend = "FastAPI + uvicorn" if use_fastapi else "stdlib http.server"
    _print_urls(args.host, args.port, backend, args.root_path)

    try:
        if use_fastapi:
            _run_fastapi(args.host, args.port, args.root_path)
        else:
            _run_stdlib(args.host, args.port)
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except OSError as exc:
        print(f"\nНе удалось занять {args.host}:{args.port} — {exc}")
        print("Попробуйте другой порт: --port 8888")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
