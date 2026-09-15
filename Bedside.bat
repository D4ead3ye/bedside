@echo off
rem Launches the real app. PyInstaller also leaves an Bedside.exe in
rem build\Bedside\ that cannot run - it has no _internal folder.
start "" "%~dp0dist\Bedside\Bedside.exe"
