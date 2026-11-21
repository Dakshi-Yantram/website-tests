@echo off
rem Windows shortcut:  login_agent.bat --dry-run | login_agent.bat | login_agent.bat --env staging --headed
call .venv\Scripts\activate
python -m login_agent %*
