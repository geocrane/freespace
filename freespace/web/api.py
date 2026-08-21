"""HTTP-слой: маршруты, JSON, отдача страницы.

Намеренно тонкий — вся логика лежит в ``freespace.service`` и
``freespace.core``. Если однажды понадобится другой фреймворк, переписывать
нужно будет только этот файл.
"""

from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..core.platform_utils import (
    default_root,
    human_size,
    list_volumes,
    open_path,
    reveal_in_explorer,
)
from ..core.scanner import SIZE_APPARENT, SIZE_DISK
from ..core.search import (
    ANY,
    CATEGORY_LABELS,
    DIRS,
    FILES,
    GLOB,
    SUBSTRING,
    SearchFilter,
    find,
    group_by_exact_name,
)
from ..core.trash import ProtectedPathError, TrashError, TrashUnavailable
from ..service.presenter import (
    breadcrumbs,
    children_rows,
    group_rows,
    is_drillable,
    layout_tiles,
    search_rows,
)
from ..service.progress import Operations, track
from ..service.scan_service import RUNNING, ScanService
from ..service.trash_service import TrashService

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class ScanRequest(BaseModel):
    path: str
    cross_filesystems: bool = False
    # "disk" — реально занятое место, "apparent" — номинальный размер файла.
    size_mode: str = SIZE_DISK
    dedup_hardlinks: bool = True
    # Подставить готовый снимок, если он есть, вместо повторного обхода.
    use_cache: bool = True


# Токен наблюдения за ходом работы. Его придумывает браузер и присылает вместе
# с запросом: удаление остаётся обычным синхронным вызовом, а страница тем
# временем опрашивает /api/progress/<токен> и показывает полосу. Пустой токен —
# никто не смотрит, операция идёт молча.
class PathRequest(BaseModel):
    job: str
    path: str
    op: str = ""


class EntryRequest(BaseModel):
    job: str
    entry: str
    op: str = ""


class PathsRequest(BaseModel):
    job: str
    paths: list[str]
    op: str = ""


# Потолок на одну пачку. Он же — потолок набора, который отдаётся странице для
# отметки: «выбрать все» должно означать все найденные, а не только показанные,
# и одно ограничение на оба конца пути честнее, чем два разных.
# Двенадцать тысяч __pycache__ — обычное дело на рабочей машине, так что запас
# нужен изрядный; на сотнях тысяч разговор всё равно другой.
MAX_BULK_PATHS = 50000


def _job_state(job) -> dict:
    """Состояние задачи в виде, пригодном для опроса из браузера."""
    data = {
        "id": job.id,
        "root_path": job.root_path,
        "state": job.state,
        "scanned": job.scanned,
        "current_path": job.current_path,
        "elapsed": round(job.elapsed, 2),
        "error": job.error,
        "size_mode": job.size_mode,
        "dedup_hardlinks": job.dedup_hardlinks,
        "from_cache": job.from_cache,
        "snapshot_at": job.snapshot_at,
    }
    if job.result is not None:
        root = job.result.root
        data.update(
            total_size=root.size,
            total_size_human=human_size(root.size),
            file_count=root.file_count,
            skipped=len(job.result.skipped),
            skipped_sample=job.result.skipped[:20],
            boundaries=job.result.boundaries[:50],
            hardlink_saved=job.result.hardlink_saved,
            hardlink_saved_human=human_size(job.result.hardlink_saved),
        )
    return data


def create_app(root_path: str = "", allow_delete: bool = True,
               local: bool = True) -> FastAPI:
    """Собрать приложение.

    ``allow_delete`` выключает разрушающие маршруты, ``local`` — открытие путей
    в проводнике: на удалённой машине оно открыло бы окно не у того, кто просил.
    """
    app = FastAPI(title="FreeSpace", root_path=root_path)
    service = ScanService()
    trash_service = TrashService(service)
    operations = Operations()
    app.state.service = service
    app.state.trash = trash_service
    app.state.operations = operations
    app.state.allow_delete = allow_delete
    app.state.local = local

    @app.middleware("http")
    async def no_store(request, call_next):
        """Запретить браузеру кэшировать что бы то ни было.

        Страница читается с диска на каждый запрос именно затем, чтобы правки
        были видны без перезапуска сервера. Без этого заголовка половина смысла
        терялась: браузер оставлял у себя прежнюю копию, и человек продолжал
        работать со старым скриптом на новом сервере — с виду всё то же самое,
        только новые возможности не появляются. Ответы API кэшировать нельзя
        тем более: дерево и корзина меняются от каждого удаления.
        """
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def _need_delete() -> None:
        if not app.state.allow_delete:
            raise HTTPException(
                status_code=403,
                detail="Удаление выключено. Сервер принимает запросы извне, и чтобы "
                       "разрешить стирать файлы по сети, его нужно запустить с ключом "
                       "--allow-delete.",
            )

    def _job_or_404(job_id: str):
        job = service.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail="Результаты сканирования не найдены. Такое бывает после "
                       "перезапуска сервера — просканируйте папку заново.",
            )
        return job

    def _finished_job(job_id: str):
        job = _job_or_404(job_id)
        if job.state == RUNNING:
            raise HTTPException(status_code=409,
                                detail="Обход ещё идёт, подождите его окончания.")
        if job.result is None:
            raise HTTPException(
                status_code=409,
                detail=job.error or "Обход не дал результата — попробуйте ещё раз.",
            )
        return job

    # --- страница и окружение --------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        # Читаем при каждом запросе: правки страницы видны без перезапуска.
        with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as fh:
            return fh.read()

    @app.get("/api/config")
    def config() -> JSONResponse:
        """Что умеет этот запуск — страница по этому решает, что показывать."""
        return JSONResponse({
            "allow_delete": app.state.allow_delete,
            "local": app.state.local,
            "categories": CATEGORY_LABELS,
            # Страница читается с диска на каждый запрос, а маршруты живут в
            # уже запущенном процессе. Свежий интерфейс поверх старого сервера
            # молча упирался бы в 404 — пусть лучше скажет об этом сразу.
            "bulk_delete": True,
            # Страница умеет показывать полосу хода, только если сервер отдаёт
            # /api/progress. Флаг избавляет от вечно крутящейся полосы поверх
            # старого сервера.
            "progress": True,
        })

    @app.get("/api/volumes")
    def volumes() -> JSONResponse:
        items = []
        for vol in list_volumes():
            item = asdict(vol)
            item["total_human"] = human_size(vol.total)
            item["free_human"] = human_size(vol.free)
            item["used_percent"] = round(vol.used / vol.total * 100, 1) if vol.total else 0
            items.append(item)
        # "default" — что подставить в поле пути при открытии страницы. В
        # Linux-контейнере это не домашний каталог, а монтирование с данными.
        return JSONResponse({"volumes": items, "home": os.path.expanduser("~"),
                             "default": default_root()})

    # --- сканирование -----------------------------------------------------

    @app.post("/api/scan")
    def start_scan(request: ScanRequest) -> JSONResponse:
        if request.size_mode not in (SIZE_DISK, SIZE_APPARENT):
            raise HTTPException(status_code=400,
                                detail="Неизвестный способ подсчёта размера.")
        try:
            job = service.start(
                request.path,
                cross_filesystems=request.cross_filesystems,
                size_mode=request.size_mode,
                dedup_hardlinks=request.dedup_hardlinks,
                from_cache=request.use_cache,
            )
        except (NotADirectoryError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(_job_state(job))

    @app.get("/api/scan/{job_id}")
    def scan_status(job_id: str) -> JSONResponse:
        return JSONResponse(_job_state(_job_or_404(job_id)))

    @app.post("/api/rescan")
    def rescan(request: PathRequest) -> JSONResponse:
        """Обойти заново только указанную папку."""
        try:
            job = service.rescan(request.job, request.path or None)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (NotADirectoryError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(_job_state(job))

    @app.delete("/api/scan/{job_id}")
    def cancel_scan(job_id: str) -> JSONResponse:
        return JSONResponse({"cancelled": service.cancel(job_id)})

    @app.get("/api/tree")
    def tree(
        job: str,
        path: str = "",
        w: float = 900.0,
        h: float = 600.0,
        max_tiles: int = 120,
    ) -> JSONResponse:
        scan_job = _finished_job(job)
        node = service.find_node(job, path or None)
        if node is None:
            raise HTTPException(
                status_code=404,
                detail="Такого пути нет в результатах обхода. Возможно, он появился "
                       "уже после сканирования — нажмите «Пересканировать».",
            )

        root = scan_job.result.root
        return JSONResponse(
            {
                "node": {
                    "name": node.name,
                    "path": node.path,
                    "size": node.size,
                    "size_human": human_size(node.size),
                    "file_count": node.file_count,
                    "is_dir": node.is_dir,
                    "drillable": is_drillable(node),
                },
                "breadcrumbs": breadcrumbs(root, node),
                "tiles": [asdict(t) for t in layout_tiles(node, w, h, max_tiles)],
                "rows": [asdict(r) for r in children_rows(node)],
            }
        )

    # --- поиск -------------------------------------------------------------

    @app.get("/api/search")
    def search(
        job: str,
        q: str = "",
        mode: str = SUBSTRING,
        kind: str = ANY,
        min_size: int = 0,
        max_size: int | None = None,
        category: list[str] = Query(default=[]),
        top_only: bool = Query(False, description="прятать находки внутри других находок"),
        group: bool = False,
        path: str = "",
        limit: int = 500,
    ) -> JSONResponse:
        """Поиск по дереву задачи. ``path`` сужает область до поддерева."""
        scan_job = _finished_job(job)
        if kind not in (ANY, DIRS, FILES):
            raise HTTPException(status_code=400,
                                detail="Неизвестный тип объектов для поиска.")

        scope = service.find_node(job, path or None)
        if scope is None:
            raise HTTPException(status_code=404,
                                detail="Папка, внутри которой ищем, не найдена "
                                       "в результатах обхода.")

        flt = SearchFilter(
            term=q,
            mode=mode,
            kind=kind,
            min_size=max(0, min_size),
            max_size=max_size,
            categories=tuple(category),
            top_level_only=top_only,
        )
        if flt.is_empty():
            raise HTTPException(
                status_code=400,
                detail="Не задано ни одного условия. Укажите имя, вид файлов, "
                       "размер или дату — иначе найдётся вообще всё.",
            )

        found = find(scope, flt, limit=0)
        root = scan_job.result.root
        payload: dict = {
            "total_found": len(found),
            "total_size": sum(n.size for n in found),
            "total_size_human": human_size(sum(n.size for n in found)),
            "scope": scope.path,
        }
        if group:
            payload["groups"] = [
                asdict(g) for g in group_rows(group_by_exact_name(found), root)[:limit]
            ]
        else:
            payload["rows"] = [asdict(r) for r in search_rows(found[:limit], root)]

        # Показать все находки нельзя — их бывают десятки тысяч, и таблица на
        # столько строк бесполезна. Но отмечать нужно именно все: «выбрать все»,
        # которое берёт двести строк из двенадцати тысяч, — обещание, которого
        # интерфейс не выполняет. Поэтому весь набор уходит отдельным списком,
        # где на объект приходятся только путь и размер.
        selectable = [n for n in found if not n.is_trash]
        payload["all"] = [{"path": n.path, "size": n.size}
                          for n in selectable[:MAX_BULK_PATHS]]
        payload["all_capped"] = len(selectable) > MAX_BULK_PATHS
        return JSONResponse(payload)

    # --- удаление и корзина ------------------------------------------------

    @app.post("/api/delete")
    def delete(request: PathRequest) -> JSONResponse:
        _need_delete()
        try:
            with track(operations, request.op, "delete") as progress:
                entry = trash_service.delete(request.job, request.path, progress)
        except TrashUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProtectedPathError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except TrashError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        data = asdict(entry)
        data["size_human"] = human_size(entry.size)
        return JSONResponse(data)

    @app.post("/api/delete-many")
    def delete_many(request: PathsRequest) -> JSONResponse:
        """Убрать в корзину пачку объектов, отмеченных галочками."""
        _need_delete()
        if not request.paths:
            raise HTTPException(status_code=400, detail="Не отмечено ни одного объекта.")
        if len(request.paths) > MAX_BULK_PATHS:
            raise HTTPException(
                status_code=400,
                detail=f"За один раз можно удалить не больше {MAX_BULK_PATHS} объектов. "
                       "Удалите частями.",
            )
        try:
            with track(operations, request.op, "delete") as progress:
                result = trash_service.delete_many(request.job, request.paths, progress)
        except TrashUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TrashError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return JSONResponse({
            "deleted": len(result.deleted),
            "freed": result.freed,
            "freed_human": human_size(result.freed),
            # Уехали в корзину вместе с отмеченным родителем.
            "inside_deleted": len(result.inside_deleted),
            # Объекта уже не было на диске — цель достигнута, это не сбой.
            "already_gone": len(result.already_gone),
            "failed_count": len(result.failed),
            # По причине, а не по объекту: сто одинаковых абзацев нечитаемы.
            "failures": [
                {"reason": reason, "count": len(paths), "examples": paths[:3]}
                for reason, paths in result.failure_groups()
            ],
        })

    @app.get("/api/trash")
    def trash_list(job: str) -> JSONResponse:
        try:
            trash = trash_service.for_job(job)
            available = trash.available
            entries = trash_service.entries(job)
        except TrashError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        total = sum(entry.size for _dir, entry in entries)
        return JSONResponse({
            "available": available,
            "dirs": trash_service.trash_dirs(job) if available else [],
            "total_size": total,
            "total_size_human": human_size(total),
            "entries": [
                {**asdict(entry), "size_human": human_size(entry.size)}
                for _dir, entry in entries
            ],
        })

    @app.post("/api/trash/restore")
    def trash_restore(request: EntryRequest) -> JSONResponse:
        _need_delete()
        try:
            restored = trash_service.restore(request.job, request.entry)
        except TrashError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"restored": restored})

    @app.post("/api/trash/empty")
    def trash_empty(request: EntryRequest) -> JSONResponse:
        _need_delete()
        try:
            with track(operations, request.op, "empty") as progress:
                count, freed = trash_service.empty(request.job, progress)
        except TrashError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"removed": count, "freed": freed,
                             "freed_human": human_size(freed)})

    @app.get("/api/progress/{token}")
    def progress(token: str) -> JSONResponse:
        """Ход операции, начатой с этим токеном.

        Незнакомый токен — не ошибка: опрос почти всегда обгоняет запрос, ради
        которого начат, и первый ответ приходится на момент, когда обработчик
        удаления ещё не дошёл до регистрации. Страница на такой ответ просто
        спрашивает ещё раз, а 404 пришлось бы отличать от настоящих.
        """
        return JSONResponse(operations.snapshot(token))

    # --- кэш ---------------------------------------------------------------

    @app.get("/api/cache")
    def cache_info() -> JSONResponse:
        cache = service.cache
        if cache is None:
            return JSONResponse({"enabled": False, "snapshots": []})
        snapshots = [
            {
                "root_path": s.root_path,
                "created_at": s.created_at,
                "total_size": s.total_size,
                "total_size_human": human_size(s.total_size),
                "file_count": s.file_count,
                "bytes_on_disk": s.bytes_on_disk,
                "bytes_on_disk_human": human_size(s.bytes_on_disk),
                "size_mode": s.size_mode,
            }
            for s in cache.list_snapshots()
        ]
        total = cache.total_bytes()
        return JSONResponse({
            "enabled": True,
            "dir": cache.dir_path,
            "total_bytes": total,
            "total_human": human_size(total),
            "limit_bytes": cache.limit_bytes,
            "limit_human": human_size(cache.limit_bytes),
            "snapshots": snapshots,
        })

    @app.delete("/api/cache")
    def cache_clear() -> JSONResponse:
        cache = service.cache
        if cache is None:
            return JSONResponse({"freed": 0, "freed_human": human_size(0)})
        freed = cache.clear()
        return JSONResponse({"freed": freed, "freed_human": human_size(freed)})

    # --- открыть в проводнике ---------------------------------------------

    @app.post("/api/reveal")
    def reveal(request: PathRequest) -> JSONResponse:
        """Показать объект в проводнике — только когда сервер и браузер на одной машине."""
        if not app.state.local:
            raise HTTPException(
                status_code=403,
                detail="Открыть проводник можно, только когда приложение и браузер "
                       "работают на одной машине. Здесь страница открыта по сети.",
            )
        node = service.find_node(request.job, request.path)
        if node is None:
            raise HTTPException(status_code=404,
                                detail="Этого пути нет в результатах обхода.")
        if node.is_dir:
            open_path(node.path)
        else:
            reveal_in_explorer(node.path)
        return JSONResponse({"opened": node.path})

    return app
