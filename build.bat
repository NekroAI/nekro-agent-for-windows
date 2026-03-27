@echo off
chcp 65001 >nul
echo ========================================
echo   Nekro-Agent Windows 打包工具
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
echo [4/5] 打包为单文件 EXE...
python -m PyInstaller build.spec
if errorlevel 1 (
    echo 错误: 打包失败！
    pause
    exit /b 1
)

echo.
echo [5/5] 制作安装包...
echo.
echo 请确保已安装 Inno Setup，然后：
echo 1. 打开 Inno Setup Compiler
echo 2. 打开文件: installer.iss
echo 3. 点击 Build -^> Compile
echo.
echo 或者在命令行运行:
echo & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
echo.

echo ========================================
echo   打包完成！
echo ========================================
echo.
echo 输出目录: dist\NekroAgent\
echo 安装包将生成在: installer\NekroAgent-Setup.exe
echo.
pause
