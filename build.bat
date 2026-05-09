@echo off
chcp 65001 >nul
echo ========================================
echo   Nekro-Agent Windows 打包工具
echo ========================================

set /p APP_VERSION=<version.txt
echo   版本号: %APP_VERSION%
echo ========================================
echo.

echo [1/5] 检查并安装依赖...
pip install -r requirements.txt
if errorlevel 1 (
    echo 错误: 依赖安装失败！
    pause
    exit /b 1
)

echo.
echo [2/5] 检查打包工具...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller 未安装，正在安装...
    pip install pyinstaller
    if errorlevel 1 (
        echo 错误: PyInstaller 安装失败！
        pause
        exit /b 1
    )
)

echo.
echo [3/5] 清理旧的构建文件...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist installer rmdir /s /q installer
echo 清理完成

echo.
echo [4/5] 打包为 EXE...
python -m PyInstaller build.spec
if errorlevel 1 (
    echo 错误: 打包失败！
    pause
    exit /b 1
)

echo.
echo [5/5] 制作安装包...

set "ISCC="
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
)
if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
)

if defined ISCC (
    echo 检测到 Inno Setup: %ISCC%
    "%ISCC%" installer.iss
    if errorlevel 1 (
        echo 错误: 安装包编译失败！
        pause
        exit /b 1
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
) else (
    echo [警告] 未检测到 Inno Setup 6，跳过安装包制作。
    echo.
    echo 请安装 Inno Setup 6 后手动编译:
    echo   https://jrsoftware.org/isinfo.php
    echo.
    echo 或在命令行运行:
    echo   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
    echo.
    echo ========================================
    echo   打包完成（未生成安装包）
    echo ========================================
    echo.
    echo   版本: %APP_VERSION%
    echo   EXE 目录: dist\NekroAgent\
    echo.
)

pause
