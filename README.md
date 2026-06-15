# Nekro Agent启动器

Nekro Agent启动器是基于 PyQt6 的 Windows 桌面启动器，用于在 Windows 上通过 WSL2 和 Docker Compose 安装、启动、更新和维护 [Nekro Agent](https://github.com/KroMiose/nekro-agent)。

当前文档对应版本：`v1.3.0`

## 下载

前往 [Releases](https://github.com/NekroAI/nekro-agent-for-windows/releases) 下载最新版 `NekroAgent-Setup.exe`。

## 系统要求

- Windows 10 / 11 64 位
- WSL2
- 可访问 Docker Hub 与 Ubuntu WSL rootfs 下载源
- 首次部署和更新时需要稳定的网络连接

如果本机尚未准备好 WSL2，首次运行向导会引导完成环境检查、发行版导入、Docker 安装与服务部署。

## 功能概览

- **首次运行向导**：从环境检测、运行环境创建到服务部署一站式完成。
- **多实例管理**：支持创建、切换、启动、停止和删除多个 Nekro Agent 实例。
- **两种部署模式**：支持精简版部署和 NapCat 完整版部署。
- **服务总览控制台**：集中查看运行状态、部署模式、实例信息、数据目录和最近日志。
- **镜像管理**：检查 Nekro Agent 相关镜像的本地与远程状态，并可独立拉取更新。
- **内置浏览器**：直接访问 Nekro Agent / NapCat，支持标签页、凭据填充与登录态持久化。
- **更新与预览版切换**：支持常规更新，也支持在高级功能中切换预览版并从备份恢复正式版。
- **日志中心**：区分应用日志、Nekro Agent 日志、NapCat 日志与更新过程输出。
- **存储与设置**：支持端口配置、端口冲突检测、打开数据目录和部署目录。

## v1.3.0 亮点

- 完善多实例运行状态同步，实例切换后端口、部署信息和发布频道保持一致。
- 支持实例级 stable / preview 发布频道，避免不同实例之间镜像频道串用。
- 新增 MIT License，明确项目开源许可。
- 依赖管理切换到 uv，并提交 `uv.lock` 以保证开发和打包环境可复现。
- 新增无 GUI 的后端环境检查命令：`python main.py --backend-check`。
- 配置文件保存改为原子写入，降低异常退出导致 `config.json` 损坏的风险。
- 首次运行和迁移向导的后台线程增加异常兜底，避免后端异常后界面无限等待。
- 新增人工测试清单，覆盖多实例、混合频道、预览版切换和失败回滚等关键路径。

## 使用说明

### 首次部署

1. 启动程序后进入首次运行向导。
2. 完成 WSL2、NekroAgent 发行版、Docker 和 Docker Compose 检测。
3. 选择部署模式、实例名称和端口。
4. 等待 Docker 镜像拉取和 Compose 服务启动。
5. 部署完成后在总览页查看状态，并通过内置浏览器进入管理界面。

### 多实例

- 每个实例拥有独立的部署目录、数据目录、端口和发布频道。
- 可在总览页切换当前实例；切换后总览、日志、文件路径和内置浏览器会指向所选实例。
- 默认实例用于程序启动后的默认展示和访问目标。

### 预览版切换

- 默认关闭高级功能。
- 在设置页启用高级功能后，总览页会显示“切换至预览版”入口。
- 切换预览版时可选择先备份当前数据与部署目录。
- 如需恢复正式版，需要依赖切换前生成的备份归档。
- 预览版备份状态按实例保存，不会影响其他实例。

### 数据目录

- 默认数据目录为 `/root/nekro_agent_data`。
- 命名实例会使用独立的数据目录和实例名前缀。
- 总览页和存储页面展示的是对应的 Windows 侧映射路径。

## 开发

本项目使用 uv 管理依赖。

```bash
uv sync
uv run python main.py
```

调试模式：

```bash
uv run python main.py --debug
```

后端环境检查：

```bash
uv run python main.py --backend-check
```

基础源码检查：

```bash
uv run python -m compileall main.py core ui
```

安装开发依赖：

```bash
uv sync --group dev
```

## 打包

Windows 下运行：

```bat
build.bat
```

打包脚本会执行以下步骤：

1. 检查 uv 是否可用。
2. 使用 `uv sync --group dev` 同步运行和打包依赖。
3. 清理旧的 `build/`、`dist/`、`installer/` 目录。
4. 使用 PyInstaller 生成 `dist/NekroAgent/`。
5. 如检测到 Inno Setup 6，则生成 `installer/NekroAgent-Setup.exe`。

## 项目结构

```text
main.py                 应用入口与命令行参数
core/                   配置、更新、后端抽象与 WSL 实现
core/wsl/               WSL、Docker、Compose、部署、镜像和迁移逻辑
ui/                     PyQt6 主窗口、向导、页面和通用组件
launcher_data/          Compose 模板、环境模板和更新配置
data/                   本地运行时数据（开发调试时生成，不参与打包）
assets/                 图标和图片资源
build.spec              PyInstaller 打包配置
installer.iss           Inno Setup 安装包脚本
MANUAL_TEST_CHECKLIST.md 人工测试清单
```

## License

本项目基于 MIT License 开源，详见 [LICENSE](LICENSE)。

## 问题反馈

如遇部署、更新或 WebView 相关问题，请前往 [Issues](https://github.com/NekroAI/nekro-agent-for-windows/issues/new) 提交反馈。
