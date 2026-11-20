@echo off
rem Windows shortcut for the QA agent:  agent.bat auto --app hospital --headed   (autonomous)  |  validate | discover | draft | approve | run ...
call .venv\Scripts\activate
python agent.py %*
