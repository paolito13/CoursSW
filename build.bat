@echo off
echo === CourSW Build ===
echo.

echo Installation des dependances...
pip install -r requirements.txt

echo.
echo Compilation de l'exe...
python -m PyInstaller --onefile --windowed ^
  --name "CourSW" ^
  --hidden-import "requests" ^
  --hidden-import "mss" ^
  --hidden-import "PIL" ^
  --hidden-import "PIL.Image" ^
  --hidden-import "win32gui" ^
  --hidden-import "win32con" ^
  --hidden-import "win32process" ^
  --hidden-import "winsdk" ^
  --hidden-import "winsdk.windows.media.ocr" ^
  --hidden-import "winsdk.windows.graphics.imaging" ^
  --hidden-import "winsdk.windows.storage.streams" ^
  --hidden-import "asyncio" ^
  --collect-all "winsdk" ^
  --hidden-import "pystray" ^
  --collect-all "pystray" ^
  --hidden-import "psutil" ^
  main.py

echo.
echo Build termine ! L'exe se trouve dans dist\CourSW.exe
pause
