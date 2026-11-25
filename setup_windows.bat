@echo off
setlocal
cd /d "%~dp0"
py -3.11 -m venv .venv
if errorlevel 1 goto fail
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto fail
pip install -r requirements.txt
if errorlevel 1 goto fail
if not exist .env copy .env.example .env
if errorlevel 1 goto fail
echo.
echo Setup complete. Edit .env, then run start_9n8.bat.
pause
exit /b 0
:fail
echo Setup failed. Install 64-bit Python 3.11 and try again.
pause
exit /b 1
