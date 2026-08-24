"""Тесты запускающих файлов: они должны быть на месте и быть корректными.

Проверить настоящий двойной клик тут нельзя, но большинство поломок этих
файлов — синтаксические: тетрадка перестала быть валидным JSON, в ячейке
опечатка, у скрипта пропало право на исполнение.
"""

from __future__ import annotations

import io
import json
import os
import socket
import stat
import sys
import time
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


# Типичная простыня pip: разведка метаданных, скачивание колёс, установка.
PIP_OUTPUT = """Collecting fastapi>=0.110
  Using cached fastapi-0.141.1-py3-none-any.whl.metadata (27 kB)
Collecting starlette>=0.46.0 (from fastapi>=0.110)
  Using cached starlette-1.6.0-py3-none-any.whl.metadata (6.4 kB)
Requirement already satisfied: typing_extensions>=4.8.0 in /usr/lib (4.16.0)
Using cached fastapi-0.141.1-py3-none-any.whl (131 kB)
Using cached starlette-1.6.0-py3-none-any.whl (75 kB)
Installing collected packages: starlette, fastapi
Successfully installed fastapi-0.141.1 starlette-1.6.0
"""


class FakePip:
    """Подделка процесса pip: отдаёт заготовленный вывод и код возврата."""

    def __init__(self, output: str = PIP_OUTPUT, code: int = 0):
        self.stdout = io.StringIO(output)
        self._code = code

    def wait(self, timeout=None) -> int:
        return self._code

    def poll(self) -> int:
        return self._code


class SilentProgress:
    """Прогресс-бар без рисования: в тесте важны цифры, а не строка."""

    def __init__(self) -> None:
        self.done = self.total = 0
        self.notes: list[str] = []

    def update(self, *, done=None, total=None, note=None) -> None:
        if done is not None:
            self.done = done
        if total is not None:
            self.total = max(total, self.done)
        if note is not None:
            self.notes.append(note)

    def close(self) -> None:
        pass


@pytest.fixture
def pip_log(monkeypatch, tmp_path):
    """Каталог, куда уйдёт лог pip: в рабочем дереве ему при тестах не место."""
    monkeypatch.setattr(notebook, "find_project_root", lambda *a, **kw: tmp_path)
    return tmp_path / notebook.PIP_LOG_NAME


def test_launcher_installs_through_the_portal_index(monkeypatch, capsys, pip_log):
    """С PyPI из контейнера связи нет: только индекс портала и только по токену."""
    seen: list[dict] = []

    def fake_popen(command, **kw):
        seen.append({"command": command, "env": kw.get("env", {})})
        return FakePip()

    monkeypatch.setattr(notebook.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(notebook, "missing_modules", lambda modules: [])
    notebook.install(["fastapi>=0.110"], "s3cret", modules=["fastapi"])

    env = seen[0]["env"]
    assert env["PIP_INDEX_URL"] == (
        f"https://token:s3cret@{notebook.INDEX_HOST}{notebook.INDEX_PATH}")
    assert env["PIP_TRUSTED_HOST"] == notebook.INDEX_HOST
    # Токен в argv видно через `ps` любому соседу по контейнеру.
    assert "s3cret" not in " ".join(seen[0]["command"])
    # И не должен попасть в вывод ячейки: тот сохраняется прямо в .ipynb.
    printed = capsys.readouterr().out
    assert "s3cret" not in printed
    assert "***" in printed and "PIP_INDEX_URL" in printed


def test_launcher_pip_env_survives_a_capricious_index():
    """Индекс портала обрывает соединения — pip должен повторять, а не сдаваться."""
    env = notebook._pip_env("s3cret")
    assert int(env["PIP_RETRIES"]) >= 3
    assert int(env["PIP_TIMEOUT"]) > 0
    assert env["PYTHONUNBUFFERED"] == "1"


def test_launcher_install_shows_progress_instead_of_pip_output(monkeypatch, capsys, pip_log):
    """Полсотни строк «Collecting …» в ячейке — не отчёт, а мусор."""
    monkeypatch.setattr(notebook.subprocess, "Popen", lambda command, **kw: FakePip())
    monkeypatch.setattr(notebook, "missing_modules", lambda modules: [])
    notebook.install(["fastapi>=0.110"], "")

    shown = capsys.readouterr()
    everything = shown.out + shown.err     # tqdm пишет в stderr, запасной бар — в stdout
    assert "Collecting" not in everything
    assert "Using cached" not in everything


def test_launcher_progress_counts_packages_not_metadata():
    """Знаменатель — найденные пакеты, числитель — приехавшие; .metadata не в счёт."""
    progress = SilentProgress()
    log = notebook._follow_pip(io.StringIO(PIP_OUTPUT), progress)

    assert (progress.done, progress.total) == (3, 3)
    assert len(log) == PIP_OUTPUT.count("\n"), "лог нужен целиком — по нему разбирают ошибки"


def test_launcher_progress_survives_without_tqdm(monkeypatch, capsys, pip_log):
    """В голом контейнере tqdm может не быть — бар всё равно должен рисоваться."""
    monkeypatch.setitem(sys.modules, "tqdm.auto", None)
    monkeypatch.setattr(notebook.subprocess, "Popen", lambda command, **kw: FakePip())
    monkeypatch.setattr(notebook, "missing_modules", lambda modules: [])
    notebook.install(["fastapi>=0.110"], "")

    assert "2/3" in capsys.readouterr().out


def test_launcher_install_writes_the_full_output_to_a_log(monkeypatch, pip_log):
    """Прогресс-бар показывает одну строку — остальное должно найтись в логе."""
    monkeypatch.setattr(notebook.subprocess, "Popen", lambda command, **kw: FakePip())
    monkeypatch.setattr(notebook, "missing_modules", lambda modules: [])
    notebook.install(["fastapi>=0.110"], "s3cret")

    written = pip_log.read_text(encoding="utf-8")
    assert "Collecting fastapi>=0.110" in written
    assert "s3cret" not in written, "в логе остаётся заголовок команды — токен в нём не нужен"


def test_launcher_install_trusts_imports_over_the_exit_code(monkeypatch, capsys, pip_log):
    """pip умеет вернуть ноль, положив пакет туда, где сервер его не увидит."""
    monkeypatch.setattr(notebook.subprocess, "Popen", lambda command, **kw: FakePip())
    monkeypatch.setattr(notebook, "missing_modules", lambda modules: ["fastapi"])
    with pytest.raises(RuntimeError, match="не импортируются: fastapi"):
        notebook.install(["fastapi>=0.110"], "s3cret", modules=["fastapi"])

    # И наоборот: ошибка pip из-за постороннего конфликта — не повод вставать,
    # если нужные модули на месте.
    monkeypatch.setattr(notebook.subprocess, "Popen",
                        lambda command, **kw: FakePip(code=1))
    monkeypatch.setattr(notebook, "missing_modules", lambda modules: [])
    notebook.install(["fastapi>=0.110"], "s3cret", modules=["fastapi"])
    assert "продолжаю" in capsys.readouterr().out


def test_launcher_install_reports_failure(monkeypatch, capsys, pip_log):
    failure = "Collecting nosuchpkg\nERROR: No matching distribution found\n"
    monkeypatch.setattr(notebook.subprocess, "Popen",
                        lambda command, **kw: FakePip(failure, code=1))
    monkeypatch.setattr(notebook, "missing_modules", lambda modules: ["fastapi"])
    with pytest.raises(RuntimeError, match="зависимости портала") as failed:
        notebook.install(["fastapi"], "s3cret", modules=["fastapi"])

    # Прогресс-бар прячет вывод pip — при ошибке он должен вернуться.
    assert "ERROR: No matching distribution found" in capsys.readouterr().out
    assert "нужной версии" in str(failed.value), "у частых причин должна быть подсказка"


def test_launcher_hints_at_a_stale_token():
    log = ["ERROR: 401 Client Error: Unauthorized for url: https://sberosc..."]
    assert "токен" in notebook._hint(log).lower()
    assert notebook._hint(["Successfully installed fastapi-0.141.1"]) == ""


def test_launcher_kills_pip_that_hangs():
    """Ячейка, висящая до конца сессии, хуже внятной ошибки."""
    progress = SilentProgress()
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    started = time.monotonic()
    code, log, expired = notebook._run_pip(command, dict(os.environ), progress, timeout=1)

    assert expired is True
    assert time.monotonic() - started < 20, "процесс должен быть убит, а не дождан"
    assert code != 0


def test_launcher_install_does_not_leave_pip_behind(monkeypatch, pip_log):
    """Прервали ячейку — pip не должен пережить её и дальше грызть индекс."""
    killed: list[bool] = []

    class Interrupting(FakePip):
        def wait(self, timeout=None):
            raise KeyboardInterrupt

        def poll(self):
            return None

        def terminate(self):
            killed.append(True)

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(notebook.subprocess, "Popen", lambda command, **kw: Interrupting())
    with pytest.raises(KeyboardInterrupt):
        notebook.install(["fastapi"], "")

    assert killed, "процесс pip остался жив"


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
