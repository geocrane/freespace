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
    notebook = json.loads((PROJECT / "start.ipynb").read_text(encoding="utf-8"))

    assert notebook["nbformat"] == 4
    code_cells = [c for c in notebook["cells"] if c["cell_type"] == "code"]
    assert len(code_cells) >= 2, "нужны ячейки запуска и остановки"
    for index, cell in enumerate(code_cells):
        source = "".join(cell["source"])
        compile(source, f"start.ipynb#{index}", "exec")


def test_notebook_knows_the_container_domain():
    source = (PROJECT / "start.ipynb").read_text(encoding="utf-8")
    assert "jupyterhub-datalab.apps.prom-datalab.ca.sbrf.ru" in source
    assert "proxy/" in source, "адрес за jupyter-server-proxy должен собираться"


def test_notebook_installs_through_the_portal_index():
    """С PyPI из контейнера связи нет: только индекс портала и только по токену."""
    notebook = json.loads((PROJECT / "start.ipynb").read_text(encoding="utf-8"))
    cells = ["".join(c["source"]) for c in notebook["cells"]]

    token_cell = next((i for i, src in enumerate(cells)
                       if src.lstrip().startswith("#") and "TOKEN = " in src), None)
    assert token_cell is not None, "нужна ячейка с токеном"

    install_cell = next(i for i, src in enumerate(cells) if "sberosc.ca.sbrf.ru" in src)
    assert token_cell < install_cell, "токен задаётся до установки, иначе он не виден"

    source = cells[install_cell]
    assert "--index-url=https://token:" in source
    assert "--trusted-host=" in source
    # Токен не должен попасть в вывод ячейки: тот сохраняется прямо в .ipynb.
    assert 'replace(token, "***")' in source


def test_notebook_outputs_carry_no_token():
    """Файл в репозитории не должен хранить чужой токен в выводе ячеек."""
    notebook = json.loads((PROJECT / "start.ipynb").read_text(encoding="utf-8"))
    for cell in notebook["cells"]:
        for output in cell.get("outputs", []):
            text = json.dumps(output, ensure_ascii=False)
            assert "token:" not in text


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
