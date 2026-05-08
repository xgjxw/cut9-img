@echo off
setlocal
cd /d %~dp0

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --icon assets\app_icon.ico ^
  --add-data assets\app_icon.ico;assets ^
  --name meme-gui ^
  run_gui.py

echo.
echo Build finished.
echo EXE: %~dp0dist\meme-gui.exe
endlocal
