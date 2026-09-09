"""Файл ``.env`` рядом с приложением: настройки, которые правят руками.

Почему отдельный файл, а не тот же ``config.json``. В json живёт то, что
приложение меняет само (список сетевых папок), и лезть туда человеку незачем.
В ``.env`` — наоборот, только то, что задаёт человек, и лежит он на виду рядом с
``start.bat``: открыл блокнотом, поправил строку, перезапустил.

Файл создаётся сам при первом запуске, поэтому настройка не требует ни консоли,
ни установки чего бы то ни было. Из репозитория он исключён (``.gitignore``):
это локальные настройки конкретной машины.

Свой разборщик вместо ``python-dotenv``: нужен разбор ``КЛЮЧ=значение`` с
комментариями, это двадцать строк, а зависимость пришлось бы ставить на каждой
машине, где приложение запускают двойным кликом.
"""

from __future__ import annotations

import os

ENV_FILE_NAME = ".env"

# PIN по умолчанию. Он и должен быть простым: это не пароль от данных, а
# преграда от случайного нажатия — см. README, раздел «Сетевые папки».
DEFAULT_PIN = "admin"
PIN_VAR = "FREESPACE_PIN"

TEMPLATE = f"""# Настройки FreeSpace. Файл читается при запуске приложения:
# поправили строку — перезапустите, чтобы изменения подхватились.

# PIN на сетевые папки. Пока он не введён в окне «Сетевые диски», общие диски
# не показываются вовсе — ни в списке, ни для сканирования, ни для удаления.
#
# Это защита от случайного удаления, а не разграничение доступа к файлам: у
# кого есть права на шару, тот сотрёт оттуда что угодно Проводником, не
# открывая FreeSpace. Поэтому комбинация лежит здесь как есть — меняйте её
# прямо в строке ниже.
{PIN_VAR}={DEFAULT_PIN}
"""


def project_root() -> str:
    """Каталог с ``start.bat`` — туда и кладётся ``.env``."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def env_path() -> str:
    override = os.environ.get("FREESPACE_ENV")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(project_root(), ENV_FILE_NAME)


def ensure_file(path: str | None = None) -> tuple[str, bool]:
    """Создать ``.env`` с настройками по умолчанию, если его нет.

    Возвращает (путь, создали ли сейчас). Нет прав на запись — не беда:
    значения по умолчанию всё равно действуют, приложение работает.
    """
    target = path or env_path()
    if os.path.exists(target):
        return target, False
    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(TEMPLATE)
    except OSError:
        return target, False
    return target, True


def read_file(path: str | None = None) -> dict[str, str]:
    """Разобрать ``.env``. Нет файла или битые строки — просто пусто."""
    values: dict[str, str] = {}
    try:
        with open(path or env_path(), encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return values

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # Кавычки вокруг значения — привычка из shell; PIN «admin» и PIN
        # «"admin"» человек считает одним и тем же.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def value(name: str, default: str = "") -> str:
    """Значение настройки: переменная окружения важнее файла.

    Так запуск с ``FREESPACE_PIN=…`` в окружении перекрывает файл, не трогая
    его, — этим пользуются тесты и запуск на чужой машине.
    """
    from_env = os.environ.get(name)
    if from_env is not None:
        return from_env
    return read_file().get(name, default)


def pin() -> str:
    """PIN на сетевые папки. Пустой — открыть замок нечем."""
    return value(PIN_VAR, DEFAULT_PIN).strip()
