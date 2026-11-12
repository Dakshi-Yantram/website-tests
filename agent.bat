@echo off
rem Windows shortcut for the approval-gated YAML agent:  agent.bat validate | discover | draft | approve | run ...
call .venv\Scripts\activate
python agent.py %*
