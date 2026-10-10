@echo off
rem 从源码启动 windowsAI灵动岛（需要已安装 Python 3.10+，并 pip install -r requirements.txt）
cd /d "%~dp0"
start "" pythonw "%~dp0notch.pyw"
