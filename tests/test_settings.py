"""Тесты настроек и замка: конфиг, PIN, признак «сетевой путь»."""

from __future__ import annotations

import os

import pytest

from freespace.core import env
from freespace.core import settings as settings_module
from freespace.core.settings import NetworkRoot, Settings
from freespace.service.access import (
    AccessGuard,
    LockedError,
    PinNotSet,
    TooManyAttempts,
    WrongPin,
)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("FREESPACE_CONFIG", str(path))
    return path


# --- файл настроек ----------------------------------------------------------


def test_missing_config_is_not_an_error(config_file):
    """Приложение обязано открываться и без конфига — с локальными дисками."""
    assert settings_module.load().network_roots == []


def test_broken_config_is_not_an_error(config_file):
    config_file.write_text("{это не json", encoding="utf-8")
    assert settings_module.load().network_roots == []


def test_roundtrip(config_file):
    settings_module.save(Settings(
        network_roots=[NetworkRoot(path=r"\\srv\share", label="Отдел")]))

    loaded = settings_module.load()
    assert [r.path for r in loaded.network_roots] == [r"\\srv\share"]
    assert loaded.network_roots[0].label == "Отдел"


# --- PIN из .env ------------------------------------------------------------


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("FREESPACE_ENV", str(path))
    monkeypatch.delenv("FREESPACE_PIN", raising=False)
    return path


def test_env_file_is_created_with_the_default_pin(env_file):
    """Настройка не должна требовать консоли: файл появляется сам."""
    path, created = env.ensure_file()
    assert created and os.path.exists(path)
    assert settings_module.configured_pin() == env.DEFAULT_PIN == "admin"

    # Повторный запуск не затирает то, что человек уже поправил.
    env_file.write_text("FREESPACE_PIN=свой\n", encoding="utf-8")
    _path, created_again = env.ensure_file()
    assert not created_again
    assert settings_module.configured_pin() == "свой"


def test_pin_is_read_from_the_file(env_file):
    env_file.write_text("# комментарий\n\nFREESPACE_PIN=  2468  \n", encoding="utf-8")
    assert settings_module.verify_pin("2468")
    assert not settings_module.verify_pin("1234")


def test_quotes_around_the_value_are_dropped(env_file):
    """«admin» и «"admin"» человек считает одним и тем же."""
    env_file.write_text('FREESPACE_PIN="admin"\n', encoding="utf-8")
    assert settings_module.configured_pin() == "admin"


def test_environment_wins_over_the_file(env_file, monkeypatch):
    env_file.write_text("FREESPACE_PIN=изфайла\n", encoding="utf-8")
    monkeypatch.setenv("FREESPACE_PIN", "изокружения")
    assert settings_module.configured_pin() == "изокружения"


def test_empty_pin_means_nothing_opens(env_file):
    env_file.write_text("FREESPACE_PIN=\n", encoding="utf-8")
    assert not settings_module.has_pin()
    assert not settings_module.verify_pin("")
    assert not settings_module.verify_pin("admin")


def test_broken_env_file_is_not_an_error(env_file):
    env_file.write_text("совсем не пары ключ-значение\n[секция]\n", encoding="utf-8")
    assert env.read_file() == {}


# --- что считается сетевым --------------------------------------------------


def test_unc_path_is_network_even_without_the_list():
    """Иначе замок обходится вписыванием пути руками."""
    assert settings_module.is_network_path(r"\\srv\share\folder", Settings())


def test_path_inside_a_listed_root_is_network(tmp_path):
    settings = Settings(network_roots=[NetworkRoot(path=str(tmp_path))])
    assert settings_module.is_network_path(str(tmp_path / "внутри"), settings)
    assert settings_module.is_network_path(str(tmp_path), settings)


def test_local_path_is_not_network(tmp_path):
    settings = Settings(network_roots=[NetworkRoot(path=str(tmp_path / "шара"))])
    assert not settings_module.is_network_path(str(tmp_path / "своё"), settings)
    assert not settings_module.is_network_path("", settings)


def test_sibling_with_a_common_prefix_is_not_inside(tmp_path):
    """«/data/share» не должен захватывать «/data/share-old»."""
    settings = Settings(network_roots=[NetworkRoot(path=str(tmp_path / "share"))])
    assert not settings_module.is_network_path(str(tmp_path / "share-old"), settings)


# --- замок ------------------------------------------------------------------


@pytest.fixture
def pinned(monkeypatch):
    """PIN задан — как в рабочем запуске, где он лежит в .env."""
    monkeypatch.setenv("FREESPACE_PIN", "1234")


def _guard(tmp_path):
    return AccessGuard(Settings(network_roots=[NetworkRoot(path=str(tmp_path))]))


def test_local_paths_pass_without_a_pin(tmp_path, pinned):
    guard = _guard(tmp_path)
    guard.check(os.sep + "где-то-ещё", token="")  # не поднимает исключение


def test_network_path_is_locked_until_the_pin(tmp_path, pinned):
    guard = _guard(tmp_path)
    with pytest.raises(LockedError):
        guard.check(str(tmp_path / "файл"), token="")

    token, ttl = guard.unlock("1234")
    assert ttl > 0
    guard.check(str(tmp_path / "файл"), token)


def test_wrong_pin_gives_nothing(tmp_path, pinned):
    guard = _guard(tmp_path)
    with pytest.raises(WrongPin):
        guard.unlock("0000")
    assert not guard.is_unlocked("что-угодно")


def test_lock_closes_the_door_again(tmp_path, pinned):
    guard = _guard(tmp_path)
    token, _ttl = guard.unlock("1234")
    assert guard.lock(token)
    with pytest.raises(LockedError):
        guard.check(str(tmp_path / "файл"), token)


def test_expired_token_stops_working(tmp_path, pinned):
    guard = _guard(tmp_path)
    guard.ttl = -1.0  # уже протух
    token, _ttl = guard.unlock("1234")
    assert not guard.is_unlocked(token)


def test_guessing_is_slowed_down(tmp_path, pinned):
    guard = _guard(tmp_path)
    guard.max_attempts = 3
    for _ in range(3):
        with pytest.raises(WrongPin):
            guard.unlock("0000")
    # Дальше пауза — и она распространяется даже на верную комбинацию.
    with pytest.raises(TooManyAttempts):
        guard.unlock("1234")


def test_pin_defaults_to_admin_even_without_a_file(tmp_path, config_file):
    """Файл ещё не создан (или создать не вышло) — замок всё равно рабочий."""
    guard = AccessGuard(Settings(network_roots=[NetworkRoot(path=str(tmp_path))]))
    token, _ttl = guard.unlock(env.DEFAULT_PIN)
    guard.check(str(tmp_path / "файл"), token)


def test_erased_pin_leaves_the_door_shut(tmp_path, monkeypatch):
    """Строку с PIN стёрли — сетевые папки не открываются вовсе, а не всем."""
    monkeypatch.setenv("FREESPACE_PIN", "")
    guard = AccessGuard(Settings(network_roots=[NetworkRoot(path=str(tmp_path))]))
    with pytest.raises(PinNotSet):
        guard.unlock("admin")
    with pytest.raises(LockedError):
        guard.check(str(tmp_path / "файл"), token="")


def test_roots_are_saved_between_runs(config_file, tmp_path):
    guard = AccessGuard(Settings())
    guard.add_root(str(tmp_path), "Общий")
    assert [r.path for r in settings_module.load().network_roots] == [str(tmp_path)]

    assert guard.remove_root(str(tmp_path))
    assert settings_module.load().network_roots == []
    assert not guard.remove_root(str(tmp_path))
