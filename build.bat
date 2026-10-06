@echo off
rem Build photo-organizer.exe (single file, no console) with PyInstaller.
rem Usage: build.bat          -> dist\photo-organizer.exe
rem Before building: Get-Process photo-organizer (do not overwrite an exe the user has open),
rem and keep dist\settings.json (it holds the user's undo log path).
setlocal
cd /d "%~dp0"

python -m PyInstaller --version >nul 2>&1 || python -m pip install pyinstaller
python -m pip install -r requirements.txt

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name photo-organizer ^
  --icon "assets\icon.ico" ^
  --add-data "assets\icon.png;assets" ^
  --add-data "lang;lang" ^
  --collect-data tkinterdnd2 ^
  --collect-data sv_ttk ^
  --collect-all pillow_heif ^
  main.py

if errorlevel 1 (
  echo Build failed.
  exit /b 1
)
echo.
echo Done: dist\photo-organizer.exe
endlocal
