@echo off
chcp 65001 >nul
rem Сборка Gloss.exe и установщика GlossSetup.exe через PyInstaller.
rem Результат: dist\GlossSetup.exe, dist\Gloss.exe, dist\SHA256SUMS.txt
setlocal
cd /d "%~dp0"
rem Python пишет в UTF-8 и при выводе в файл или в журнал GitHub Actions
set PYTHONUTF8=1

if not exist .venv\Scripts\python.exe (
    echo [Gloss] Создаю виртуальное окружение .venv ...
    py -3 -m venv .venv || python -m venv .venv || goto :error
)
set PY=.venv\Scripts\python.exe

echo [Gloss] Устанавливаю зависимости ...
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller || goto :error

echo [Gloss] Записываю адрес репозитория для обновлений ...
%PY% tools\build_info.py || goto :error

echo [Gloss] Генерирую иконку ...
%PY% -c "from ui import export_ico; export_ico(r'build\gloss.ico')" || goto :error

echo [Gloss] Собираю Gloss.exe ...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name Gloss ^
    --icon build\gloss.ico ^
    --hidden-import pystray._win32 ^
    --add-data "assets;assets" ^
    --add-data "CHANGELOG.md;." ^
    main.py || goto :error

echo [Gloss] Собираю установщик ...
%PY% -m PyInstaller --noconfirm --onefile --windowed ^
    --name GlossSetup ^
    --icon build\gloss.ico ^
    --hidden-import pystray._win32 ^
    --add-data "dist\Gloss.exe;payload" ^
    --add-data "assets;assets" ^
    --add-data "CHANGELOG.md;." ^
    installer.py || goto :error

echo [Gloss] Считаю контрольные суммы ...
%PY% tools\checksums.py dist\Gloss.exe dist\GlossSetup.exe > dist\SHA256SUMS.txt || goto :error

echo.
echo [Gloss] Готово:
echo [Gloss]   dist\GlossSetup.exe: установщик для пользователей (ставит движок, модель и Gloss)
echo [Gloss]   dist\Gloss.exe:      сама программа (используется для обновлений и портативного запуска)
echo [Gloss]   dist\SHA256SUMS.txt: контрольные суммы
exit /b 0

:error
echo.
echo [Gloss] Сборка не удалась, смотрите сообщения выше.
exit /b 1
