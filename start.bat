@echo off
rem FreeSpace - запуск на Windows. Двойной клик по этому файлу.
rem
rem Скрипт сам находит подходящий Python, создаёт виртуальное окружение, ставит
rem зависимости, подбирает свободный порт и открывает браузер.
rem
rem Переменные читаются только вне блоков if(): внутри блока cmd подставляет
rem значение на разборе строки, а не на выполнении, и всё ломается молча.

chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"
set "NEED=3.10"

echo FreeSpace - подготовка окружения...

if exist "%PY%" goto :deps

rem --- поиск подходящего Python ---------------------------------------------
rem В PATH нередко стоит старая версия - например 3.9, - а рядом установлены
rem свежие. Поэтому после PATH проверяются стандартные места установки, и берётся
rem первая версия не ниже %NEED%.

rem Путь и аргументы храним раздельно: у "py" аргумент -3, у найденного файла
rem аргументов нет, зато путь бывает с пробелами - "C:\Program Files\Python313".
rem Держать всё в одной переменной значит либо потерять пробелы, либо тащить
rem кавычки внутри значения; и то и другое ломается на ровном месте.
set "PYEXE="
set "PYARG="

rem 1) Лаунчер py сам выбирает самую свежую установленную версию.
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PYEXE=py" && set "PYARG=-3"
if defined PYEXE goto :havepy

rem 2) python из PATH - если он достаточно свежий.
python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PYEXE=python"
if defined PYEXE goto :havepy

rem 3) Стандартные места установки, от пользовательских к системным.
call :scandir "%LocalAppData%\Programs\Python"
if defined PYEXE goto :havepy
call :scandir "%ProgramFiles%"
if defined PYEXE goto :havepy
call :scandir "%ProgramFiles(x86)%"
if defined PYEXE goto :havepy
call :scandir "%SystemDrive%"
if defined PYEXE goto :havepy
call :scandir "%ProgramData%"
if defined PYEXE goto :havepy

echo.
echo !! Не найден Python %NEED% или новее.
echo    Искал: в PATH, через лаунчер py, а также в
echo      %LocalAppData%\Programs\Python\Python3*
echo      %ProgramFiles%\Python3*
echo      %ProgramFiles(x86)%\Python3*
echo      %SystemDrive%\Python3*
echo.
echo    Установите Python с python.org и отметьте "Add Python to PATH",
echo    либо положите этот файл рядом с уже установленным python.exe.
goto :fail

:scandir
rem Ищет Python3* в каталоге %~1 и берёт первый подходящий.
rem Сортировка /o-n даёт свежие версии раньше: Python313 перед Python310.
rem "dir /b" отдаёт только имя папки, поэтому пробел в "Program Files" остаётся
rem внутри %~1 и никуда не расползается; все подстановки закавычены.
set "PYDIR=%~1"
if not defined PYDIR goto :eof
if "%PYDIR:~-1%"=="\" set "PYDIR=%PYDIR:~0,-1%"
if not exist "%PYDIR%\" goto :eof
for /f "delims=" %%D in ('dir /b /ad /o-n "%PYDIR%\Python3*" 2^>nul') do (
    if not defined PYEXE (
        if exist "%PYDIR%\%%D\python.exe" (
            "%PYDIR%\%%D\python.exe" -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PYEXE=%PYDIR%\%%D\python.exe"
        )
    )
)
goto :eof

:havepy
echo Найден Python: "%PYEXE%" %PYARG%
echo Создаю виртуальное окружение в %VENV%...
"%PYEXE%" %PYARG% -m venv "%VENV%"
if errorlevel 1 goto :novenv

:deps
rem Ставим зависимости только когда их действительно нет: обычный запуск не
rem должен каждый раз ходить в сеть.
"%PY%" -c "import fastapi, uvicorn" >nul 2>&1
if not errorlevel 1 goto :run
echo Ставлю зависимости (один раз)...
"%PY%" -m pip install --quiet --upgrade pip
"%PY%" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :nodeps

:run
echo Запускаю. Браузер откроется сам.
echo Чтобы остановить - закройте это окно или нажмите Ctrl+C.
echo.
"%PY%" -m freespace.web --port auto --open-browser
if errorlevel 1 goto :fail
goto :eof

:novenv
echo.
echo !! Не удалось создать виртуальное окружение %VENV% через "%PYEXE%".
goto :fail

:nodeps
echo.
echo !! Не удалось поставить зависимости. Проверьте доступ в сеть или
echo    прокси-настройки pip.
goto :fail

:fail
echo.
pause
exit /b 1
