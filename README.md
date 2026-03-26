# Nekro-Agent Windows Manager

基于 PyQt6 的 Windows 图形化部署工具，用于在本机通过独立运行时环境部署和管理 Nekro-Agent。

## 当前状态

- 当前默认后端为 `WSL`
- 项目已重构为后端抽象结构，便于后续接入 `Hyper-V`
- `Hyper-V` 后端目前已具备环境检测、基础镜像下载、交换机/NAT/VM 创建骨架
- `Hyper-V` 的 cloud-init、SSH 初始化、Docker 安装和 Compose 部署仍待补齐

## 功能

- 图形化环境检测、初始化、部署、更新、卸载
- 首次运行向导
- Nekro-Agent / NapCat / 应用日志分离展示
- 内置 WebView 访问 Nekro-Agent / NapCat 管理界面，并保留系统浏览器兜底打开
- 运行环境内文件路径快捷打开

## 目录结构

```text
na_for_windows/
├── core/
│   ├── backend_base.py
│   ├── backend_factory.py
│   ├── config_manager.py
│   ├── hyperv_manager.py
│   ├── hyperv_backend.py
│   ├── mirror_config.py
│   ├── powershell.py
│   ├── runtime_image_fetcher.py
│   ├── ssh_transport.py
│   └── wsl_manager.py
├── ui/
│   ├── first_run_dialog.py
│   ├── main_window.py
│   ├── styles.py
│   └── widgets.py
├── assets/
├── docker-compose_with_napcat.yml
├── docker-compose_withnot_napcat.yml
├── env
├── main.py
└── build.spec
```

## 运行

```bash
pip install -r requirements.txt
python main.py
```

调试模式：

```bash
python main.py --debug
```

如果内置 WebView 在个别 Windows 机器上出现闪烁或渲染异常，可临时关闭 GPU 加速：

```bash
python main.py --disable-webview-gpu
```

## 配置

运行时配置写入 `config.json`。核心字段如下：

```json
{
  "backend": "wsl",
  "autostart": false,
  "first_run": true,
  "deploy_mode": "napcat",
  "wsl_distro": "NekroAgent",
  "wsl_install_dir": "D:/NekroAgent/wsl",
  "data_dir": "/root/nekro_agent_data"
}
```

将 `backend` 改为 `hyperv` 后，重启应用即可切换到 Hyper-V 后端骨架流程。

## 打包

```bash
build.bat
```

## 后续计划

- 为 `Hyper-V` 后端补齐 cloud-init、SSH 初始化、Docker 安装与 Compose 部署
- 将自动启动从仅配置项补成真实系统集成
- 继续拆分 `WSLManager` 中的命令执行、Compose 部署和日志解析逻辑
