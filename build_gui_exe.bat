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
  --name meme-workshop ^
  run_gui.py

echo.
echo Build finished.
copy /Y "%~dp0dist\meme-workshop.exe" "%~dp0dist\表情包工坊.exe" >nul
echo EXE: %~dp0dist\meme-workshop.exe
echo EXE: %~dp0dist\表情包工坊.exe
endlocal
