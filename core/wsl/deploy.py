import json
import os
import shlex
import subprocess
import threading
import time

from core.wsl.constants import DISTRO_NAME, STABLE_IMAGE


class WSLDeployMixin:
    def _get_active_deploy_paths(self):
        """从当前活跃实例配置中获取部署路径，兼容无多实例配置的情况。"""
        if self.config:
            inst = self.config.get_instance()
            if inst:
                return (
                    inst.get("deploy_dir", "/root/nekro_agent"),
                    inst.get("data_dir", "/root/nekro_agent_data"),
                    inst.get("instance_name", ""),
                )
        return "/root/nekro_agent", "/root/nekro_agent_data", ""

    def _save_deploy_info(self, info, inst_id=None):
        if not self.config:
            return
        self.config.set("deploy_info", info)
        target_id = inst_id or self.config.get_active_instance_id()
        if target_id:
            self.config.update_instance(target_id, deploy_info=info)

    def start_services(self, deploy_mode, force_new_instance=False):
        """部署 Docker Compose 服务。

        force_new_instance=True 时跳过 is_running 检查，用于部署新的共存实例。
        旧实例的 compose 服务保持运行，仅切换日志读取到新实例。
        """
        if self._deploying:
            return True
        if self.is_running and not force_new_instance:
            return True

        if force_new_instance:
            self._stop_event.set()
            if self._log_process and self._log_process.poll() is None:
                try:
                    self._log_process.terminate()
                except Exception:
                    pass
                self._log_process = None
            self._stop_event.clear()

        self._deploying = True
        distro = DISTRO_NAME
        if not self._distro_exists():
            self.log_received.emit("NekroAgent 发行版不存在", "error")
            self._deploying = False
            return False

        self._stop_event.clear()
        self.status_changed.emit("启动中...")

        compose_file = "docker-compose_with_napcat.yml" if deploy_mode == "napcat" else "docker-compose_withnot_napcat.yml"
        compose_src = os.path.join(self.base_path, "data", compose_file)
        env_src = os.path.join(self.base_path, "data", "env")

        if not os.path.exists(compose_src):
            self.log_received.emit(f"Compose 文件不存在: {compose_src}", "error")
            self._deploying = False
            return False

        inst_id = ""
        if self.config:
            inst_id = self.config.get_active_instance_id() or ""
        inst_display = inst_id if inst_id and inst_id != "default" else ""
        log_prefix = f"[{inst_display}] " if inst_display else ""

        def _log(msg, level="info"):
            self.log_received.emit(f"{log_prefix}{msg}", level)

        def _deploy():
            try:
                deploy_dir, data_dir, _ = self._get_active_deploy_paths()
                compose_dest = f"{deploy_dir}/docker-compose.yml"
                env_dest = f"{deploy_dir}/.env"

                self._wsl_exec(distro, f"mkdir -p {shlex.quote(deploy_dir)}")
                self._wsl_exec(distro, f"mkdir -p {shlex.quote(data_dir)}")

                env_exists = self._wsl_exec(distro, f"test -f {shlex.quote(env_dest)} && echo yes").strip()
                compose_exists = self._wsl_exec(distro, f"test -f {shlex.quote(compose_dest)} && echo yes").strip()
                existing_env_content = ""
                existing_compose_content = ""
                if env_exists == "yes":
                    existing_env_content = self._wsl_exec(distro, f"cat {shlex.quote(env_dest)}")
                if compose_exists == "yes":
                    existing_compose_content = self._wsl_exec(distro, f"cat {shlex.quote(compose_dest)}")

                if env_exists == "yes":
                    _log("检测到已有部署配置，将保留旧凭据并按当前设置重写部署文件")
                else:
                    _log("首次部署，写入配置文件")

                compose_content = self._prepare_compose_content(compose_src)
                env_content = self._prepare_env(env_src, data_dir, existing_env_content)
                reuse_existing_runtime = (
                    env_exists == "yes"
                    and compose_exists == "yes"
                    and existing_env_content.strip() == env_content.strip()
                    and existing_compose_content.strip() == compose_content.strip()
                )

                if reuse_existing_runtime:
                    _log("检测到部署配置未变化，本次启动将复用现有容器，不强制重建")
                elif env_exists == "yes":
                    _log("检测到部署配置发生变化，将重建 Compose 服务以应用新配置")

                self._write_to_wsl(distro, compose_content, compose_dest)
                self._write_to_wsl(distro, env_content, env_dest)

                ls_output = self._wsl_exec(distro, f"ls -la {shlex.quote(deploy_dir)}")
                _log(f"部署目录内容:\n{ls_output}", "debug")
                _log("配置文件已部署到 WSL")

                _log("确保 Docker 服务启动...")
                self._wsl_exec(distro, "systemctl start docker", timeout=30)

                docker_version = self._wsl_exec(distro, "docker version")
                _log(f"Docker 版本:\n{docker_version}", "debug")

                compose_version = self._wsl_exec(distro, "docker compose version")
                _log(f"Docker Compose 版本: {compose_version}", "debug")

                missing = self._get_missing_images(distro, deploy_mode)
                if missing:
                    _log(f"检测到 {len(missing)} 个镜像需要拉取...")
                    self._emit_pull_progress("start", f"准备拉取 {len(missing)} 个镜像")
                    if not self._pull_images(distro, missing):
                        self.status_changed.emit("启动失败")
                        return
                else:
                    _log("所有镜像已就绪")

                _log("启动 Docker Compose 服务...")
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
                        f"cd {shlex.quote(deploy_dir)} && {compose_cmd}",
                    ],
                    capture_output=True,
                    timeout=120,
                    creationflags=self._creation_flags(),
                )

                if proc.returncode != 0:
                    _log(f"返回码: {proc.returncode}", "error")
                    _log(f"部署目录: {deploy_dir}", "error")
                    _log(f"STDOUT:\n{self._clean_stderr(proc.stdout, 0)}", "error")
                    _log(f"STDERR:\n{self._clean_stderr(proc.stderr, 0)}", "error")
                    _log("Compose 启动失败，详见上方日志", "error")
                    self.status_changed.emit("启动失败")
                    return

                self.is_running = True
                _log("Compose 服务已启动，等待就绪...")

                is_first_deploy = env_exists != "yes"
                deploy_info = self._parse_deploy_info(env_content, deploy_mode)

                inst_id_cfg = inst_id
                if self.config:
                    _, _, inst_name = self._get_active_deploy_paths()
                    inst_id_cfg = self.config.get_active_instance_id()
                    if not inst_id_cfg:
                        inst_id_cfg = self.config.next_instance_id()
                    if is_first_deploy:
                        self.config.set_instance(inst_id_cfg, {
                            "instance_name": inst_name,
                            "deploy_dir": deploy_dir,
                            "data_dir": data_dir,
                            "deploy_mode": deploy_mode,
                            "nekro_port": self.config.get("nekro_port") or 8021,
                            "napcat_port": self.config.get("napcat_port") or 6099,
                            "release_channel": self.config.get("release_channel") or "stable",
                            "deploy_info": deploy_info,
                        })
                        self.config.set("active_instance", inst_id_cfg)
                    else:
                        self.config.update_instance(inst_id_cfg, deploy_info=deploy_info)

                if is_first_deploy:
                    if deploy_mode == "napcat":
                        self._pending_deploy_info = deploy_info
                    else:
                        self._show_deploy_info(deploy_info, inst_id=inst_id_cfg)
                else:
                    self._refresh_deploy_info(deploy_info, inst_id=inst_id_cfg)

                nekro_port = int(deploy_info.get("port") or 8021)
                threading.Thread(target=self._log_reader, args=(distro, deploy_dir, log_prefix, inst_id_cfg), daemon=True).start()
                threading.Thread(target=self._health_check, args=(nekro_port,), daemon=True).start()
            except Exception as e:
                _log(f"部署失败: {e}", "error")
                self.status_changed.emit("启动失败")
            finally:
                self._deploying = False

        threading.Thread(target=_deploy, daemon=True).start()
        return True

    def stop_services(self):
        """停止 Docker Compose 服务"""
        self._stop_event.set()
        was_running = self.is_running

        if self._log_process and self._log_process.poll() is None:
            try:
                self._log_process.terminate()
            except Exception:
                pass
            self._log_process = None

        if not was_running:
            self.is_running = False
            self.status_changed.emit("已停止")
            return

        inst_id = ""
        if self.config:
            inst_id = self.config.get_active_instance_id() or ""
        inst_display = inst_id if inst_id and inst_id != "default" else ""
        log_prefix = f"[{inst_display}] " if inst_display else ""

        distro = DISTRO_NAME
        self.log_received.emit(f"{log_prefix}正在停止服务...", "info")
        self.status_changed.emit("停止中...")

        def _do_stop():
            deploy_dir, _, _ = self._get_active_deploy_paths()

            def _restore_runtime_state():
                self.is_running = True
                self._stop_event.clear()
                if self._log_process is None or self._log_process.poll() is not None:
                    threading.Thread(target=self._log_reader, args=(distro, deploy_dir, log_prefix, inst_id), daemon=True).start()

            try:
                compose_check = f"test -f {shlex.quote(deploy_dir)}/docker-compose.yml && echo yes"
                has_compose = False
                try:
                    check_result = subprocess.run(
                        ["wsl", "-d", distro, "--", "bash", "-c", compose_check],
                        capture_output=True, timeout=10,
                        creationflags=self._creation_flags(),
                    )
                    has_compose = "yes" in (check_result.stdout.decode(errors="replace") if isinstance(check_result.stdout, bytes) else check_result.stdout)
                except Exception:
                    pass

                if has_compose:
                    stop_proc = subprocess.run(
                        ["wsl", "-d", distro, "--", "bash", "-c", f"cd {shlex.quote(deploy_dir)} && docker compose -f docker-compose.yml stop"],
                        capture_output=True,
                        timeout=60,
                        creationflags=self._creation_flags(),
                    )
                    if stop_proc.returncode != 0:
                        stderr_text = self._clean_stderr(stop_proc.stderr, 0)
                        stdout_text = self._clean_command_output(stop_proc.stdout, 0)
                        detail = stderr_text or stdout_text or f"返回码: {stop_proc.returncode}"
                        self.log_received.emit(f"{log_prefix}停止 Compose 服务失败: {detail}", "error")
                        _restore_runtime_state()
                        self.status_changed.emit("停止失败")
                        return
                else:
                    self.log_received.emit(f"{log_prefix}当前实例无 Compose 部署文件，跳过 docker compose stop", "warn")

                self.is_running = False
                self.log_received.emit(f"{log_prefix}服务已停止", "info")

                self.status_changed.emit("已停止")
            except subprocess.TimeoutExpired:
                self.log_received.emit(f"{log_prefix}停止服务超时", "warn")
                _restore_runtime_state()
                self.status_changed.emit("停止失败")
            except Exception as e:
                self.log_received.emit(f"{log_prefix}停止服务异常: {e}", "error")
                _restore_runtime_state()
                self.status_changed.emit("停止失败")

        threading.Thread(target=_do_stop, daemon=True).start()

    def stop_all_services(self):
        """退出启动器时停止所有已登记实例的 Compose 服务。"""
        self._stop_event.set()
        if self._log_process and self._log_process.poll() is None:
            try:
                self._log_process.terminate()
            except Exception:
                pass
            self._log_process = None

        distro = DISTRO_NAME
        instances = self.config.list_instances() if self.config else []
        if not instances:
            self.stop_services()
            return

        self.status_changed.emit("停止中...")
        self.log_received.emit("正在停止所有实例服务...", "info")

        def _do_stop_all():
            failed = []
            try:
                for inst_id, inst in instances:
                    deploy_dir = inst.get("deploy_dir", "/root/nekro_agent")
                    name = inst.get("instance_name", "").rstrip("_") or inst_id
                    prefix = f"[{name}] " if name and name != "default" else ""
                    compose_check = f"test -f {shlex.quote(deploy_dir)}/docker-compose.yml && echo yes"
                    has_compose = False
                    try:
                        check_result = subprocess.run(
                            ["wsl", "-d", distro, "--", "bash", "-c", compose_check],
                            capture_output=True,
                            timeout=10,
                            creationflags=self._creation_flags(),
                        )
                        stdout = check_result.stdout.decode(errors="replace") if isinstance(check_result.stdout, bytes) else check_result.stdout
                        has_compose = "yes" in stdout
                    except Exception:
                        pass

                    if not has_compose:
                        self.log_received.emit(f"{prefix}无 Compose 部署文件，跳过", "warn")
                        continue

                    self.log_received.emit(f"{prefix}正在停止 Compose 服务...", "info")
                    stop_proc = subprocess.run(
                        ["wsl", "-d", distro, "--", "bash", "-c", f"cd {shlex.quote(deploy_dir)} && docker compose -f docker-compose.yml stop"],
                        capture_output=True,
                        timeout=60,
                        creationflags=self._creation_flags(),
                    )
                    if stop_proc.returncode != 0:
                        stderr_text = self._clean_stderr(stop_proc.stderr, 0)
                        stdout_text = self._clean_command_output(stop_proc.stdout, 0)
                        failed.append(f"{name}: {stderr_text or stdout_text or stop_proc.returncode}")
                    else:
                        self.log_received.emit(f"{prefix}服务已停止", "info")

                if failed:
                    self.is_running = True
                    self._stop_event.clear()
                    self.log_received.emit("停止部分实例失败: " + "；".join(failed), "error")
                    self.status_changed.emit("停止失败")
                    return

                self.is_running = False
                self.log_received.emit("所有实例服务已停止", "info")
                self.status_changed.emit("已停止")
            except subprocess.TimeoutExpired:
                self.is_running = True
                self._stop_event.clear()
                self.log_received.emit("停止所有实例超时", "error")
                self.status_changed.emit("停止失败")
            except Exception as e:
                self.is_running = True
                self._stop_event.clear()
                self.log_received.emit(f"停止所有实例异常: {e}", "error")
                self.status_changed.emit("停止失败")

        threading.Thread(target=_do_stop_all, daemon=True).start()

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
                deploy_dir, _, _ = self._get_active_deploy_paths()

                self.log_received.emit("[卸载] 1/3 停止并删除容器...", "info")
                self._wsl_exec(
                    distro,
                    f"cd {shlex.quote(deploy_dir)} && docker compose -f docker-compose.yml down -v 2>/dev/null; "
                    "docker system prune -af 2>/dev/null",
                    timeout=120,
                )
                self.log_received.emit("[卸载] ✓ 容器已清除", "info")

                self.log_received.emit("[卸载] 2/3 清理部署文件...", "info")
                self._wsl_exec(distro, f"rm -rf {shlex.quote(deploy_dir)}")
                self.log_received.emit("[卸载] ✓ 部署文件已清理", "info")

                self.log_received.emit("[卸载] 3/3 删除 WSL 发行版...", "info")
                if not self.remove_distro():
                    self.status_changed.emit("卸载失败")
                    return
                self.log_received.emit("[卸载] ✓ 环境卸载完成", "info")

                if self.config:
                    self.config.set("first_run", True)
                    self.config.set("deploy_mode", "")
                    self.config.set("wsl_distro", "")
                    self.config.set("wsl_install_dir", "")
                    self.config.set("release_channel", "stable")
                    self.config.set("preview_backup_available", False)
                    self.config.set("image_status_cache", {})
                    self.config.set("image_update_last_alert_signature", "")
                    self.config.set("last_image_update_check_ts", 0)
                    self.config.set("deploy_info", None)
                    self.config.set("instances", {})
                    self.config.set("active_instance", "")

                self.status_changed.emit("已卸载")
            except Exception as e:
                self.log_received.emit(f"卸载异常: {e}", "error")
                self.status_changed.emit("卸载失败")

        threading.Thread(target=_do_uninstall, daemon=True).start()

    def remove_single_instance(self, inst_id, inst_data, was_active=False):
        """移除单个实例：停止其 compose 服务、删除 deploy_dir。保留数据目录。
        完成后通过 instance_removed 信号通知 UI。"""
        distro = DISTRO_NAME
        deploy_dir = inst_data.get("deploy_dir", "")
        name = inst_data.get("instance_name", "").rstrip("_") or inst_id

        def _do_remove():
            success = True
            try:
                self.log_received.emit(f"[移除 {name}] 1/2 停止并删除容器...", "info")
                try:
                    compose_check = f"test -f {shlex.quote(deploy_dir)}/docker-compose.yml && echo yes"
                    check_result = subprocess.run(
                        ["wsl", "-d", distro, "--", "bash", "-c", compose_check],
                        capture_output=True, timeout=10,
                        creationflags=self._creation_flags(),
                    )
                    has_compose = "yes" in (check_result.stdout.decode(errors="replace") if isinstance(check_result.stdout, bytes) else check_result.stdout)
                except Exception:
                    has_compose = False

                if has_compose:
                    self._wsl_exec(
                        distro,
                        f"cd {shlex.quote(deploy_dir)} && docker compose -f docker-compose.yml down -v 2>/dev/null",
                        timeout=60,
                    )
                    self.log_received.emit(f"[移除 {name}] ✓ 容器已停止并清除", "info")
                else:
                    self.log_received.emit(f"[移除 {name}] 无 Compose 文件，跳过容器清理", "warn")

                self.log_received.emit(f"[移除 {name}] 2/2 清理部署目录...", "info")
                if deploy_dir:
                    self._wsl_exec(distro, f"rm -rf {shlex.quote(deploy_dir)}", timeout=30)
                self.log_received.emit(f"[移除 {name}] ✓ 实例已移除", "info")
            except Exception as e:
                self.log_received.emit(f"[移除 {name}] 异常: {e}", "error")
                success = False

            self.instance_removed.emit(success, inst_id, was_active)

        threading.Thread(target=_do_remove, daemon=True).start()

    def _show_deploy_info(self, info, inst_id=None):
        """保存凭据并发送信号给 UI 弹窗"""
        self._save_deploy_info(info, inst_id=inst_id)

        self.log_received.emit("=== 部署完成！===", "info")
        self.log_received.emit(f"管理员账号: admin | 密码: {info['admin_password']}", "info")
        self.log_received.emit(f"Web 访问地址: http://127.0.0.1:{info['port']}", "info")

        self.deploy_info_ready.emit(info)

    def _refresh_deploy_info(self, info, inst_id=None):
        """非首次启动时静默刷新凭据（不弹窗），防止上次中途退出丢失"""
        if self.config:
            old_info = self.config.get("deploy_info") or {}
            if old_info.get("napcat_token"):
                info["napcat_token"] = old_info["napcat_token"]
            self._save_deploy_info(info, inst_id=inst_id)

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
        _, _, active_instance_name = self._get_active_deploy_paths()

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
                new_lines.append(f"{key}={existing_value or active_instance_name}")
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
            _, data_dir, _ = self._get_active_deploy_paths()
            config_dir = f"{data_dir}/napcat_data/napcat"
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
                config_exists = self._wsl_exec_checked(
                    distro,
                    f'[ -d "{config_dir}" ] && printf yes || printf no',
                ).strip()
                if config_exists != "yes":
                    _emit("config_missing", "未找到 NapCat 配置目录，请先完成 NapCat 部署。")
                    return

                files_output = self._wsl_exec_checked(
                    distro,
                    f'find "{config_dir}" -maxdepth 1 -type f -name \'onebot11_*.json\' | sort',
                )
                onebot_paths = [line.strip() for line in self._clean_command_output(files_output).splitlines() if line.strip()]
                if not onebot_paths:
                    _emit("login_required", "尚未检测到 NapCat 账号配置，请先登录一次 QQ。")
                    return

                backup_dir = f"{config_dir}/_launcher_backup_{time.strftime('%Y%m%d-%H%M%S')}"
                self._wsl_exec_checked(distro, f'mkdir -p "{backup_dir}"')

                configured_accounts = []
                for path in onebot_paths:
                    content = self._wsl_exec_checked(distro, f'cat "{path}"')
                    if not content.strip():
                        _emit("config_read_failed", f"读取 NapCat 配置失败: {os.path.basename(path)}")
                        return

                    try:
                        data = json.loads(content)
                    except json.JSONDecodeError:
                        _emit("config_invalid", f"NapCat 配置文件格式异常: {os.path.basename(path)}")
                        return

                    self._wsl_exec_checked(distro, f'cp "{path}" "{backup_dir}/"')

                    network = data.setdefault("network", {})
                    existing_clients = network.get("websocketClients")
                    if not isinstance(existing_clients, list):
                        existing_clients = []

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

                    if not replaced:
                        updated_clients.append(desired_client)

                    network["websocketClients"] = updated_clients
                    self._write_to_wsl(distro, json.dumps(data, ensure_ascii=False, indent=2) + "\n", path)

                    filename = os.path.basename(path)
                    account = filename[len("onebot11_"):-len(".json")]
                    if account:
                        configured_accounts.append(account)

                deploy_dir, _, _ = self._get_active_deploy_paths()
                proc = subprocess.run(
                    [
                        "wsl",
                        "-d",
                        distro,
                        "--",
                        "bash",
                        "-c",
                        f'cd {shlex.quote(deploy_dir)} && docker compose -f docker-compose.yml restart nekro_napcat',
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
