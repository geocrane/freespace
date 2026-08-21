"""Тесты запускающих файлов: они должны быть на месте и быть корректными.

Проверить настоящий двойной клик тут нельзя, но большинство поломок этих
файлов — синтаксические: тетрадка перестала быть валидным JSON, в ячейке
опечатка, у скрипта пропало право на исполнение.
"""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path

import pytest

from freespace import notebook
from freespace.web.__main__ import find_free_port, proxy_prefix

PROJECT = Path(__file__).resolve().parent.parent


def test_all_three_launchers_exist():
    for name in ("start.command", "start.bat", "start.ipynb"):
        assert (PROJECT / name).is_file(), f"нет запускающего файла {name}"


@pytest.mark.skipif(os.name == "nt", reason="права на исполнение — не про Windows")
def test_mac_launcher_is_executable():
    """Без бита исполнения двойной клик в Finder ничего не запустит."""
    mode = (PROJECT / "start.command").stat().st_mode
    assert mode & stat.S_IXUSR


def test_notebook_is_valid_and_runnable():
    nb = json.loads((PROJECT / "start.ipynb").read_text(encoding="utf-8"))

    assert nb["nbformat"] == 4
    code_cells = [c for c in nb["cells"] if c["cell_type"] == "code"]
    assert len(code_cells) >= 2, "нужны ячейки запуска и остановки"
    for index, cell in enumerate(code_cells):
        source = "".join(cell["source"])
        compile(source, f"start.ipynb#{index}", "exec")


def test_notebook_keeps_the_code_in_the_module():
    """Тетрадку тиражируют на людей: в ячейках должны быть вызовы, а не реализация."""
    nb = json.loads((PROJECT / "start.ipynb").read_text(encoding="utf-8"))
    cells = ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]

    token_cell = next((i for i, src in enumerate(cells) if "TOKEN = " in src), None)
    assert token_cell is not None, "нужна ячейка с токеном"
    start_cell = next(i for i, src in enumerate(cells) if "start(TOKEN)" in src)
    assert token_cell < start_cell, "токен задаётся до запуска, иначе он не виден"
    assert any("stop()" in src for src in cells), "нет ячейки остановки"

    for src in cells:
        lines = [line for line in src.splitlines() if line.strip()]
        assert len(lines) <= 4, f"ячейка разрослась, её место в freespace/notebook.py:\n{src}"


def test_notebook_shows_the_logo():
    source = (PROJECT / "start.ipynb").read_text(encoding="utf-8")
    assert "logo.svg" in source
    assert (PROJECT / "freespace" / "web" / "static" / "logo.svg").is_file()


def test_notebook_outputs_carry_no_token():
    """Файл в репозитории не должен хранить чужой токен в выводе ячеек."""
    nb = json.loads((PROJECT / "start.ipynb").read_text(encoding="utf-8"))
    for cell in nb["cells"]:
        for output in cell.get("outputs", []):
            text = json.dumps(output, ensure_ascii=False)
            assert "token:" not in text


def test_launcher_knows_the_container_domain():
    source = (PROJECT / "freespace" / "notebook.py").read_text(encoding="utf-8")
    assert "jupyterhub-datalab.apps.prom-datalab.ca.sbrf.ru" in source
    assert "proxy/" in source, "адрес за jupyter-server-proxy должен собираться"


def test_launcher_installs_through_the_portal_index(monkeypatch, capsys):
    """С PyPI из контейнера связи нет: только индекс портала и только по токену."""
    seen: list[list[str]] = []

    class Result:
        returncode = 0

    monkeypatch.setattr(notebook.subprocess, "run",
                        lambda command, *a, **kw: seen.append(command) or Result())
    notebook.install(["fastapi>=0.110"], "s3cret")

    command = " ".join(seen[0])
    assert f"--index-url=https://token:s3cret@{notebook.INDEX_HOST}" in command
    assert f"--trusted-host={notebook.INDEX_HOST}" in command
    # Токен не должен попасть в вывод ячейки: тот сохраняется прямо в .ipynb.
    printed = capsys.readouterr().out
    assert "s3cret" not in printed
    assert "***" in printed


def test_launcher_install_reports_failure(monkeypatch):
    class Result:
        returncode = 1

    monkeypatch.setattr(notebook.subprocess, "run", lambda *a, **kw: Result())
    with pytest.raises(RuntimeError, match="зависимости портала"):
        notebook.install(["fastapi"], "s3cret")


def test_launcher_finds_the_project_root(tmp_path):
    (tmp_path / "freespace" / "web").mkdir(parents=True)
    (tmp_path / "freespace" / "web" / "api.py").touch()
    deep = tmp_path / "notebooks" / "sub"
    deep.mkdir(parents=True)

    assert notebook.find_project_root(deep) == tmp_path.resolve()


def test_launcher_proxy_prefix_always_ends_with_slash(monkeypatch):
    monkeypatch.delenv("JUPYTERHUB_SERVICE_PREFIX", raising=False)
    monkeypatch.setenv("NB_PREFIX", "/notebook/x")
    assert notebook.proxy_prefix() == "/notebook/x/"

    monkeypatch.delenv("NB_PREFIX")
    monkeypatch.setenv("JUPYTERHUB_USER", "ivanov")
    assert notebook.proxy_prefix() == "/user/ivanov/"


def test_launcher_stop_without_server_says_so(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(notebook, "find_project_root", lambda *a: tmp_path)
    assert notebook.stop() is False
    assert "не найдено" in capsys.readouterr().out


def test_find_free_port_returns_bindable_port():
    port = find_free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_find_free_port_skips_busy_one():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        taken = busy.getsockname()[1]
        assert find_free_port(taken) != taken


def test_proxy_prefix_reads_jupyter_env(monkeypatch):
    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/user/ivanov/")
    assert proxy_prefix() == "/user/ivanov/"

    monkeypatch.delenv("JUPYTERHUB_SERVICE_PREFIX")
    monkeypatch.setenv("NB_PREFIX", "/notebook/x")
    assert proxy_prefix() == "/notebook/x"

    monkeypatch.delenv("NB_PREFIX")
    assert proxy_prefix() == ""
