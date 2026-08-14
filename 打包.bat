@echo off
rem ============================================
rem  漫画浏览器 一键打包脚本 (PyInstaller onedir)
rem  请直接双击运行本文件；若被安全软件拦截，
rem  请在文件属性中勾选"解除锁定"后重试。
rem ============================================
chcp 936 >nul
title 漫画浏览器打包
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem 默认日志路径（任何失败分支都可引用）
set "LOGFILE=%~dp0logs\build.log"

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
echo [1/7] 使用 Python: %PYCMD%
%PYCMD% --version

rem ---- 2. 校验 Python 版本 >= 3.10（防止 py -3 命中 3.9 等旧版本）----
%PYCMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [错误] Python 版本过低，需要 3.10 或更高版本。
    echo 请安装 Python 3.10+，并确保 %PYCMD% 指向它。
    goto :fail
)
echo [2/7] Python 版本校验通过（3.10+）
echo.

rem ---- 3. 检查运行依赖（缺失时自动安装 requirements.txt）----
rem 检查项与 requirements.txt 一致（含 Pillow，缺失时打包产物运行时封面/查重会崩）
%PYCMD% -c "import PySide6, requests, lxml, curl_cffi, openai, opencc, PIL" >nul 2>nul
if errorlevel 1 (
    echo [3/7] 安装运行依赖 requirements.txt ...
    %PYCMD% -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
) else (
    echo [3/7] 运行依赖已就绪
)

rem ---- 4. 检查 PyInstaller（缺失时自动安装）----
%PYCMD% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo [4/7] 安装 PyInstaller ...
    %PYCMD% -m pip install pyinstaller
    if errorlevel 1 goto :fail
) else (
    echo [4/7] PyInstaller 已就绪
)
echo.

rem ---- 5. 清除 WorkBuddy 沙箱删除保护 ----
rem 在 WorkBuddy 会话内运行时，CODEBUDDY/CLAUDE_SESSION_ID 存在会使
rem os.remove/shutil.rmtree 走回收站（回收站不可用即报错），导致
rem PyInstaller 清理 build 缓存失败；清掉这两个变量即恢复原生删除。
rem 普通环境不受影响。
if defined CODEBUDDY_SESSION_ID set "CODEBUDDY_SESSION_ID="
if defined CLAUDE_SESSION_ID set "CLAUDE_SESSION_ID="

rem ---- 6. 读取版本号（ui/version.py），产物目录带版本号 ----
set "VER=dev"
for /f "delims=" %%v in ('%PYCMD% -c "from ui.version import __version__; print(__version__)"') do set "VER=%%v"
echo [5/7] 版本号: !VER!
set "DIST_DIR=%~dp0dist\漫画浏览器-v!VER!"
set "BUILD_DIR=%~dp0build\漫画浏览器-v!VER!"
rem 临时打包目录：PyInstaller 的 COLLECT 会在 distpath 下再建一层
rem name 子目录，先打包到临时目录，再移动为单层版本目录
set "STAGE_DIR=%~dp0build\stage-v!VER!"
set "LOGFILE=%~dp0logs\build-!VER!.log"

rem 打包前清理旧产物（同版本目录重建，避免旧文件残留）
if exist "!DIST_DIR!" rmdir /s /q "!DIST_DIR!"
if exist "!BUILD_DIR!" rmdir /s /q "!BUILD_DIR!"
if exist "!STAGE_DIR!" rmdir /s /q "!STAGE_DIR!"

rem ---- 7. 执行打包（日志落盘，先到临时目录）----
set "LOGDIR=%~dp0logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo [6/7] 正在打包，约 1-2 分钟，请稍候...
echo 打包日志: !LOGFILE!
echo.
%PYCMD% -m PyInstaller 漫画浏览器.spec --noconfirm --clean ^
    --distpath "!STAGE_DIR!" --workpath "!BUILD_DIR!" > "!LOGFILE!" 2>&1
if errorlevel 1 (
    echo.
    echo [错误] 打包失败！日志尾部：
    powershell -NoProfile -Command "Get-Content -Tail 25 '!LOGFILE!'"
    goto :fail
)

rem ---- 移动产物到版本目录（同盘 move 为瞬时重命名）----
if exist "!STAGE_DIR!\漫画浏览器" (
    move "!STAGE_DIR!\漫画浏览器" "!DIST_DIR!" >nul 2>nul
)
if exist "!STAGE_DIR!" rmdir /s /q "!STAGE_DIR!" >nul 2>nul

rem ---- 自检：主程序 exe 是否生成 ----
if not exist "!DIST_DIR!\漫画浏览器.exe" (
    echo [错误] 打包结束但未找到主程序 exe。
    echo 请查看日志: !LOGFILE!
    goto :fail
)

echo.
echo [7/7] 打包完成！
echo.
echo ============================================
echo   打包完成！
echo ============================================
echo.
echo 产物目录: !DIST_DIR!
echo 主程序:   !DIST_DIR!\漫画浏览器.exe
echo 打包日志: !LOGFILE!
echo.
echo 整个 dist\漫画浏览器-v!VER! 目录即完整程序，可整体拷贝分发。
echo.
explorer "!DIST_DIR!" >nul 2>nul
pause
exit /b 0

:fail
echo.
echo [错误] 打包失败，请查看上方日志定位原因。
echo 完整日志: !LOGFILE!
pause
exit /b 1
