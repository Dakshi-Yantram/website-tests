@echo off
python -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
if not exist .env copy .env.example .env
echo.
echo Done. Open .env in Notepad and fill in the 4 passwords, then run:  run_tests.bat
