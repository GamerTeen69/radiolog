@echo off
setlocal
cd /d "%~dp0"

call :findpy
if defined PY goto run

echo Python 3 is not installed on this computer.
echo (The "python" command in Windows is only a shortcut to the Microsoft Store.)
echo.
where winget >nul 2>nul
if errorlevel 1 goto manual

choice /c YN /m "Install Python 3.13 now (official python.org build, via winget)"
if errorlevel 2 goto manual

winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements
call :findpy
if defined PY goto run
echo.
echo Python was installed, but this window can't see it yet.
echo Close this window and double-click install.bat again.
goto end

:manual
echo.
echo Install Python 3 from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during setup), then run install.bat again.
goto end

:run
%PY% install.py %*
goto end

:findpy
rem Each check actually runs Python, so the Microsoft Store placeholder is skipped.
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3" && exit /b
python -c "import sys" >nul 2>nul && set "PY=python" && exit /b
if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" (
    "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3 -c "import sys" >nul 2>nul && set PY="%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3 && exit /b
)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%~fD\python.exe" set PY="%%~fD\python.exe"
)
exit /b

:end
echo.
pause
