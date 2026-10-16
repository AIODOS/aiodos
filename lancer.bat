@echo off
REM AIODOS : lance le noeud local et sa fenetre (sans console).
cd /d "%~dp0"
set "PYW=%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe"
if exist "%PYW%" (
  start "" "%PYW%" -m aiodos
) else (
  start "" pythonw -m aiodos
)
