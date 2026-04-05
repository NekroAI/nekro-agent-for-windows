import json
import os
import subprocess
import threading
import time

from core.wsl.constants import DISTRO_NAME, STABLE_IMAGE


class WSLDeployMixin:
    def start_services(self, deploy_mode):
        """部署 Docker Compose 服务"""
        if self.is_running:
            return True

        distro = DISTRO_NAME
        if not self._distro_exists():
            self.log_received.emit("NekroAgent 发行版不存在", "error")
            return False

        self._stop_event.clear()
        self.status_changed.emit("启动中...")

        compose_file = "docker-compose_with_napcat.yml" if deploy_mode == "napcat" else "docker-compose_withnot_napcat.yml"
        compose_src = os.path.join(self.base_path, "data", compose_file)
        env_src = os.path.join(self.base_path, "data", "env")

        if not os.path.exists(compose_src):
            self.log_received.emit(f"Compose 文件不存在: {compose_src}", "error")
            return False

        def _deploy():
            try:
                deploy_dir = "/root/nekro_agent"
                data_dir = "/root/nekro_agent_data"
                compose_dest = f"{deploy_dir}/docker-compose.yml"
                env_dest = f"{deploy_dir}/.env"

                self._wsl_exec(distro, f"mkdir -p {deploy_dir}")
                self._wsl_exec(distro, f"mkdir -p {data_dir}")

                env_exists = self._wsl_exec(distro, f"test -f {env_dest} && echo yes").strip()
                compose_exists = self._wsl_exec(distro, f"test -f {compose_dest} && echo yes").strip()
                existing_env_content = ""
                existing_compose_content = ""
                if env_exists == "yes":
                    existing_env_content = self._wsl_exec(distro, f"cat {env_dest}")
                if compose_exists == "yes":
                    existing_compose_content = self._wsl_exec(distro, f"cat {compose_dest}")

                if env_exists == "yes":
                    self.log_received.emit("检测到已有部署配置，将保留旧凭据并按当前设置重写部署文件", "info")
                else:
                    self.log_received.emit("首次部署，写入配置文件", "info")

                compose_content = self._prepare_compose_content(compose_src)
                env_content = self._prepare_env(env_src, data_dir, existing_env_content)
                reuse_existing_runtime = (
                    env_exists == "yes"
                    and compose_exists == "yes"
                    and existing_env_content.strip() == env_content.strip()
                    and existing_compose_content.strip() == compose_content.strip()
                )

                if reuse_existing_runtime:
                    self.log_received.emit("检测到部署配置未变化，本次启动将复用现有容器，不强制重建", "info")
                elif env_exists == "yes":
                    self.log_received.emit("检测到部署配置发生变化，将重建 Compose 服务以应用新配置", "info")

                self._write_to_wsl(distro, compose_content, compose_dest)
                self._write_to_wsl(distro, env_content, env_dest)

                ls_output = self._wsl_exec(distro, f"ls -la {deploy_dir}")
                self.log_received.emit(f"部署目录内容:\n{ls_output}", "debug")
                self.log_received.emit("配置文件已部署到 WSL", "info")

                self.log_received.emit("确保 Docker 服务启动...", "info")
                self._wsl_exec(distro, "systemctl start docker", timeout=30)

                docker_version = self._wsl_exec(distro, "docker version")
                self.log_received.emit(f"Docker 版本:\n{docker_version}", "debug")

                compose_version = self._wsl_exec(distro, "docker compose version")
                self.log_received.emit(f"Docker Compose 版本: {compose_version}", "debug")

                missing = self._get_missing_images(distro, deploy_mode)
                if missing:
                    self.log_received.emit(f"检测到 {len(missing)} 个镜像需要拉取...", "info")
                    self._emit_pull_progress("start", f"准备拉取 {len(missing)} 个镜像")
                    if not self._pull_images(distro, missing):
                        self.status_changed.emit("启动失败")
                        return
                else:
                    self.log_received.emit("所有镜像已就绪", "info")

                self.log_received.emit("启动 Docker Compose 服务...", "info")
                self.progress_updated.emit("启动 Compose 服务...")
                compose_cmd = "docker compose -f docker-compose.yml --env-file .env up -d --remove-orphans"
                if not reuse_existing_runtime:
                    compose_cmd = f"{compose_cmd} --force-recreate"
                proc = subprocess.run(
                    [
                        "wsl",
                        "-d",
                        distro,
                        "--",
                        "bash",
                        "-c",
                        f"cd {deploy_dir} && {compose_cmd}",
                    ],
                    capture_output=True,
                    timeout=120,
                    creationflags=self._creation_flags(),
                )

                if proc.returncode != 0:
                    self.log_received.emit(f"返回码: {proc.returncode}", "error")
                    self.log_received.emit(f"部署目录: {deploy_dir}", "error")
                    self.log_received.emit(f"STDOUT:\n{self._clean_stderr(proc.stdout, 0)}", "error")
                    self.log_received.emit(f"STDERR:\n{self._clean_stderr(proc.stderr, 0)}", "error")
                    self.log_received.emit("Compose 启动失败，详见上方日志", "error")
                    self.status_changed.emit("启动失败")
                    return

                self.is_running = True
                self.log_received.emit("Compose 服务已启动，等待就绪...", "info")

                is_first_deploy = env_exists != "yes"
                deploy_info = self._parse_deploy_info(env_content, deploy_mode)

                if is_first_deploy:
                    if deploy_mode == "napcat":
                        self._pending_deploy_info = deploy_info
                    else:
                        self._show_deploy_info(deploy_info)
                else:
                    self._refresh_deploy_info(deploy_info)

                threading.Thread(target=self._log_reader, args=(distro, deploy_dir), daemon=True).start()
                threading.Thread(target=self._health_check, daemon=True).start()
            except Exception as e:
                self.log_received.emit(f"部署失败: {e}", "error")
                self.status_changed.emit("启动失败")

        threading.Thread(target=_deploy, daemon=True).start()
        return True

    def stop_services(self):
        """停止 Docker Compose 服务"""
        self._stop_event.set()
        was_running = self.is_running
        self.is_running = False

        if self._log_process and self._log_process.poll() is None:
            try:
                self._log_process.terminate()
            except Exception:
                pass
            self._log_process = None

        if not was_running:
            self.status_changed.emit("已停止")
            return

        distro = DISTRO_NAME
        self.log_received.emit("正在停止服务...", "info")

        def _do_stop():
            try:
                wsl_home = self._wsl_exec(distro, "echo $HOME").strip()
                if not wsl_home:
                    wsl_home = "/root"
                deploy_dir = f"{wsl_home}/nekro_agent"

                subprocess.run(
                    ["wsl", "-d", distro, "--", "bash", "-c", f"cd {deploy_dir} && docker compose -f docker-compose.yml stop"],
                    capture_output=True,
                    timeout=60,
                    creationflags=self._creation_flags(),
                )
                self.log_received.emit("服务已停止", "info")

                self.log_received.emit(f"关闭 {distro} 发行版...", "info")
                subprocess.run(
                    ["wsl", "--terminate", distro],
                    capture_output=True,
                    timeout=30,
                    creationflags=self._creation_flags(),
                )
                self.log_received.emit(f"{distro} 已关闭", "info")
            except subprocess.TimeoutExpired:
                self.log_received.emit("停止服务超时", "warn")
            except Exception as e:
                self.log_received.emit(f"停止服务异常: {e}", "error")
            finally:
                self.status_changed.emit("已停止")

        threading.Thread(target=_do_stop, daemon=True).start()

    def uninstall_environment(self):
        """卸载：停止服务 → 删除容器/镜像 → 删除 WSL 发行版"""
        distro = DISTRO_NAME
        self.log_received.emit("开始卸载环境...", "info")
        self.status_changed.emit("卸载中...")

        self._stop_event.set()
        self.is_running = False
        if self._log_process and self._log_process.poll() is None:
            try:
                self._log_process.terminate()
            except Exception:
                pass
            self._log_process = None

        def _do_uninstall():
            try:
                wsl_home = self._wsl_exec(distro, "echo $HOME").strip()
                if not wsl_home:
                    wsl_home = "/root"
                deploy_dir = f"{wsl_home}/nekro_agent"

                self.log_received.emit("[卸载] 1/3 停止并删除容器...", "info")
                self._wsl_exec(
                    distro,
                    f"cd {deploy_dir} && docker compose -f docker-compose.yml down -v 2>/dev/null; "
                    "docker system prune -af 2>/dev/null",
                    timeout=120,
                )
                self.log_received.emit("[卸载] ✓ 容器已清除", "info")

                self.log_received.emit("[卸载] 2/3 清理部署文件...", "info")
                self._wsl_exec(distro, f"rm -rf {deploy_dir}")
                self.log_received.emit("[卸载] ✓ 部署文件已清理", "info")

                self.log_received.emit("[卸载] 3/3 删除 WSL 发行版...", "info")
                self.remove_distro()
                self.log_received.emit("[卸载] ✓ 环境卸载完成", "info")

                if self.config:
                    self.config.set("first_run", True)
                    self.config.set("deploy_mode", "")
                    self.config.set("wsl_distro", "")
                    self.config.set("wsl_install_dir", "")
                    self.config.set("deploy_info", None)

                self.status_changed.emit("已卸载")
            except Exception as e:
                self.log_received.emit(f"卸载异常: {e}", "error")
                self.status_changed.emit("卸载失败")

        threading.Thread(target=_do_uninstall, daemon=True).start()

    def _show_deploy_info(self, info):
        """保存凭据并发送信号给 UI 弹窗"""
        if self.config:
            self.config.set("deploy_info", info)

        self.log_received.emit("=== 部署完成！===", "info")
        self.log_received.emit(f"管理员账号: admin | 密码: {info['admin_password']}", "info")
        self.log_received.emit(f"Web 访问地址: http://127.0.0.1:{info['port']}", "info")

        self.deploy_info_ready.emit(info)

    def _refresh_deploy_info(self, info):
        """非首次启动时静默刷新凭据（不弹窗），防止上次中途退出丢失"""
        if self.config:
            old_info = self.config.get("deploy_info") or {}
            if old_info.get("napcat_token"):
                info["napcat_token"] = old_info["napcat_token"]
            self.config.set("deploy_info", info)

    def _parse_deploy_info(self, env_content, deploy_mode):
        """从 .env 内容中解析部署凭据信息"""
        env_vars = self._parse_env_values(env_content)

        info = {
            "port": env_vars.get("NEKRO_EXPOSE_PORT", "8021"),
            "admin_password": env_vars.get("NEKRO_ADMIN_PASSWORD", ""),
            "onebot_token": env_vars.get("ONEBOT_ACCESS_TOKEN", ""),
            "deploy_mode": deploy_mode,
        }
        if deploy_mode == "napcat":
            info["napcat_port"] = env_vars.get("NAPCAT_EXPOSE_PORT", "6099")

        return info

    def _parse_env_values(self, env_content):
        env_vars = {}
        for line in env_content.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env_vars[key.strip()] = value.strip()
        return env_vars

    def _prepare_compose_content(self, compose_template_path):
        content = ""
        if os.path.exists(compose_template_path):
            with open(compose_template_path, "r", encoding="utf-8") as f:
                content = f.read()

        agent_image = self.get_agent_image_ref(self.config)
        if agent_image != STABLE_IMAGE:
            content = content.replace(
                f"image: {STABLE_IMAGE}",
                f"image: {agent_image}",
            )
        return content

    def _prepare_env(self, env_template_path, data_dir, existing_env_content=""):
        """读取 env 模板文件，填充必要值，返回最终 .env 内容"""
        content = ""
        if os.path.exists(env_template_path):
            with open(env_template_path, "r", encoding="utf-8") as f:
                content = f.read()

        existing_env = self._parse_env_values(existing_env_content or "")
        lines = content.splitlines()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue

            key = stripped.split("=", 1)[0].strip()
            existing_value = existing_env.get(key, "").strip()

            if key == "NEKRO_DATA_DIR":
                new_lines.append(f"NEKRO_DATA_DIR={data_dir}")
            elif key == "NEKRO_EXPOSE_PORT":
                new_lines.append(f"NEKRO_EXPOSE_PORT={self.config.get('nekro_port') or 8021}")
            elif key == "NAPCAT_EXPOSE_PORT":
                new_lines.append(f"NAPCAT_EXPOSE_PORT={self.config.get('napcat_port') or 6099}")
            elif key == "QDRANT_API_KEY":
                new_lines.append(f"QDRANT_API_KEY={existing_value or self._random_token(32)}")
            elif key == "ONEBOT_ACCESS_TOKEN":
                new_lines.append(f"ONEBOT_ACCESS_TOKEN={existing_value or self._random_token(32)}")
            elif key == "NEKRO_ADMIN_PASSWORD":
                new_lines.append(f"NEKRO_ADMIN_PASSWORD={existing_value or self._random_token(16)}")
            elif key == "INSTANCE_NAME":
                new_lines.append(f"{key}={existing_value}")
            elif key in {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DATABASE"} and existing_value:
                new_lines.append(f"{key}={existing_value}")
            else:
                new_lines.append(line)

        return "\n".join(new_lines) + "\n"

    def configure_napcat_network(self, payload):
        """直接写入 NapCat 配置文件并重启 NapCat 容器。"""
        if not isinstance(payload, dict) or not payload.get("token"):
            self.napcat_network_config_finished.emit(
                {
                    "status": "missing_token",
                    "message": "未找到 OneBot 令牌，无法执行一键配网。",
                }
            )
            return

        if not self._distro_exists():
            self.napcat_network_config_finished.emit(
                {
                    "status": "runtime_missing",
                    "message": "NekroAgent 发行版不存在，请先完成环境部署。",
                }
            )
            return

        def _emit(status, message, **extra):
            self.napcat_network_config_finished.emit({"status": status, "message": message, **extra})

        def _configure():
            distro = DISTRO_NAME
            config_dir = "/root/nekro_agent_data/napcat_data/napcat"
            desired_client = {
                "enable": True,
                "name": payload.get("name") or "Nekro Agent",
                "url": payload.get("url") or "ws://nekro_agent:8021/onebot/v11/ws",
                "reportSelfMessage": False,
                "messagePostFormat": "array",
                "token": payload["token"],
                "debug": False,
                "heartInterval": 30000,
                "reconnectInterval": 30000,
            }

            try:
                config_exists = self._wsl_exec(distro, f'test -d "{config_dir}" && echo yes').strip()
                if config_exists != "yes":
                    _emit("config_missing", "未找到 NapCat 配置目录，请先完成 NapCat 部署。")
                    return

                files_output = self._wsl_exec(
                    distro,
                    f'find "{config_dir}" -maxdepth 1 -type f -name \'onebot11_*.json\' | sort',
                )
                onebot_paths = [line.strip() for line in self._clean_command_output(files_output).splitlines() if line.strip()]
                if not onebot_paths:
                    _emit("login_required", "尚未检测到 NapCat 账号配置，请先登录一次 QQ。")
                    return

                backup_dir = f"{config_dir}/_launcher_backup_{time.strftime('%Y%m%d-%H%M%S')}"
                self._wsl_exec(distro, f'mkdir -p "{backup_dir}"')

                configured_accounts = []
                for path in onebot_paths:
                    content = self._wsl_exec(distro, f'cat "{path}"')
                    if not content.strip():
                        _emit("config_read_failed", f"读取 NapCat 配置失败: {os.path.basename(path)}")
                        return

                    try:
                        data = json.loads(content)
                    except json.JSONDecodeError:
                        _emit("config_invalid", f"NapCat 配置文件格式异常: {os.path.basename(path)}")
                        return

                    self._wsl_exec(distro, f'cp "{path}" "{backup_dir}/"')

                    network = data.setdefault("network", {})
                    existing_clients = network.get("websocketClients")
                    if not isinstance(existing_clients, list):
                        existing_clients = []

                    fallback_index = None
                    updated_clients = []
                    replaced = False
                    for client in existing_clients:
                        if not isinstance(client, dict):
                            updated_clients.append(client)
                            continue

                        if client.get("name") == desired_client["name"] or client.get("url") == desired_client["url"]:
                            if not replaced:
                                merged = dict(client)
                                merged.update(desired_client)
                                updated_clients.append(merged)
                                replaced = True
                            continue

                        updated_clients.append(client)
                        if fallback_index is None and client.get("url") == "ws://localhost:8082":
                            fallback_index = len(updated_clients) - 1

                    if not replaced and fallback_index is not None:
                        merged = dict(updated_clients[fallback_index])
                        merged.update(desired_client)
                        updated_clients[fallback_index] = merged
                        replaced = True

                    if not replaced:
                        updated_clients.append(desired_client)

                    network["websocketClients"] = updated_clients
                    self._write_to_wsl(distro, json.dumps(data, ensure_ascii=False, indent=2) + "\n", path)

                    filename = os.path.basename(path)
                    account = filename[len("onebot11_"):-len(".json")]
                    if account:
                        configured_accounts.append(account)

                wsl_home = self._wsl_exec(distro, "echo $HOME").strip() or "/root"
                deploy_dir = f"{wsl_home}/nekro_agent"
                proc = subprocess.run(
                    [
                        "wsl",
                        "-d",
                        distro,
                        "--",
                        "bash",
                        "-c",
                        f'cd "{deploy_dir}" && docker compose -f docker-compose.yml restart nekro_napcat',
                    ],
                    capture_output=True,
                    timeout=60,
                    creationflags=self._creation_flags(),
                )

                if proc.returncode != 0:
                    stderr_text = self._clean_stderr(proc.stderr, 0)
                    self.log_received.emit("NapCat 配置文件已写入，但重启 NapCat 失败", "warn")
                    if stderr_text:
                        self.log_received.emit(stderr_text, "debug")
                    _emit(
                        "restart_failed",
                        "NapCat 配置已写入，但重启服务失败，请手动重启 NapCat 后再验证。",
                        backup_dir=backup_dir,
                    )
                    return

                self.log_received.emit("NapCat 配置文件已更新，并已重启 NapCat 服务", "info")
                message = "NapCat 配置已写入并重启生效。"
                _emit(
                    "saved",
                    message,
                    accounts=configured_accounts,
                    backup_dir=backup_dir,
                )
            except Exception as exc:
                _emit("error", f"NapCat 一键配网失败: {exc}")

        threading.Thread(target=_configure, daemon=True).start()
