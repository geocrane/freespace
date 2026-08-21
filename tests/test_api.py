"""Тесты HTTP-слоя: маршруты, коды ошибок и запреты."""

from __future__ import annotations

import os
import time

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from freespace.web.api import MAX_BULK_PATHS, create_app  # noqa: E402


def _client(**kwargs):
    return fastapi_testclient.TestClient(create_app(**kwargs))


def _scan(client, path, **body):
    response = client.post("/api/scan", json={"path": path, "size_mode": "apparent",
                                              "use_cache": False, **body})
    assert response.status_code == 200, response.text
    job = response.json()
    deadline = time.time() + 10
    while job["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
        job = client.get(f"/api/scan/{job['id']}").json()
    assert job["state"] == "done", job
    return job


def test_index_and_config():
    with _client() as client:
        assert "<title>FreeSpace</title>" in client.get("/").text
        config = client.get("/api/config").json()
        assert config["allow_delete"] is True
        assert "video" in config["categories"]


def test_nothing_is_cached_by_the_browser(sample_tree):
    """Иначе браузер оставит у себя прежнюю страницу и покажет её поверх нового
    сервера: с виду всё то же самое, только новые возможности не появляются."""
    with _client() as client:
        for path in ("/", "/api/config", "/api/volumes"):
            assert client.get(path).headers["cache-control"] == "no-store", path


def test_scan_rejects_missing_path():
    with _client() as client:
        response = client.post("/api/scan", json={"path": "/нет/такого/пути"})
        assert response.status_code == 400


def test_scan_rejects_unknown_size_mode(sample_tree):
    with _client() as client:
        response = client.post("/api/scan", json={"path": sample_tree, "size_mode": "хз"})
        assert response.status_code == 400


def test_tree_returns_tiles_and_rows(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)
        data = client.get(f"/api/tree?job={job['id']}&w=400&h=300").json()

        assert data["node"]["size"] == 8350
        assert data["tiles"], "плитки должны быть"
        assert [r["name"] for r in data["rows"]][0] == "docs"
        assert data["breadcrumbs"][0]["path"] == job["root_path"]


def test_tree_rejects_unknown_job():
    with _client() as client:
        assert client.get("/api/tree?job=нет").status_code == 404


def test_search_by_glob(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)
        data = client.get(f"/api/search?job={job['id']}&q=*.bin&mode=glob").json()

        assert data["total_found"] == 2
        assert {r["name"] for r in data["rows"]} == {"big.bin", "lib.bin"}


def test_search_grouped_by_name(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)
        data = client.get(
            f"/api/search?job={job['id']}&q=venv&mode=exact&kind=dirs&group=true"
        ).json()

        assert len(data["groups"]) == 1
        group = data["groups"][0]
        assert group["count"] == 2
        assert group["total_size"] == 8000
        # Группа обязана нести сами объекты: иначе «venv занимают 8 КБ» —
        # справка, с которой пользователь ничего не может сделать.
        assert len(group["items"]) == 2
        assert [i["size"] for i in group["items"]] == [5000, 3000]
        assert all(i["path"].endswith("venv") for i in group["items"])
        assert all(i["parent_path"] for i in group["items"])


def test_search_without_conditions_is_rejected(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)
        assert client.get(f"/api/search?job={job['id']}").status_code == 400


def test_delete_moves_to_trash_and_updates_tree(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)

        response = client.post("/api/delete",
                               json={"job": job["id"], "path": os.path.join(sample_tree, "docs")})
        assert response.status_code == 200, response.text

        tree = client.get(f"/api/tree?job={job['id']}").json()
        assert tree["node"]["size"] == 3150
        trash = client.get(f"/api/trash?job={job['id']}").json()
        assert [e["name"] for e in trash["entries"]] == ["docs"]


def test_delete_publishes_progress_under_its_token(sample_tree):
    """Полоса хода: страница опрашивает токен, который сама и придумала."""
    with _client() as client:
        job = _scan(client, sample_tree)
        target = os.path.join(sample_tree, "docs")
        response = client.post("/api/delete",
                               json={"job": job["id"], "path": target, "op": "тест-1"})
        assert response.status_code == 200, response.text

        progress = client.get("/api/progress/тест-1").json()
        assert progress["state"] == "done"
        assert progress["done"] == progress["total"] == 1
        assert progress["freed"] == response.json()["size"]


def test_delete_many_progress_counts_every_path(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)
        client.post("/api/delete-many", json={
            "job": job["id"],
            "paths": [os.path.join(sample_tree, "docs"),
                      os.path.join(sample_tree, "project"), "/usr"],
            "op": "тест-2",
        })
        progress = client.get("/api/progress/тест-2").json()
        # Системный путь удалить нельзя, но работа по нему проделана: полоса
        # обязана дойти до конца, а не застрять на двух третях.
        assert progress["done"] == progress["total"] == 3
        assert progress["state"] == "done"


def test_progress_of_unknown_token_is_not_an_error():
    """Опрос начинается раньше, чем сервер успевает завести операцию."""
    with _client() as client:
        response = client.get("/api/progress/никто-не-начинал")
        assert response.status_code == 200
        assert response.json()["state"] == "unknown"


def test_empty_trash_progress_knows_the_bytes_in_advance(sample_tree):
    """Объём очистки известен заранее: размеры лежат в meta.json."""
    with _client() as client:
        job = _scan(client, sample_tree)
        client.post("/api/delete",
                    json={"job": job["id"], "path": os.path.join(sample_tree, "docs")})
        client.post("/api/trash/empty", json={"job": job["id"], "entry": "", "op": "тест-3"})

        progress = client.get("/api/progress/тест-3").json()
        assert progress["total"] == 1
        assert progress["total_bytes"] == progress["freed"] == 5200


def test_delete_of_system_path_is_forbidden(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)
        response = client.post("/api/delete", json={"job": job["id"], "path": "/usr"})
        assert response.status_code == 403


def test_delete_is_off_when_server_started_without_it(sample_tree):
    """Запуск наружу без --allow-delete не должен давать стирать файлы по HTTP."""
    with _client(allow_delete=False) as client:
        job = _scan(client, sample_tree)
        target = os.path.join(sample_tree, "docs")

        assert client.post("/api/delete", json={"job": job["id"], "path": target}).status_code == 403
        assert client.post("/api/trash/empty",
                           json={"job": job["id"], "entry": ""}).status_code == 403
        assert os.path.exists(target)


def test_reveal_is_off_behind_proxy(sample_tree):
    """За прокси браузер на другой машине — открывать там проводник бессмысленно."""
    with _client(local=False) as client:
        job = _scan(client, sample_tree)
        response = client.post("/api/reveal",
                               json={"job": job["id"], "path": sample_tree})
        assert response.status_code == 403


def test_restore_returns_file(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)
        target = os.path.join(sample_tree, "docs")
        entry = client.post("/api/delete", json={"job": job["id"], "path": target}).json()

        restored = client.post("/api/trash/restore",
                               json={"job": job["id"], "entry": entry["id"]})
        assert restored.status_code == 200
        assert os.path.exists(os.path.join(target, "b.txt"))


def test_cache_reports_and_clears(sample_tree):
    with _client() as client:
        _scan(client, sample_tree, use_cache=True)
        deadline = time.time() + 5
        while time.time() < deadline and not client.get("/api/cache").json()["snapshots"]:
            time.sleep(0.02)

        info = client.get("/api/cache").json()
        assert info["enabled"] is True
        assert info["snapshots"], "снимок должен был сохраниться"
        assert info["limit_bytes"] == 100 * 1024 * 1024

        assert client.delete("/api/cache").json()["freed"] > 0
        assert client.get("/api/cache").json()["snapshots"] == []


def test_delete_many_moves_all_marked(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)

        response = client.post("/api/delete-many", json={
            "job": job["id"],
            "paths": [os.path.join(sample_tree, "docs"), os.path.join(sample_tree, "a.txt")],
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["deleted"] == 2
        assert data["freed"] == 5300
        assert data["failed_count"] == 0

        assert client.get(f"/api/tree?job={job['id']}").json()["node"]["size"] == 3050
        assert len(client.get(f"/api/trash?job={job['id']}").json()["entries"]) == 2


def test_delete_many_reports_forbidden_paths(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)

        data = client.post("/api/delete-many", json={
            "job": job["id"],
            "paths": ["/usr", os.path.join(sample_tree, "a.txt")],
        }).json()

        assert data["deleted"] == 1
        assert data["failed_count"] == 1
        assert data["failures"][0]["examples"] == ["/usr"]


def test_delete_many_rejects_empty_and_oversized(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)

        assert client.post("/api/delete-many",
                           json={"job": job["id"], "paths": []}).status_code == 400
        too_many = [os.path.join(sample_tree, f"f{i}")
                    for i in range(MAX_BULK_PATHS + 1)]
        assert client.post("/api/delete-many",
                           json={"job": job["id"], "paths": too_many}).status_code == 400


def test_delete_many_is_off_without_permission(sample_tree):
    with _client(allow_delete=False) as client:
        job = _scan(client, sample_tree)
        target = os.path.join(sample_tree, "docs")

        assert client.post("/api/delete-many",
                           json={"job": job["id"], "paths": [target]}).status_code == 403
        assert os.path.exists(target)


def test_search_hides_nested_matches_with_top_only(tmp_path):
    """Вложенные node_modules прячутся флагом top_only: удалять надо внешний."""
    root = tmp_path / "root"
    inner = root / "проект" / "node_modules" / "пакет" / "node_modules"
    inner.mkdir(parents=True)
    (root / "проект" / "node_modules" / "big.bin").write_bytes(b"x" * 5000)
    (inner / "small.bin").write_bytes(b"x" * 700)

    with _client() as client:
        job = _scan(client, str(root))
        query = f"/api/search?job={job['id']}&q=node_modules&mode=exact&kind=dirs"

        assert len(client.get(query).json()["rows"]) == 2
        top = client.get(query + "&top_only=true").json()["rows"]
        assert len(top) == 1
        assert top[0]["size"] == 5700


def test_config_announces_bulk_delete():
    """Страница читается с диска, маршруты — нет.

    Свежий интерфейс поверх сервера, запущенного из старой версии, молча
    упирался бы в 404 при пакетном удалении. Флаг даёт странице сказать об
    этом заранее, поэтому он не должен потеряться.
    """
    with _client() as client:
        assert client.get("/api/config").json()["bulk_delete"] is True


def test_rescan_updates_only_the_folder(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)
        with open(os.path.join(sample_tree, "project", "новый.bin"), "wb") as fh:
            fh.write(b"x" * 1000)

        response = client.post("/api/rescan", json={
            "job": job["id"], "path": os.path.join(sample_tree, "project"),
        })
        assert response.status_code == 200, response.text
        state = response.json()
        deadline = time.time() + 10
        while state["state"] == "running" and time.time() < deadline:
            time.sleep(0.02)
            state = client.get(f"/api/scan/{state['id']}").json()

        assert state["total_size"] == 9350
        tree = client.get(f"/api/tree?job={job['id']}"
                          f"&path={os.path.join(sample_tree, 'project')}").json()
        assert tree["node"]["size"] == 4050


def test_rescan_rejects_unknown_path(sample_tree):
    with _client() as client:
        job = _scan(client, sample_tree)
        response = client.post("/api/rescan", json={
            "job": job["id"], "path": os.path.join(sample_tree, "нет-такой"),
        })
        assert response.status_code == 404


def test_delete_many_counts_missing_as_done(sample_tree):
    """«Объекта уже нет» не должно попадать в ошибки."""
    with _client() as client:
        job = _scan(client, sample_tree)
        data = client.post("/api/delete-many", json={
            "job": job["id"],
            "paths": [os.path.join(sample_tree, "a.txt"),
                      os.path.join(sample_tree, "нет-такого")],
        }).json()

        assert data["deleted"] == 1
        assert data["already_gone"] == 1
        assert data["failed_count"] == 0
        assert data["failures"] == []


def test_trash_lists_every_trash_dir(sample_tree):
    """Корзина у вложенной папки тоже должна попасть в общий список."""
    inner = os.path.join(sample_tree, "docs")
    with _client() as client:
        job = _scan(client, sample_tree)
        client.post("/api/delete", json={"job": job["id"],
                                         "path": os.path.join(inner, "b.txt")})

        data = client.get(f"/api/trash?job={job['id']}").json()
        assert data["available"] is True
        assert len(data["entries"]) == 1
        assert data["dirs"]
