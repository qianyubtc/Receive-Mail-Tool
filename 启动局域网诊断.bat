@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"

if not exist "mailtool.py" goto incomplete
if not exist "web\index.html" goto incomplete
if exist "runtime\python.exe" goto use_embedded

py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 goto use_py

python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 goto use_python

echo 未检测到 Python 3.9 或更高版本。
echo 免安装版应包含 runtime 文件夹，请重新完整解压免安装 ZIP。
echo 如果使用源码目录，也可自行安装 Python 后再启动。
pause
exit /b 1

:use_embedded
"%~dp0runtime\python.exe" -X utf8 -B "%~dp0mailtool.py" --open --diagnostics
set "MAILTOOL_EXIT=%ERRORLEVEL%"
goto finished

:use_py
py -3 -X utf8 -B mailtool.py --open --diagnostics
set "MAILTOOL_EXIT=%ERRORLEVEL%"
goto finished

:use_python
python -X utf8 -B mailtool.py --open --diagnostics
set "MAILTOOL_EXIT=%ERRORLEVEL%"
goto finished

:incomplete
echo 文件不完整，请先将 ZIP 全部解压到一个文件夹，再运行本文件。
pause
exit /b 1

:finished
if "%MAILTOOL_EXIT%"=="0" exit /b 0
echo.
echo 启动失败，请查看上方提示。若端口被占用，可在当前文件夹的终端运行：
echo runtime\python.exe -X utf8 -B mailtool.py --port 8766 --open --diagnostics
pause
exit /b 1
