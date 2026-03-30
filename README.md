# Nekro-Agent for Windows

基于 PyQt6 的 Windows 图形化部署工具，用于在 Windows 上安装、启动、更新和维护 [Nekro Agent](https://github.com/KroMiose/nekro-agent)。

当前文档对应版本：`v1.1.0`

## 下载

前往 [Releases](https://github.com/NekroAI/nekro-agent-for-windows/releases) 下载最新版 `NekroAgent-Setup.exe`。

## 系统要求

- Windows 10 / 11 64 位
- WSL2
- 可访问 Docker Hub 与 Ubuntu WSL rootfs 下载源

如果本机尚未准备好 WSL2，首次运行向导会引导完成环境检查、发行版导入、Docker 安装与服务部署。

## 功能概览

- **首次运行向导**：从环境检测到服务部署一站式完成，安装和镜像拉取过程带实时进度展示
- **两种部署模式**：支持精简版与 NapCat 完整版部署
- **服务总览控制台**：集中查看运行状态、部署模式、数据目录与最近活动日志
- **镜像管理**：检查 Nekro Agent 相关镜像的本地与远程状态，并独立执行更新
- **内置浏览器**：直接访问 Nekro Agent / NapCat，支持页面跳转、标签页、凭据填充与登录态持久化
- **更新与预览版切换**：支持常规更新，也支持在高级功能中切换预览版并恢复正式版
- **日志中心**：区分应用日志、Nekro Agent 日志、NapCat 日志与更新过程输出
- **设置页**：支持保存端口配置、检测端口冲突、打开数据目录和部署目录

## v1.1.0 亮点

- 新增高级功能入口，可在总览控制台切换到预览版，或从备份恢复正式版
- 预览版切换支持可选备份；跳过备份会明确提示无法回退到正式版
- 内置浏览器支持凭据自动填充，并在重启应用后保留站点登录状态
- 镜像管理页聚焦 Nekro Agent 相关镜像，不再混入无关镜像项
- 设置页和首次安装流程都加入端口冲突检测，避免部署后才发现端口被占用
- 总览页直接显示 Windows 侧数据目录映射路径，并可一键打开

## 使用说明

### 首次部署

1. 启动程序后进入首次运行向导
2. 选择部署模式与端口
3. 等待 WSL、Docker 和镜像准备完成
4. 部署完成后在总览页查看状态，并通过内置浏览器进入管理界面

### 预览版切换

- 默认关闭高级功能
- 在设置页启用高级功能后，总览控制台会显示“切换至预览版”入口
- 切换预览版时可选择先备份当前数据与部署目录
- 如需恢复正式版，需依赖切换前生成的预览版备份归档

### 数据目录

- 运行数据目录固定为 `/root/nekro_agent_data`
- 总览页展示的是对应的 Windows 映射路径
- 点击数据目录卡片或设置页按钮即可直接打开宿主机侧目录

## 开发运行

```bash
pip install -r requirements.txt
python main.py
```

调试模式：

```bash
python main.py --debug
```

如果内置 WebView 出现闪烁或渲染异常，可尝试禁用 GPU：

```bash
python main.py --disable-webview-gpu
```

## 问题反馈

如遇部署、更新或 WebView 相关问题，请前往 [Issues](https://github.com/NekroAI/nekro-agent-for-windows/issues/new) 提交反馈。
