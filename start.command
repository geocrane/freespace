#!/bin/bash
# FreeSpace — запуск на macOS. Двойной клик по этому файлу в Finder.
#
# Скрипт сам создаёт виртуальное окружение, ставит зависимости, подбирает
# свободный порт и открывает страницу в браузере.

cd "$(dirname "$0")" || exit 1

VENV=".venv"
PY="$VENV/bin/python"

die() {
  echo
  echo "!! $1"
  echo
  echo "Нажмите любую клавишу, чтобы закрыть окно."
  read -r -n 1 -s
  exit 1
}

echo "FreeSpace — подготовка окружения…"

if [ ! -x "$PY" ]; then
  SYSTEM_PY="$(command -v python3)"
  [ -n "$SYSTEM_PY" ] || die "Не найден python3. Установите Python 3.10 или новее с python.org."

  "$SYSTEM_PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Нужен Python 3.10 или новее, а найден $($SYSTEM_PY -V). Установите свежий с python.org."

  echo "Создаю виртуальное окружение в $VENV…"
  "$SYSTEM_PY" -m venv "$VENV" || die "Не удалось создать виртуальное окружение."
fi

# Ставим зависимости только когда их действительно нет: обычный запуск не
# должен каждый раз ходить в сеть.
if ! "$PY" -c 'import fastapi, uvicorn' >/dev/null 2>&1; then
  echo "Ставлю зависимости (один раз)…"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r requirements.txt || die "Не удалось поставить зависимости."
fi

echo "Запускаю. Браузер откроется сам; чтобы остановить — закройте это окно или нажмите Ctrl+C."
echo
"$PY" -m freespace.web --port auto --open-browser
STATUS=$?

[ $STATUS -eq 0 ] || die "Сервер завершился с ошибкой (код $STATUS). Смотрите сообщения выше."
