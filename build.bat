@echo off
setlocal EnableExtensions
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
    echo 错误: 无法进入项目目录 "%~dp0"。
    pause
    endlocal & exit /b 1
)
chcp 65001 >nul
echo ========================================
echo   Nekro-Agent Windows 打包工具
echo ========================================

if not exist "version.txt" (
    echo 错误: 未找到 version.txt。
    goto :build_failed
)
set /p APP_VERSION=<version.txt
if not defined APP_VERSION (
    echo 错误: version.txt 为空。
    goto :build_failed
)
echo   版本号: %APP_VERSION%
echo ========================================
echo.

echo [1/6] 检查 uv 和 Inno Setup...
where uv >nul 2>&1
if errorlevel 1 (
    echo 错误: 未检测到 uv，请先安装 uv 后重试。
    echo 安装说明: https://docs.astral.sh/uv/getting-started/installation/
    goto :build_failed
)

call :find_inno_compiler
if errorlevel 1 (
    goto :build_failed
)

echo.
echo [2/6] 同步依赖...
uv sync --python 3.11 --group dev
if errorlevel 1 (
    echo 错误: 依赖同步失败！
    goto :build_failed
)

echo.
echo [3/6] 清理旧的构建文件...
if exist build rmdir /s /q build
if exist build (
    echo 错误: 无法清理 build 目录，可能有文件被占用。
    goto :build_failed
)
if exist dist rmdir /s /q dist
if exist dist (
    echo 错误: 无法清理 dist 目录，可能有文件被占用。
    goto :build_failed
)
if exist installer rmdir /s /q installer
if exist installer (
    echo 错误: 无法清理 installer 目录，可能有文件被占用。
    goto :build_failed
)
echo 清理完成

echo.
echo [4/6] 打包为 EXE...
uv run --python 3.11 python -m PyInstaller build.spec
if errorlevel 1 (
    echo 错误: 打包失败！
    goto :build_failed
)
if not exist "dist\NekroAgent\NekroAgent.exe" (
    echo 错误: 打包完成但未找到 dist\NekroAgent\NekroAgent.exe。
    goto :build_failed
)

if not exist "dist\NekroAgent\_internal\libssl-3-x64.dll" (
    echo 错误: 打包结果缺少 libssl-3-x64.dll，HTTPS 下载将不可用！
    goto :build_failed
)
if not exist "dist\NekroAgent\_internal\libcrypto-3-x64.dll" (
    echo 错误: 打包结果缺少 libcrypto-3-x64.dll，HTTPS 下载将不可用！
    goto :build_failed
)

echo.
echo [5/6] 验证打包结果（含 SSL DLL 加载和 EXE 启动检查）...
if not exist "%~dp0scripts\package_smoke_test.ps1" (
    echo 错误: 缺少打包验证脚本 scripts\package_smoke_test.ps1。
    goto :build_failed
)
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\package_smoke_test.ps1" -BundleDir "%~dp0dist\NekroAgent"
rem 这一步不产出可校验的产物，必须自己兜住退出码；powershell 解析不了 -File
rem 时会返回负数（如 -196608），而 if errorlevel 1 是有符号 >= 比较会漏判。
if %errorlevel% neq 0 (
    echo 错误: 打包结果验证失败（退出码 %errorlevel%），已停止制作安装包。
    goto :build_failed
)

echo.
echo [6/6] 制作安装包...
echo 使用 Inno Setup: %ISCC%
"%ISCC%" installer.iss
if errorlevel 1 (
    echo 错误: 安装包编译失败！
    goto :build_failed
)
if not exist "installer\NekroAgent-Setup.exe" (
    echo 错误: Inno Setup 返回成功，但未生成 installer\NekroAgent-Setup.exe。
    goto :build_failed
)

echo.
echo ========================================
echo   全部完成！
echo ========================================
echo.
echo   版本: %APP_VERSION%
echo   EXE 目录:  dist\NekroAgent\
echo   安装包:    installer\NekroAgent-Setup.exe
echo.
popd
pause
endlocal & exit /b 0

:find_inno_compiler
set "ISCC="
if defined INNO_SETUP_COMPILER (
    if exist "%INNO_SETUP_COMPILER%" (
        set "ISCC=%INNO_SETUP_COMPILER%"
    ) else (
        echo 错误: INNO_SETUP_COMPILER 指向的文件不存在:
        echo   %INNO_SETUP_COMPILER%
        exit /b 1
    )
)
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if not defined ISCC (
    echo 错误: 未检测到 Inno Setup 6 稳定版编译器。
    echo 请安装稳定版 Inno Setup 6:
    echo   https://jrsoftware.org/isinfo.php
    echo 如果编译器安装在自定义目录，可设置环境变量 INNO_SETUP_COMPILER 指向 ISCC.exe。
    echo 不会自动使用 Inno Setup 7 Preview，以免生成不适合发布的安装包。
    exit /b 1
)

rem 默认只接受 Inno Setup 6；显式指定 INNO_SETUP_ALLOW_UNSTABLE=1 才允许其他版本。
"%ISCC%" /? 2>&1 | findstr.exe /b /c:"Inno Setup 6 " >nul
if errorlevel 1 if /i not "%INNO_SETUP_ALLOW_UNSTABLE%"=="1" (
    echo 错误: 检测到的 Inno Setup 不是稳定版 6:
    echo   %ISCC%
    echo 如确实要使用其他版本，请设置 INNO_SETUP_ALLOW_UNSTABLE=1 后重试。
    exit /b 1
)
if /i "%INNO_SETUP_ALLOW_UNSTABLE%"=="1" echo [警告] 已通过 INNO_SETUP_ALLOW_UNSTABLE 使用非 Inno Setup 6 编译器。
echo 检测到 Inno Setup: %ISCC%
exit /b 0

:build_failed
echo.
echo ========================================
echo   打包失败，未生成可发布安装包。
echo ========================================
popd
pause
endlocal & exit /b 1
