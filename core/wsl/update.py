import shlex
import threading
import time
import urllib.error
import urllib.request
import json

from core.port_utils import normalize_port
from core.wsl.constants import (
    CC_SANDBOX_IMAGE,
    DISTRO_NAME,
    NA_BACKUP_TARGETS,
    PREVIEW_BACKUP_ARCHIVE_PATH,
    PREVIEW_COMPOSE_IMAGE,
    PREVIEW_IMAGE,
    STABLE_IMAGE,
    UPDATE_BACKUP_ARCHIVE_PATH,
)


class WSLUpdateMixin:
    def _preview_backup_archive_path(self, inst_id=None):
        inst = self.config.get_instance(inst_id) if self.config and inst_id else None
        if inst is None and self.config:
            inst = self.config.get_instance()
        instance_name = (inst or {}).get("instance_name", "").strip()
        if instance_name:
            archive_name = f"{instance_name.rstrip('_')}_preview_backup.tar.gz"
            return f"/root/{archive_name}"
        return PREVIEW_BACKUP_ARCHIVE_PATH

    def _backup_target_candidates(self, distro):
        deploy_dir, data_dir, _ = self._get_active_deploy_paths()
        env_path = f"{deploy_dir}/.env"
        instance_name = ""

        env_content = self._wsl_exec(
            distro,
            f"test -f {shlex.quote(env_path)} && cat {shlex.quote(env_path)}",
            timeout=30,
        )
        for line in env_content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "INSTANCE_NAME":
                instance_name = value.strip()
                break

        volume_prefix = instance_name
        postgres_volume = f"/var/lib/docker/volumes/{volume_prefix}nekro_postgres_data"
        qdrant_volume = f"/var/lib/docker/volumes/{volume_prefix}nekro_qdrant_data"
        targets = [
            postgres_volume,
            qdrant_volume,
            data_dir,
            deploy_dir,
        ]

        for target in NA_BACKUP_TARGETS:
            if target not in targets:
                targets.append(target)
        return targets

    def get_backup_size_hint(self):
        distro = DISTRO_NAME
        existing_targets = self._existing_backup_targets(distro)
        if not existing_targets:
            return "未知"
        cmd = "du -shc " + " ".join(shlex.quote(target) for target in existing_targets) + " | tail -n1 | cut -f1"
        size = self._wsl_exec(distro, cmd, timeout=60).strip()
        return size or "未知"

    def preview_backup_exists(self, inst_id=None):
        distro = DISTRO_NAME
        archive_path = self._preview_backup_archive_path(inst_id=inst_id)
        return self._wsl_exec(
            distro,
            f"test -f {shlex.quote(archive_path)} && echo yes",
        ).strip() == "yes"

    def run_remote_update(self):
        """执行内置升级流程，optional 步骤通过信号询问用户确认。"""
        from core.update_runner import build_update_plan, log_update_plan

        distro = DISTRO_NAME
        deploy_dir, _, _ = self._get_active_deploy_paths()

        def _exec(cmd, timeout=300, action="[更新] 命令执行失败"):
            proc = self._run_wsl_checked(
                distro,
                cmd,
                action=action,
                cwd=deploy_dir,
                timeout=timeout,
            )
            out = self._clean_command_output(self._safe_decode(proc.stdout) + self._safe_decode(proc.stderr))
            return proc.returncode, out.strip()

        def _wait_optional_reply(label, prompt):
            self._update_optional_reply = None
            self.update_optional_confirm.emit(label, prompt)

            import time as _time

            deadline = _time.time() + 120
            while self._update_optional_reply is None and _time.time() < deadline:
                _time.sleep(0.1)
            return self._update_optional_reply

        def _do_update():
            self._stop_event.clear()
            self.status_changed.emit("更新中...")
            agent_image = self.get_agent_image_ref(self.config)
            steps = build_update_plan(agent_image)
            log_update_plan(self.log_received.emit, steps)
            if not steps:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "未找到可用的升级步骤。")
                return

            for step in steps:
                step_type = step.get("type")
                label = step.get("label", "")

                if step_type == "notify":
                    self.log_received.emit(step.get("message", label), "info")
                    continue

                self.log_received.emit(f"[更新] 执行: {label}", "info")
                self._emit_pull_progress("stage", label)

                if step_type == "backup":
                    if step.get("optional"):
                        backup_size = self.get_backup_size_hint()
                        prompt = step.get("optional_prompt", f"是否执行：{label}？").replace("{backup_size}", backup_size)
                        reply = _wait_optional_reply(label, prompt)
                        if reply is None:
                            self.log_received.emit(f"[更新] 可选步骤确认超时，已跳过: {label}", "warning")
                            continue
                        if not reply:
                            self.log_received.emit(f"[更新] 已跳过可选步骤: {label}", "info")
                            continue

                    ok, backup_message = self._backup_nekro_archive(
                        distro,
                        step.get("archive_path", UPDATE_BACKUP_ARCHIVE_PATH),
                    )
                    if not ok:
                        self.status_changed.emit("更新失败")
                        self.update_finished.emit(False, backup_message)
                        return
                    self.log_received.emit(f"[更新] {backup_message}", "info")
                    continue

                if step_type == "pull":
                    image = step.get("image", "")
                    ok = self._pull_images(distro, [image])
                    if not ok:
                        self.status_changed.emit("更新失败")
                        self.update_finished.emit(False, f"镜像拉取失败: {image}")
                        return
                    continue

                if step_type == "compose_up":
                    services = step.get("services", [])
                    service_args = " ".join(shlex.quote(service) for service in services)
                    cmd = (
                        "docker compose -f docker-compose.yml --env-file .env "
                        f"up -d --no-deps --force-recreate {service_args}"
                    ).strip()
                    try:
                        _rc, out = _exec(
                            cmd,
                            timeout=300,
                            action=f"[更新] {label}失败",
                        )
                    except Exception as e:
                        self.status_changed.emit("更新失败")
                        self.update_finished.emit(False, str(e))
                        return

                    if out:
                        self.log_received.emit(out, "info")

                    self.log_received.emit(f"[更新] ✓ {label}", "info")
                    continue

                self.log_received.emit(f"[更新] 未知步骤类型，已跳过: {step_type}", "warning")

            self._emit_pull_progress("done", "更新完成")
            self.is_running = True
            inst_id = self.config.get_active_instance_id() if self.config else ""
            inst_display = inst_id if inst_id and inst_id != "default" else ""
            log_prefix = f"[{inst_display}] " if inst_display else ""
            nekro_port = (
                normalize_port(self.config.get("nekro_port"), 8021)
                if self.config
                else 8021
            )
            if self._log_process is None or self._log_process.poll() is not None:
                threading.Thread(
                    target=self._log_reader,
                    args=(distro, deploy_dir, log_prefix, inst_id),
                    daemon=True,
                ).start()
            threading.Thread(target=self._health_check, args=(nekro_port,), daemon=True).start()
            self.update_finished.emit(True, "Nekro Agent 更新完成，正在等待服务重新就绪。")

        threading.Thread(target=_do_update, daemon=True).start()

    def run_daemon_update_job(self, request: dict, job):
        """执行 daemon facade 的非交互 stable 更新任务。"""
        from core.update_runner import build_update_plan

        distro = DISTRO_NAME
        deploy_dir, _, _ = self._get_active_deploy_paths()
        channel = request.get("channel") or "stable"
        if channel != "stable":
            job.fail("invalid_channel", "Windows 启动器 daemon 首版仅支持 stable 更新")
            return

        backup = bool(request.get("backup", True))
        update_sandbox = bool(request.get("update_sandbox", True))
        update_cc_sandbox = bool(request.get("update_cc_sandbox", False))
        agent_image = self.get_agent_image_ref(self.config)
        steps = [
            step for step in build_update_plan(agent_image)
            if step.get("type") != "notify"
        ]
        if not backup:
            steps = [step for step in steps if step.get("type") != "backup"]
        if update_sandbox:
            steps.append(
                {
                    "type": "pull",
                    "label": "拉取 Nekro Agent 沙盒镜像",
                    "image": "kromiose/nekro-agent-sandbox",
                    "phase": "pull_sandbox",
                }
            )
        if update_cc_sandbox:
            steps.append(
                {
                    "type": "pull",
                    "label": "拉取 Claude Code 沙盒镜像",
                    "image": CC_SANDBOX_IMAGE,
                    "phase": "pull_sandbox",
                }
            )
        steps.append({"type": "verify", "label": "等待服务健康检查通过"})

        total = len(steps) + 1
        current = 1
        self._stop_event.clear()
        self.status_changed.emit("更新中...")
        job.start("validate_instance", "正在校验实例与 Docker 环境")

        def _fail(code, message, details=None):
            self.status_changed.emit("更新失败")
            job.fail(code, message, details=details)

        def _exec(cmd, timeout=300, action="[daemon 更新] 命令执行失败"):
            proc = self._run_wsl_checked(
                distro,
                cmd,
                action=action,
                cwd=deploy_dir,
                timeout=timeout,
            )
            out = self._clean_command_output(
                self._safe_decode(proc.stdout) + self._safe_decode(proc.stderr)
            ).strip()
            if out:
                job.add_log(out, stream="stdout")
            return proc.returncode, out

        try:
            self._run_wsl_checked(
                distro,
                "test -f docker-compose.yml && test -f .env",
                action="[daemon 更新] 实例部署文件缺失",
                cwd=deploy_dir,
                timeout=30,
            )
            self._run_wsl_checked(
                distro,
                "systemctl start docker && docker version >/dev/null && docker compose version >/dev/null",
                action="[daemon 更新] Docker 或 Compose 不可用",
                timeout=60,
            )
        except Exception as e:
            _fail("docker_unavailable", str(e))
            return

        for step in steps:
            current += 1
            step_type = str(step.get("type") or "")
            label = str(step.get("label") or "")
            phase = str(step.get("phase") or "") or {
                "backup": "backup",
                "pull": "pull_images",
                "compose_up": "restart_services",
                "verify": "verify",
            }.get(step_type, "validate_instance")
            job.set_progress(phase, current, total, label)
            self.log_received.emit(f"[daemon 更新] {label}", "info")
            self._emit_pull_progress("stage", label)

            if step_type == "backup":
                ok, backup_message = self._backup_nekro_archive(
                    distro,
                    step.get("archive_path", UPDATE_BACKUP_ARCHIVE_PATH),
                )
                if not ok:
                    _fail("backup_failed", backup_message)
                    return
                job.add_log(backup_message)
                continue

            if step_type == "pull":
                image = step.get("image", "")
                if not self._pull_images(distro, [image]):
                    _fail("pull_failed", f"镜像拉取失败: {image}", {"image": image})
                    return
                continue

            if step_type == "compose_up":
                services = step.get("services", [])
                service_args = " ".join(shlex.quote(service) for service in services)
                cmd = (
                    "docker compose -f docker-compose.yml --env-file .env "
                    f"up -d --no-deps --force-recreate {service_args}"
                ).strip()
                try:
                    _exec(
                        cmd,
                        timeout=300,
                        action=f"[daemon 更新] {label}失败",
                    )
                except Exception as e:
                    _fail("restart_failed", str(e))
                    return
                continue

            if step_type == "verify":
                if not self._wait_daemon_update_health(job):
                    _fail("verify_timeout", "服务健康检查超时")
                    return
                continue

        self._emit_pull_progress("done", "更新完成")
        self.is_running = True
        inst_id = self.config.get_active_instance_id() if self.config else ""
        inst_display = inst_id if inst_id and inst_id != "default" else ""
        log_prefix = f"[{inst_display}] " if inst_display else ""
        nekro_port = (
            normalize_port(self.config.get("nekro_port"), 8021)
            if self.config
            else 8021
        )
        if self._log_process is None or self._log_process.poll() is not None:
            threading.Thread(
                target=self._log_reader,
                args=(distro, deploy_dir, log_prefix, inst_id),
                daemon=True,
            ).start()
        self.status_changed.emit("运行中")
        job.succeed(
            "Nekro Agent 更新完成",
            {
                "channel": "stable",
                "image": agent_image,
                "app_health": "ok",
            },
        )

    def _wait_daemon_update_health(self, job, timeout=120):
        port = normalize_port(self.config.get("nekro_port"), 8021) if self.config else 8021
        url = f"http://127.0.0.1:{port}/api/health"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    body = response.read(4096).decode("utf-8", errors="replace")
                    if response.status == 200:
                        try:
                            payload = json.loads(body or "{}")
                        except json.JSONDecodeError:
                            payload = {}
                        if payload.get("ok") is True:
                            job.add_log(f"健康检查通过: {url}")
                            return True
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(2)
        return False

    def switch_to_preview(self, create_backup=True):
        """备份数据与配置后，将 Nekro Agent 主容器切换到预览版镜像。"""
        distro = DISTRO_NAME
        deploy_dir, _, _ = self._get_active_deploy_paths()

        def _exec(cmd, timeout=300, action="[预览版] 命令执行失败"):
            proc = self._run_wsl_checked(
                distro,
                cmd,
                action=action,
                cwd=deploy_dir,
                timeout=timeout,
            )
            out = self._clean_command_output(self._safe_decode(proc.stdout) + self._safe_decode(proc.stderr))
            return proc.returncode, out.strip()

        def _emit_cmd_failure(prefix, detail):
            self.status_changed.emit("更新失败")
            self.update_finished.emit(False, f"{prefix}\n{detail}" if detail else prefix)

        def _rewrite_compose_to_preview():
            compose_path = f"{deploy_dir}/docker-compose.yml"
            self._emit_pull_progress("stage", "写入预览版镜像配置")
            try:
                compose_content = self._wsl_exec_checked(
                    distro,
                    f"cat {shlex.quote(compose_path)}",
                    timeout=30,
                )
            except RuntimeError as e:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, f"读取 Compose 文件失败\n{e}")
                return False
            if PREVIEW_IMAGE in compose_content:
                return True
            if PREVIEW_COMPOSE_IMAGE not in compose_content:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "未找到稳定版镜像引用，无法切换到预览版。")
                return False

            updated_content = compose_content.replace(PREVIEW_COMPOSE_IMAGE, PREVIEW_IMAGE)
            try:
                self._write_to_wsl(distro, updated_content, compose_path)
            except RuntimeError as e:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, f"写入预览版 Compose 配置失败\n{e}")
                return False
            return True

        def _do_switch():
            self._stop_event.clear()
            self.status_changed.emit("更新中...")
            self.log_received.emit("[预览版] 开始切换到预览版 Nekro Agent", "info")

            try:
                if create_backup:
                    self._emit_pull_progress("stage", "备份数据与配置")
                    archive_path = self._preview_backup_archive_path()
                    ok, backup_message = self._backup_nekro_archive(
                        distro,
                        archive_path,
                    )
                    if not ok:
                        self.status_changed.emit("更新失败")
                        self.update_finished.emit(False, backup_message)
                        return
                    self.log_received.emit(f"[预览版] {backup_message}", "info")
                else:
                    self.log_received.emit("[预览版] 已按用户选择跳过备份，后续将无法恢复到正式版。", "warning")

                self.log_received.emit("[预览版] 拉取预览版镜像", "info")
                self._emit_pull_progress("stage", "拉取预览版镜像")
                if not self._pull_images(distro, [PREVIEW_IMAGE]):
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"镜像拉取失败: {PREVIEW_IMAGE}")
                    return

                if not _rewrite_compose_to_preview():
                    return

                self._emit_pull_progress("stage", "重建 Nekro Agent 主容器")
                try:
                    recreate_cmd = (
                        "docker compose -f docker-compose.yml --env-file .env up -d "
                        "--no-deps --force-recreate nekro_agent"
                    )
                    _rc, out = _exec(
                        recreate_cmd,
                        action="[预览版] 重建 Nekro Agent 主容器失败",
                    )
                except Exception as e:
                    _emit_cmd_failure("重建 Nekro Agent 主容器失败", str(e))
                    return
                if out:
                    self.log_received.emit(out, "info")

                self._emit_pull_progress("done", "预览版切换完成")
                self.is_running = True
                inst_id = self.config.get_active_instance_id() if self.config else ""
                inst_display = inst_id if inst_id and inst_id != "default" else ""
                log_prefix = f"[{inst_display}] " if inst_display else ""
                nekro_port = (
                    normalize_port(self.config.get("nekro_port"), 8021)
                    if self.config
                    else 8021
                )
                if self.config:
                    preview_available = bool(create_backup)
                    if inst_id:
                        self.config.update_instance_with_globals(
                            inst_id,
                            instance_updates={
                                "release_channel": "preview",
                                "preview_backup_available": preview_available,
                            },
                            global_updates={
                                "release_channel": "preview",
                                "preview_backup_available": preview_available,
                            },
                        )
                    else:
                        self.config.set_many(
                            {
                                "release_channel": "preview",
                                "preview_backup_available": preview_available,
                            }
                        )
                if self._log_process is None or self._log_process.poll() is not None:
                    threading.Thread(
                        target=self._log_reader,
                        args=(distro, deploy_dir, log_prefix, inst_id),
                        daemon=True,
                    ).start()
                threading.Thread(target=self._health_check, args=(nekro_port,), daemon=True).start()
                self.update_finished.emit(True, "预览版切换完成，正在等待服务重新就绪。")
            except Exception as e:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(
                    False,
                    "切换到预览版异常\n"
                    f"发行版: {distro}\n"
                    f"部署目录: {deploy_dir}\n"
                    f"异常: {type(e).__name__}: {e}",
                )

        threading.Thread(target=_do_switch, daemon=True).start()

    def restore_stable_from_backup(self):
        """从预览版备份恢复正式版。"""
        distro = DISTRO_NAME
        deploy_dir, _, _ = self._get_active_deploy_paths()

        def _exec(cmd, timeout=300, action="[恢复正式版] 命令执行失败"):
            proc = self._run_wsl_checked(
                distro,
                cmd,
                action=action,
                cwd=deploy_dir,
                timeout=timeout,
            )
            out = self._clean_command_output(self._safe_decode(proc.stdout) + self._safe_decode(proc.stderr))
            return proc.returncode, out.strip()

        def _rewrite_compose_to_stable():
            compose_path = f"{deploy_dir}/docker-compose.yml"
            self._emit_pull_progress("stage", "写回正式版镜像配置")
            try:
                compose_content = self._wsl_exec_checked(
                    distro,
                    f"cat {shlex.quote(compose_path)}",
                    timeout=30,
                )
            except RuntimeError as e:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, f"读取 Compose 文件失败\n{e}")
                return False
            if STABLE_IMAGE in compose_content and PREVIEW_IMAGE not in compose_content:
                return True
            if PREVIEW_IMAGE not in compose_content:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "未找到预览版镜像引用，无法恢复正式版。")
                return False
            updated_content = compose_content.replace(PREVIEW_IMAGE, STABLE_IMAGE)
            try:
                self._write_to_wsl(distro, updated_content, compose_path)
            except RuntimeError as e:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, f"写入正式版 Compose 配置失败\n{e}")
                return False
            return True

        def _do_restore():
            self._stop_event.clear()
            self.status_changed.emit("更新中...")
            self.log_received.emit("[恢复正式版] 开始从预览版备份恢复正式版", "info")

            archive_path = self._preview_backup_archive_path()

            if self.config and not self.config.get_active_preview_backup_available():
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "当前预览版是在未备份的情况下切换的，无法恢复到正式版。")
                return

            if not self.preview_backup_exists():
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, f"未找到备份文件：{archive_path}")
                return

            try:
                self._emit_pull_progress("stage", "停止相关服务")
                stop_services_cmd = (
                    "docker compose -f docker-compose.yml stop "
                    "nekro_agent nekro_postgres nekro_qdrant nekro_napcat "
                    "2>/dev/null || true"
                )
                _rc, out = _exec(
                    stop_services_cmd,
                    timeout=120,
                    action="[恢复正式版] 停止相关服务失败",
                )
                if out:
                    self.log_received.emit(out, "info")

                self._emit_pull_progress("stage", "恢复备份数据")
                try:
                    _rc, out = _exec(
                        f"tar -xzf {shlex.quote(archive_path)} -C /",
                        timeout=600,
                        action="[恢复正式版] 恢复备份数据失败",
                    )
                except Exception as e:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, str(e))
                    return
                if out:
                    self.log_received.emit(out, "info")

                self.log_received.emit("[恢复正式版] 拉取正式版镜像", "info")
                self._emit_pull_progress("stage", "拉取正式版镜像")
                if not self._pull_images(distro, [STABLE_IMAGE]):
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"镜像拉取失败: {STABLE_IMAGE}")
                    return

                if not _rewrite_compose_to_stable():
                    return

                self._emit_pull_progress("stage", "重建并启动服务")
                try:
                    _rc, out = _exec(
                        "docker compose -f docker-compose.yml --env-file .env up -d",
                        timeout=300,
                        action="[恢复正式版] 重建并启动服务失败",
                    )
                except Exception as e:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, str(e))
                    return
                if out:
                    self.log_received.emit(out, "info")

                self._emit_pull_progress("done", "正式版恢复完成")
                self.is_running = True
                inst_id = self.config.get_active_instance_id() if self.config else ""
                inst_display = inst_id if inst_id and inst_id != "default" else ""
                log_prefix = f"[{inst_display}] " if inst_display else ""
                nekro_port = (
                    normalize_port(self.config.get("nekro_port"), 8021)
                    if self.config
                    else 8021
                )
                if self.config:
                    if inst_id:
                        self.config.update_instance_with_globals(
                            inst_id,
                            instance_updates={
                                "release_channel": "stable",
                                "preview_backup_available": False,
                            },
                            global_updates={
                                "release_channel": "stable",
                                "preview_backup_available": False,
                            },
                        )
                    else:
                        self.config.set_many(
                            {
                                "release_channel": "stable",
                                "preview_backup_available": False,
                            }
                        )
                if self._log_process is None or self._log_process.poll() is not None:
                    threading.Thread(
                        target=self._log_reader,
                        args=(distro, deploy_dir, log_prefix, inst_id),
                        daemon=True,
                    ).start()
                threading.Thread(target=self._health_check, args=(nekro_port,), daemon=True).start()
                self.update_finished.emit(True, "正式版恢复完成，正在等待服务重新就绪。")
            except Exception as e:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(
                    False,
                    "恢复正式版异常\n"
                    f"发行版: {distro}\n"
                    f"部署目录: {deploy_dir}\n"
                    f"异常: {type(e).__name__}: {e}",
                )

        threading.Thread(target=_do_restore, daemon=True).start()

    def reply_update_optional(self, confirmed: bool):
        """UI 调用此方法回复 optional 步骤的用户选择"""
        self._update_optional_reply = confirmed

    def _existing_backup_targets(self, distro):
        existing = []
        for target in self._backup_target_candidates(distro):
            if self._wsl_exec(distro, f"test -d {shlex.quote(target)} && echo yes").strip() == "yes":
                existing.append(target)
        return existing

    def _backup_nekro_archive(self, distro, archive_path):
        existing_targets = self._existing_backup_targets(distro)
        if not existing_targets:
            return False, "未找到可备份的目录。"

        target_args = " ".join(shlex.quote(target.lstrip("/")) for target in existing_targets)
        cmd = f"rm -f {shlex.quote(archive_path)} && tar -czf {shlex.quote(archive_path)} -C / {target_args}"
        try:
            self._run_wsl_checked(
                distro,
                cmd,
                action="创建备份归档失败",
                timeout=600,
            )
        except Exception as e:
            return False, (
                f"备份失败\n备份文件: {archive_path}\n"
                f"备份目录:\n" + "\n".join(existing_targets) + f"\n{e}"
            )
        return True, f"已生成备份归档：{archive_path}"
