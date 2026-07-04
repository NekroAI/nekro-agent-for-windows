import json
import os
import posixpath
import shlex
import subprocess
import threading
import time

from core.port_utils import normalize_port, validate_instance_port_conflicts
from core.wsl.constants import CC_SANDBOX_IMAGE, DISTRO_NAME, STABLE_IMAGE


class WSLDeployMixin:
    _DANGEROUS_WSL_DELETE_PATHS = {
        "",
        "/",
        "/root",
        "/home",
        "/opt",
        "/var",
        "/var/lib",
        "/var/lib/docker",
        "/var/lib/docker/volumes",
    }

    @staticmethod
    def _instance_release_channel(inst):
        return (inst or {}).get("release_channel", "stable") or "stable"

    @staticmethod
    def _normalize_wsl_abs_path(path):
        normalized = str(path or "").strip().replace("\\", "/")
        if not normalized.startswith("/"):
            return ""
        return posixpath.normpath(normalized)

    def _validate_managed_deploy_dir_for_delete(self, distro, deploy_dir, action):
        path = self._normalize_wsl_abs_path(deploy_dir)
        if not path:
            return False, f"{action}\n部署目录为空或不是 WSL 绝对路径: {deploy_dir!r}"
        if path in self._DANGEROUS_WSL_DELETE_PATHS:
            return False, f"{action}\n拒绝删除高风险路径: {path}"
        if not path.startswith("/root/"):
            return False, f"{action}\n拒绝删除非托管路径: {path}"
        if not posixpath.basename(path).endswith("nekro_agent"):
            return False, f"{action}\n部署目录名称不符合托管实例规则: {path}"

        compose_path = posixpath.join(path, "docker-compose.yml")
        marker_cmd = (
            f"test -d {shlex.quote(path)} "
            f"&& test -f {shlex.quote(compose_path)} "
            f"&& grep -qi 'nekro' {shlex.quote(compose_path)} "
            "&& echo yes"
        )
        marker = self._wsl_exec(distro, marker_cmd, timeout=15).strip()
        if marker != "yes":
            return False, (
                f"{action}\n"
                "部署目录缺少 Nekro Agent Compose 标记，已拒绝删除。\n"
                f"目录: {path}"
            )
        return True, path

    def _remove_managed_deploy_dir(self, distro, deploy_dir, action):
        ok, detail_or_path = self._validate_managed_deploy_dir_for_delete(
            distro,
            deploy_dir,
            action,
        )
        if not ok:
            raise RuntimeError(detail_or_path)
        self._run_wsl_checked(
            distro,
            f"rm -rf {shlex.quote(detail_or_path)}",
            action=action,
            timeout=30,
        )

    def _launcher_data_path(self, *parts):
        return os.path.join(self.base_path, "launcher_data", *parts)

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
        target_id = inst_id or self.config.get_active_instance_id()
        if target_id:
            self.config.update_instance_with_globals(
                target_id,
                instance_updates={"deploy_info": info},
                global_updates={"deploy_info": info},
            )
        else:
            self.config.set("deploy_info", info)

    def _wait_deploy_optional_reply(self, label, prompt, timeout=300):
        self._deploy_optional_reply = None
        self.deploy_optional_confirm.emit(label, prompt)
        deadline = time.time() + timeout
        while self._deploy_optional_reply is None and time.time() < deadline:
            time.sleep(0.1)
        return self._deploy_optional_reply

    def reply_deploy_optional(self, confirmed: bool):
        self._deploy_optional_reply = confirmed

    def _start_instance_sync(
        self, inst_id, inst, attach_logs=True, attach_health=True, emit_status=True
    ):
        distro = DISTRO_NAME
        deploy_mode = inst.get("deploy_mode") or "lite"
        deploy_dir = inst.get("deploy_dir", "/root/nekro_agent")
        data_dir = inst.get("data_dir", "/root/nekro_agent_data")
        inst_name = inst.get("instance_name", "")
        inst_display = inst_name.rstrip("_") or inst_id
        log_prefix = (
            f"[{inst_display}] " if inst_display and inst_display != "default" else ""
        )

        def _log(msg, level="info"):
            self.log_received.emit(f"{log_prefix}{msg}", level)

        compose_file = (
            "docker-compose_with_napcat.yml"
            if deploy_mode == "napcat"
            else "docker-compose_withnot_napcat.yml"
        )
        compose_src = self._launcher_data_path(compose_file)
        env_src = self._launcher_data_path("env")
        if not os.path.exists(compose_src):
            _log(
                "Compose 模板文件不存在\n"
                f"模板: {compose_src}\n"
                f"部署模式: {deploy_mode}\n"
                f"实例: {inst_display}",
                "error",
            )
            return False
        if not os.path.exists(env_src):
            _log(
                "环境变量模板文件不存在\n"
                f"模板: {env_src}\n"
                f"实例: {inst_display}",
                "error",
            )
            return False

        compose_dest = f"{deploy_dir}/docker-compose.yml"
        env_dest = f"{deploy_dir}/.env"

        self.progress_updated.emit("__deploy_progress__|config|准备部署目录和配置文件")
        try:
            self._run_wsl_checked(
                distro,
                f"mkdir -p {shlex.quote(deploy_dir)}",
                action="[部署] 创建部署目录失败",
                timeout=30,
            )
            self._run_wsl_checked(
                distro,
                f"mkdir -p {shlex.quote(data_dir)}",
                action="[部署] 创建数据目录失败",
                timeout=30,
            )
        except RuntimeError as e:
            _log(str(e), "error")
            if emit_status:
                self.status_changed.emit("启动失败")
            return False

        env_exists = self._wsl_exec(
            distro, f"test -f {shlex.quote(env_dest)} && echo yes"
        ).strip()
        compose_exists = self._wsl_exec(
            distro, f"test -f {shlex.quote(compose_dest)} && echo yes"
        ).strip()
        existing_env_content = (
            self._wsl_exec(distro, f"cat {shlex.quote(env_dest)}")
            if env_exists == "yes"
            else ""
        )
        existing_compose_content = (
            self._wsl_exec(distro, f"cat {shlex.quote(compose_dest)}")
            if compose_exists == "yes"
            else ""
        )

        if env_exists == "yes":
            _log("检测到已有部署配置，将保留旧凭据并按当前设置重写部署文件")
        else:
            _log("首次部署，写入配置文件")

        daemon_env = {}
        launcher_daemon = getattr(self, "launcher_daemon", None)
        if launcher_daemon is not None:
            try:
                binding = launcher_daemon.ensure_instance_binding(inst_id, inst)
                daemon_env = launcher_daemon.env_values_for_binding(binding)
            except Exception as e:
                _log(f"Windows 启动器 daemon 绑定失败，WebUI 在线更新将不可用: {e}", "warn")

        compose_content = self._prepare_compose_content(compose_src, inst=inst)
        env_content = self._prepare_env(
            env_src,
            data_dir,
            existing_env_content,
            nekro_port=inst.get("nekro_port") or 8021,
            napcat_port=inst.get("napcat_port") or 6099,
            instance_name=inst_name,
            daemon_env=daemon_env,
        )
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

        try:
            self._write_to_wsl(distro, compose_content, compose_dest)
            self._write_to_wsl(distro, env_content, env_dest)
        except RuntimeError as e:
            _log(f"[部署] 写入 Compose/.env 失败\n{e}", "error")
            if emit_status:
                self.status_changed.emit("启动失败")
            return False

        ls_output = self._wsl_exec(distro, f"ls -la {shlex.quote(deploy_dir)}")
        _log(f"部署目录内容:\n{ls_output}", "debug")
        _log("配置文件已部署到 WSL")

        self.progress_updated.emit("__deploy_progress__|docker|确保 Docker 服务启动")
        _log("确保 Docker 服务启动...")
        try:
            self._run_wsl_checked(
                distro,
                "systemctl start docker",
                action="[部署] 启动 Docker 服务失败",
                timeout=30,
            )

            docker_proc = self._run_wsl_checked(
                distro,
                "docker version",
                action="[部署] Docker 版本检查失败",
                timeout=30,
            )
            docker_version = self._clean_command_output(docker_proc.stdout)
            _log(f"Docker 版本:\n{docker_version}", "debug")

            compose_proc = self._run_wsl_checked(
                distro,
                "docker compose version",
                action="[部署] Docker Compose 版本检查失败",
                timeout=30,
            )
            compose_version = self._clean_command_output(compose_proc.stdout)
            _log(f"Docker Compose 版本: {compose_version}", "debug")
        except RuntimeError as e:
            _log(str(e), "error")
            if emit_status:
                self.status_changed.emit("启动失败")
            return False

        self.progress_updated.emit("__deploy_progress__|images|检查必需镜像")
        missing = self._get_missing_images(
            distro,
            deploy_mode,
            release_channel=self._instance_release_channel(inst),
        )
        if missing:
            _log(f"检测到 {len(missing)} 个镜像需要拉取...")
            self._emit_pull_progress("start", f"准备拉取 {len(missing)} 个镜像")
            if not self._pull_images(distro, missing):
                if emit_status:
                    self.status_changed.emit("启动失败")
                return False
        else:
            _log("所有镜像已就绪")

        if is_first_deploy := (env_exists != "yes"):
            doc_url = "https://doc.nekro.ai/docs/03_workspace/claude_code_sandbox.html"
            prompt = (
                "Claude Code 沙盒属于工作区进阶功能。<br><br>"
                "如需在工作区中使用 Claude Code 沙盒，请先阅读文档确认：<br>"
                f"<a href=\"{doc_url}\">打开 Claude Code 沙盒文档</a><br><br>"
                "是否现在下载 Claude Code 沙盒镜像？"
            )
            self.progress_updated.emit(
                "__deploy_progress__|optional|确认是否下载 Claude Code 沙盒"
            )
            reply = self._wait_deploy_optional_reply(
                "可选下载 Claude Code 沙盒", prompt
            )
            if reply:
                self.progress_updated.emit(
                    "__deploy_progress__|cc_sandbox|下载 Claude Code 沙盒"
                )
                if not self._pull_images(distro, [CC_SANDBOX_IMAGE]):
                    if emit_status:
                        self.status_changed.emit("启动失败")
                    return False
            elif reply is None:
                _log("Claude Code 沙盒下载确认超时，已跳过", "warn")
            else:
                _log("已跳过 Claude Code 沙盒下载", "info")

        _log("启动 Docker Compose 服务...")
        if emit_status:
            self.progress_updated.emit(
                "__deploy_progress__|compose|启动 Docker Compose 服务"
            )
            self.progress_updated.emit("启动 Compose 服务...")
        compose_cmd = "docker compose -f docker-compose.yml --env-file .env up -d --remove-orphans"
        if not reuse_existing_runtime:
            compose_cmd = f"{compose_cmd} --force-recreate"
        try:
            proc = self._run_wsl_checked(
                distro,
                compose_cmd,
                action="[部署] Docker Compose 启动失败",
                cwd=deploy_dir,
                timeout=120,
            )
            compose_output = self._clean_command_output(proc.stdout)
            if compose_output:
                _log(f"Compose 输出:\n{compose_output}", "debug")
        except RuntimeError as e:
            _log(str(e), "error")
            if emit_status:
                self.status_changed.emit("启动失败")
            return False

        self.is_running = True
        self.progress_updated.emit("__deploy_progress__|health|等待服务就绪")
        _log("Compose 服务已启动，等待就绪...")
        deploy_info = self._parse_deploy_info(env_content, deploy_mode)
        # update_instance 会原地覆盖 inst["deploy_info"]，旧凭据必须先取出
        previous_info = inst.get("deploy_info") or {}
        self.config.update_instance(inst_id, deploy_info=deploy_info)

        if attach_logs and is_first_deploy:
            if deploy_mode == "napcat":
                self._pending_deploy_info = deploy_info
            else:
                self._show_deploy_info(deploy_info, inst_id=inst_id)
        else:
            if previous_info.get("napcat_token"):
                deploy_info["napcat_token"] = previous_info["napcat_token"]
            self.config.update_instance(inst_id, deploy_info=deploy_info)
            if attach_logs:
                self.config.set("deploy_info", deploy_info)

        if attach_logs:
            if self._log_process and self._log_process.poll() is None:
                try:
                    self._log_process.terminate()
                except Exception:
                    pass
                self._log_process = None
            threading.Thread(
                target=self._log_reader,
                args=(distro, deploy_dir, log_prefix, inst_id),
                daemon=True,
            ).start()
        if attach_health:
            nekro_port = normalize_port(
                deploy_info.get("port") or inst.get("nekro_port"),
                8021,
            )
            threading.Thread(
                target=self._health_check, args=(nekro_port,), daemon=True
            ).start()
        return True

    def start_services(self, deploy_mode, force_new_instance=False):
        """部署 Docker Compose 服务。"""
        if self._deploying:
            return True
        if self.is_running and not force_new_instance:
            return True

        self._deploying = True
        if force_new_instance:
            self._stop_event.set()
            if self._log_process and self._log_process.poll() is None:
                try:
                    self._log_process.terminate()
                except Exception:
                    pass
                self._log_process = None
            self._stop_event.clear()

        if not self._distro_exists():
            self.log_received.emit("NekroAgent 发行版不存在", "error")
            self._deploying = False
            return False

        self._stop_event.clear()
        self.status_changed.emit("启动中...")
        inst_id = self.config.get_active_instance_id() if self.config else ""
        inst = self.config.get_instance(inst_id) if self.config and inst_id else None
        if not inst:
            deploy_dir, data_dir, inst_name = self._get_active_deploy_paths()
            inst_id = inst_id or self.config.next_instance_id()
            inst = {
                "instance_name": inst_name,
                "deploy_dir": deploy_dir,
                "data_dir": data_dir,
                "deploy_mode": deploy_mode,
                "nekro_port": self.config.get("nekro_port") or 8021,
                "napcat_port": self.config.get("napcat_port") or 6099,
                "release_channel": self.config.get("release_channel") or "stable",
                "preview_backup_available": False,
            }
            self.config.set_instance(inst_id, inst)
            if not self.config.get_default_instance_id():
                self.config.set_default_instance_id(inst_id)
            self.config.set("active_instance", inst_id)

        def _deploy():
            try:
                self._start_instance_sync(
                    inst_id,
                    inst,
                    attach_logs=True,
                    attach_health=True,
                    emit_status=True,
                )
            except Exception as e:
                self.log_received.emit(f"部署失败: {e}", "error")
                self.status_changed.emit("启动失败")
            finally:
                self._deploying = False

        threading.Thread(target=_deploy, daemon=True).start()
        return True

    def start_all_services(self, default_instance_id=None):
        if self._deploying:
            return True
        instances = self.config.list_instances() if self.config else []
        if not instances:
            return self.start_services(
                self.config.get("deploy_mode") if self.config else "lite"
            )
        ports_ok, ports_message = validate_instance_port_conflicts(instances)
        if not ports_ok:
            self.log_received.emit(ports_message, "error")
            self.status_changed.emit("启动失败")
            return False
        if not self._distro_exists():
            self.log_received.emit("NekroAgent 发行版不存在", "error")
            return False

        self._deploying = True
        self._stop_event.clear()
        default_id = default_instance_id or self.config.get_default_instance_id()
        ordered = sorted(instances, key=lambda item: 0 if item[0] == default_id else 1)
        self.status_changed.emit("启动中...")
        self.log_received.emit("正在启动所有实例服务...", "info")

        def _sync_active_compat_fields(inst_id, inst):
            self.config.set_many(
                {
                    "active_instance": inst_id,
                    "deploy_mode": inst.get("deploy_mode", ""),
                    "nekro_port": inst.get("nekro_port", 8021),
                    "napcat_port": inst.get("napcat_port", 6099),
                    "release_channel": inst.get("release_channel", "stable"),
                    "preview_backup_available": bool(
                        inst.get("preview_backup_available", False)
                    ),
                    "deploy_info": inst.get("deploy_info"),
                }
            )

        def _deploy_all():
            failures = []
            try:
                for inst_id, inst in ordered:
                    attach = inst_id == default_id
                    if attach:
                        _sync_active_compat_fields(inst_id, inst)
                    try:
                        ok = self._start_instance_sync(
                            inst_id,
                            inst,
                            attach_logs=attach,
                            attach_health=attach,
                            emit_status=attach,
                        )
                        if not ok:
                            failures.append(inst_id)
                    except Exception as e:
                        name = inst.get("instance_name", "").rstrip("_") or inst_id
                        self.log_received.emit(f"[{name}] 启动异常: {e}", "error")
                        failures.append(inst_id)

                if default_id:
                    default_inst = self.config.get_instance(default_id)
                    if default_inst:
                        _sync_active_compat_fields(default_id, default_inst)

                if failures and default_id in failures:
                    self.status_changed.emit("启动失败")
                elif failures:
                    self.log_received.emit(
                        "部分实例启动失败，其余实例已继续启动", "warn"
                    )
                self.log_received.emit("所有实例启动流程已完成", "info")
            finally:
                self._deploying = False

        threading.Thread(target=_deploy_all, daemon=True).start()
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
                    threading.Thread(
                        target=self._log_reader,
                        args=(distro, deploy_dir, log_prefix, inst_id),
                        daemon=True,
                    ).start()

            try:
                compose_check = (
                    f"test -f {shlex.quote(deploy_dir)}/docker-compose.yml && echo yes"
                )
                has_compose = False
                try:
                    check_result = subprocess.run(
                        ["wsl", "-d", distro, "--", "bash", "-c", compose_check],
                        capture_output=True,
                        timeout=10,
                        creationflags=self._creation_flags(),
                    )
                    has_compose = "yes" in (
                        check_result.stdout.decode(errors="replace")
                        if isinstance(check_result.stdout, bytes)
                        else check_result.stdout
                    )
                    if check_result.returncode != 0:
                        self.log_received.emit(
                            self._format_command_failure(
                                f"{log_prefix}检查 Compose 文件失败",
                                cmd=compose_check,
                                distro=distro,
                                timeout=10,
                                returncode=check_result.returncode,
                                stdout=check_result.stdout,
                                stderr=check_result.stderr,
                            ),
                            "debug",
                        )
                except Exception as e:
                    self.log_received.emit(
                        self._format_command_failure(
                            f"{log_prefix}检查 Compose 文件异常",
                            cmd=compose_check,
                            distro=distro,
                            timeout=10,
                            exception=e,
                        ),
                        "debug",
                    )
                    pass

                if has_compose:
                    try:
                        self._run_wsl_checked(
                            distro,
                            "docker compose -f docker-compose.yml stop",
                            action=f"{log_prefix}停止 Compose 服务失败",
                            cwd=deploy_dir,
                            timeout=60,
                        )
                    except RuntimeError as e:
                        self.log_received.emit(
                            str(e),
                            "error",
                        )
                        _restore_runtime_state()
                        self.status_changed.emit("停止失败")
                        return
                else:
                    self.log_received.emit(
                        f"{log_prefix}当前实例无 Compose 部署文件，跳过 docker compose stop",
                        "warn",
                    )

                self.is_running = False
                self.log_received.emit(f"{log_prefix}服务已停止", "info")

                self.status_changed.emit("已停止")
            except Exception as e:
                detail = (
                    f"{log_prefix}停止服务异常\n"
                    f"发行版: {distro}\n"
                    f"部署目录: {deploy_dir}\n"
                    f"异常: {type(e).__name__}: {e}"
                )
                self.log_received.emit(detail, "error")
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
                        stdout = (
                            check_result.stdout.decode(errors="replace")
                            if isinstance(check_result.stdout, bytes)
                            else check_result.stdout
                        )
                        has_compose = "yes" in stdout
                        if check_result.returncode != 0:
                            self.log_received.emit(
                                self._format_command_failure(
                                    f"{prefix}检查 Compose 文件失败",
                                    cmd=compose_check,
                                    distro=distro,
                                    timeout=10,
                                    returncode=check_result.returncode,
                                    stdout=check_result.stdout,
                                    stderr=check_result.stderr,
                                ),
                                "debug",
                            )
                    except Exception as e:
                        self.log_received.emit(
                            self._format_command_failure(
                                f"{prefix}检查 Compose 文件异常",
                                cmd=compose_check,
                                distro=distro,
                                timeout=10,
                                exception=e,
                            ),
                            "debug",
                        )
                        pass

                    if not has_compose:
                        self.log_received.emit(
                            f"{prefix}无 Compose 部署文件，跳过", "warn"
                        )
                        continue

                    self.log_received.emit(f"{prefix}正在停止 Compose 服务...", "info")
                    try:
                        self._run_wsl_checked(
                            distro,
                            "docker compose -f docker-compose.yml stop",
                            action=f"{prefix}停止 Compose 服务失败",
                            cwd=deploy_dir,
                            timeout=60,
                        )
                    except RuntimeError as e:
                        failed.append(f"{name}:\n{e}")
                    else:
                        self.log_received.emit(f"{prefix}服务已停止", "info")

                if failed:
                    self.is_running = True
                    self._stop_event.clear()
                    self.log_received.emit(
                        "停止部分实例失败: " + "；".join(failed), "error"
                    )
                    self.status_changed.emit("停止失败")
                    return

                self.is_running = False
                self.log_received.emit("所有实例服务已停止", "info")
                self.status_changed.emit("已停止")
            except Exception as e:
                self.is_running = True
                self._stop_event.clear()
                self.log_received.emit(
                    "停止所有实例异常\n"
                    f"发行版: {distro}\n"
                    f"异常: {type(e).__name__}: {e}",
                    "error",
                )
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
                try:
                    self._run_wsl_checked(
                        distro,
                        (
                            "if [ -f docker-compose.yml ]; then "
                            "docker compose -f docker-compose.yml down -v; "
                            "fi; docker system prune -af"
                        ),
                        action="[卸载] 停止并删除容器失败",
                        cwd=deploy_dir,
                        timeout=120,
                    )
                except RuntimeError as e:
                    self.log_received.emit(
                        f"[卸载] ⚠ 容器清理失败，将继续删除发行版\n{e}",
                        "warn",
                    )
                self.log_received.emit("[卸载] ✓ 容器已清除", "info")

                self.log_received.emit("[卸载] 2/3 清理部署文件...", "info")
                self._remove_managed_deploy_dir(
                    distro,
                    deploy_dir,
                    "[卸载] 清理部署文件失败",
                )
                self.log_received.emit("[卸载] ✓ 部署文件已清理", "info")

                self.log_received.emit("[卸载] 3/3 删除 WSL 发行版...", "info")
                if not self.remove_distro():
                    self.status_changed.emit("卸载失败")
                    return
                self.log_received.emit("[卸载] ✓ 环境卸载完成", "info")

                if self.config:
                    self.config.set("wsl_distro", "")
                    self.config.set("wsl_install_dir", "")
                    self.config.set("image_status_cache", {})
                    self.config.set("image_update_last_alert_signature", "")
                    self.config.set("last_image_update_check_ts", 0)
                    self.config.set("instances", {})
                    self.config.clear_runtime_state(keep_first_run=True)

                self.status_changed.emit("已卸载")
            except Exception as e:
                self.log_received.emit(
                    "卸载异常\n"
                    f"发行版: {distro}\n"
                    f"异常: {type(e).__name__}: {e}",
                    "error",
                )
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
                        capture_output=True,
                        timeout=10,
                        creationflags=self._creation_flags(),
                    )
                    has_compose = "yes" in (
                        check_result.stdout.decode(errors="replace")
                        if isinstance(check_result.stdout, bytes)
                        else check_result.stdout
                    )
                    if check_result.returncode != 0:
                        self.log_received.emit(
                            self._format_command_failure(
                                f"[移除 {name}] 检查 Compose 文件失败",
                                cmd=compose_check,
                                distro=distro,
                                timeout=10,
                                returncode=check_result.returncode,
                                stdout=check_result.stdout,
                                stderr=check_result.stderr,
                            ),
                            "debug",
                        )
                except Exception as e:
                    self.log_received.emit(
                        self._format_command_failure(
                            f"[移除 {name}] 检查 Compose 文件异常",
                            cmd=compose_check,
                            distro=distro,
                            timeout=10,
                            exception=e,
                        ),
                        "debug",
                    )
                    has_compose = False

                if has_compose:
                    self._run_wsl_checked(
                        distro,
                        "docker compose -f docker-compose.yml down -v",
                        action=f"[移除 {name}] 停止并删除容器失败",
                        cwd=deploy_dir,
                        timeout=60,
                    )
                    self.log_received.emit(f"[移除 {name}] ✓ 容器已停止并清除", "info")
                else:
                    self.log_received.emit(
                        f"[移除 {name}] 无 Compose 文件，跳过容器清理", "warn"
                    )

                self.log_received.emit(f"[移除 {name}] 2/2 清理部署目录...", "info")
                if deploy_dir:
                    self._remove_managed_deploy_dir(
                        distro,
                        deploy_dir,
                        f"[移除 {name}] 清理部署目录失败",
                    )
                self.log_received.emit(f"[移除 {name}] ✓ 实例已移除", "info")
            except Exception as e:
                self.log_received.emit(
                    f"[移除 {name}] 异常\n"
                    f"发行版: {distro}\n"
                    f"部署目录: {deploy_dir or '<empty>'}\n"
                    f"异常: {type(e).__name__}: {e}",
                    "error",
                )
                success = False

            self.instance_removed.emit(success, inst_id, was_active)

        threading.Thread(target=_do_remove, daemon=True).start()

    def _show_deploy_info(self, info, inst_id=None):
        """保存凭据并发送信号给 UI 弹窗"""
        self._save_deploy_info(info, inst_id=inst_id)

        self.log_received.emit("=== 部署完成！===", "info")
        self.log_received.emit("管理员凭据已生成，请在部署凭据窗口查看。", "info")
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

    def _prepare_compose_content(self, compose_template_path, inst=None):
        content = ""
        if os.path.exists(compose_template_path):
            with open(compose_template_path, "r", encoding="utf-8") as f:
                content = f.read()

        agent_image = self.get_agent_image_ref(
            release_channel=self._instance_release_channel(inst)
        )
        if agent_image != STABLE_IMAGE:
            content = content.replace(
                f"image: {STABLE_IMAGE}",
                f"image: {agent_image}",
            )
        return content

    def _prepare_env(
        self,
        env_template_path,
        data_dir,
        existing_env_content="",
        nekro_port=None,
        napcat_port=None,
        instance_name=None,
        daemon_env=None,
    ):
        """读取 env 模板文件，填充必要值，返回最终 .env 内容"""
        content = ""
        if os.path.exists(env_template_path):
            with open(env_template_path, "r", encoding="utf-8") as f:
                content = f.read()

        existing_env = self._parse_env_values(existing_env_content or "")
        if instance_name is None:
            _, _, instance_name = self._get_active_deploy_paths()
        nekro_port = nekro_port or self.config.get("nekro_port") or 8021
        napcat_port = napcat_port or self.config.get("napcat_port") or 6099

        lines = content.splitlines()
        new_lines = []
        daemon_env = daemon_env or {}
        seen_keys = set()
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue

            key = stripped.split("=", 1)[0].strip()
            seen_keys.add(key)
            existing_value = existing_env.get(key, "").strip()

            if key in daemon_env:
                new_lines.append(f"{key}={daemon_env[key]}")
            elif key == "NEKRO_DATA_DIR":
                new_lines.append(f"NEKRO_DATA_DIR={data_dir}")
            elif key == "NEKRO_EXPOSE_PORT":
                new_lines.append(f"NEKRO_EXPOSE_PORT={nekro_port}")
            elif key == "NAPCAT_EXPOSE_PORT":
                new_lines.append(f"NAPCAT_EXPOSE_PORT={napcat_port}")
            elif key == "QDRANT_API_KEY":
                new_lines.append(
                    f"QDRANT_API_KEY={existing_value or self._random_token(32)}"
                )
            elif key == "ONEBOT_ACCESS_TOKEN":
                new_lines.append(
                    f"ONEBOT_ACCESS_TOKEN={existing_value or self._random_token(32)}"
                )
            elif key == "NEKRO_ADMIN_PASSWORD":
                new_lines.append(
                    f"NEKRO_ADMIN_PASSWORD={existing_value or self._random_token(16)}"
                )
            elif key == "INSTANCE_NAME":
                new_lines.append(f"{key}={existing_value or instance_name}")
            elif (
                key in {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DATABASE"}
                and existing_value
            ):
                new_lines.append(f"{key}={existing_value}")
            else:
                new_lines.append(line)

        for key, value in daemon_env.items():
            if key not in seen_keys:
                new_lines.append(f"{key}={value}")

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
            self.napcat_network_config_finished.emit(
                {"status": status, "message": message, **extra}
            )

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
                    _emit(
                        "config_missing",
                        "未找到 NapCat 配置目录，请先完成 NapCat 部署。",
                    )
                    return

                files_output = self._wsl_exec_checked(
                    distro,
                    f'find "{config_dir}" -maxdepth 1 -type f -name \'onebot11_*.json\' | sort',
                )
                onebot_paths = [
                    line.strip()
                    for line in self._clean_command_output(files_output).splitlines()
                    if line.strip()
                ]
                if not onebot_paths:
                    _emit(
                        "login_required",
                        "尚未检测到 NapCat 账号配置，请先登录一次 QQ。",
                    )
                    return

                backup_dir = (
                    f"{config_dir}/_launcher_backup_{time.strftime('%Y%m%d-%H%M%S')}"
                )
                self._wsl_exec_checked(distro, f'mkdir -p "{backup_dir}"')

                configured_accounts = []
                for path in onebot_paths:
                    content = self._wsl_exec_checked(distro, f'cat "{path}"')
                    if not content.strip():
                        _emit(
                            "config_read_failed",
                            f"读取 NapCat 配置失败: {os.path.basename(path)}",
                        )
                        return

                    try:
                        data = json.loads(content)
                    except json.JSONDecodeError:
                        _emit(
                            "config_invalid",
                            f"NapCat 配置文件格式异常: {os.path.basename(path)}",
                        )
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

                        if (
                            client.get("name") == desired_client["name"]
                            or client.get("url") == desired_client["url"]
                        ):
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
                    self._write_to_wsl(
                        distro,
                        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        path,
                    )

                    filename = os.path.basename(path)
                    account = filename[len("onebot11_") : -len(".json")]
                    if account:
                        configured_accounts.append(account)

                deploy_dir, _, _ = self._get_active_deploy_paths()
                try:
                    self._run_wsl_checked(
                        distro,
                        "docker compose -f docker-compose.yml restart nekro_napcat",
                        action="[NapCat 配网] 重启 NapCat 服务失败",
                        cwd=deploy_dir,
                        timeout=60,
                    )
                except RuntimeError as e:
                    self.log_received.emit(
                        "NapCat 配置文件已写入，但重启 NapCat 失败", "warn"
                    )
                    self.log_received.emit(str(e), "debug")
                    _emit(
                        "restart_failed",
                        "NapCat 配置已写入，但重启服务失败，请手动重启 NapCat 后再验证。\n\n"
                        + str(e),
                        backup_dir=backup_dir,
                    )
                    return

                self.log_received.emit(
                    "NapCat 配置文件已更新，并已重启 NapCat 服务", "info"
                )
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
