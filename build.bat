@echo off
echo === CourSW Build ===
echo.

echo Installation des dependances...
pip install -r requirements.txt

echo.
echo Nettoyage des fichiers precedents...
if exist CourSW.spec del CourSW.spec
if exist build rmdir /s /q build

echo Compilation de l'exe...
python -m PyInstaller --onedir --windowed ^
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
echo Création du ZIP de distribution...
if exist dist\CourSW.zip del dist\CourSW.zip
powershell -Command "Compress-Archive -Path 'dist\CourSW' -DestinationPath 'dist\CourSW.zip'"

echo.
echo Build termine ! Le dossier est dans dist\CourSW\ et le ZIP dans dist\CourSW.zip
pause
