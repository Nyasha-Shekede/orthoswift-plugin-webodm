@echo off
set "SCRIPT=%~dp0orthoswift\install.py"
if not exist "%SCRIPT%" set "SCRIPT=%~dp0install.py"
where py >nul 2>nul || (echo Python launcher not found. Install 64-bit Python 3.12 first. & pause & exit /b 1)
py -3.12 "%SCRIPT%" && goto done
py -3.11 "%SCRIPT%" && goto done
py -3.10 "%SCRIPT%" && goto done
echo OrthoSWIFT requires 64-bit Python 3.10, 3.11, or 3.12.
pause
exit /b 1
:done
echo Installation complete. Restart Metashape.
pause
