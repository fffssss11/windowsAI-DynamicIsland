@echo off
rem 打包成单个 exe：dist\WindowsAI-Island.exe
rem 需要：pip install -r requirements.txt pyinstaller
cd /d "%~dp0"

rem Anaconda 的 ctypes / ssl / tkinter 依赖的 DLL 在 Library\bin 里，要让 PyInstaller 找得到
for /f "delims=" %%i in ('python -c "import sys; print(sys.prefix)"') do set "PYPREFIX=%%i"
if exist "%PYPREFIX%\Library\bin" set "PATH=%PYPREFIX%\Library\bin;%PATH%"

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name WindowsAI-Island ^
  --icon assets\icon.ico ^
  --hidden-import timezone_float ^
  --collect-data tzdata ^
  --exclude-module numpy --exclude-module matplotlib --exclude-module pandas --exclude-module scipy ^
  --exclude-module IPython --exclude-module PyQt5 --exclude-module PySide6 --exclude-module notebook ^
  notch.pyw
