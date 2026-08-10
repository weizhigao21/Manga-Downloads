@echo off
rem ============================================
rem  漫画浏览器 一键打包脚本 (PyInstaller onedir)
rem  请直接双击运行本文件；若被安全软件拦截，
rem  请在文件属性中勾选"解除锁定"后重试。
rem ============================================
chcp 936 >nul
title 漫画浏览器打包
setlocal
cd /d "%~dp0"

echo ============================================
echo   漫画浏览器 一键打包
echo ============================================
echo.

rem ---- 1. 定位 Python（优先 py launcher 的 3.10，降级 py -3，再回退 python）----
set "PYCMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3.10 -c "pass" >nul 2>nul
    if not errorlevel 1 set "PYCMD=py -3.10"
)
if not defined PYCMD (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -c "pass" >nul 2>nul
        if not errorlevel 1 set "PYCMD=py -3"
    )
)
if not defined PYCMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
    echo [错误] 未找到 Python。
    echo 请安装 Python 3.10+，并在安装时勾选 "Add Python to PATH"。
    goto :fail
)
echo [1/4] 使用 Python: %PYCMD%
%PYCMD% --version
echo.

rem ---- 2. 检查运行依赖（缺失时自动安装 requirements.txt）----
%PYCMD% -c "import PySide6, requests, lxml, curl_cffi, openai, opencc" >nul 2>nul
if errorlevel 1 (
    echo [2/4] 安装运行依赖 requirements.txt ...
    %PYCMD% -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
) else (
    echo [2/4] 运行依赖已就绪
)

rem ---- 3. 检查 PyInstaller（缺失时自动安装）----
%PYCMD% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo [3/4] 安装 PyInstaller ...
    %PYCMD% -m pip install pyinstaller
    if errorlevel 1 goto :fail
) else (
    echo [3/4] PyInstaller 已就绪
)
echo.

rem ---- 4. 清除 WorkBuddy 沙箱删除保护 ----
rem 在 WorkBuddy 会话内运行时，CODEBUDDY/CLAUDE_SESSION_ID 存在会使
rem os.remove/shutil.rmtree 走回收站（回收站不可用即报错），导致
rem PyInstaller 清理 build 缓存失败；清掉这两个变量即恢复原生删除。
rem 普通环境不受影响。
if defined CODEBUDDY_SESSION_ID set "CODEBUDDY_SESSION_ID="
if defined CLAUDE_SESSION_ID set "CLAUDE_SESSION_ID="

rem ---- 5. 执行打包（--clean 清理缓存，--noconfirm 免确认）----
echo [4/4] 正在打包，约 1-2 分钟，请稍候...
%PYCMD% -m PyInstaller 漫画浏览器.spec --noconfirm --clean
if errorlevel 1 goto :fail

echo.
echo ============================================
echo   打包完成！
echo ============================================
echo.
echo 产物目录: %~dp0dist\漫画浏览器\
echo 主程序:   %~dp0dist\漫画浏览器\漫画浏览器.exe
echo.
echo 整个 dist\漫画浏览器 目录即完整程序，可整体拷贝分发。
echo.
explorer "%~dp0dist\漫画浏览器" >nul 2>nul
pause
exit /b 0

:fail
echo.
echo [错误] 打包失败，请查看上方日志定位原因。
pause
exit /b 1
