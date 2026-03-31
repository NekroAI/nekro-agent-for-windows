import os
import shlex
import subprocess
import threading

from core.wsl.constants import (
    DISTRO_NAME,
    NA_BACKUP_TARGETS,
    PREVIEW_BACKUP_ARCHIVE_PATH,
    PREVIEW_COMPOSE_IMAGE,
    PREVIEW_IMAGE,
    STABLE_IMAGE,
)


class WSLUpdateMixin:
    def _backup_target_candidates(self, distro):
        deploy_dir = "/root/nekro_agent"
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
            "/root/nekro_agent_data",
            "/root/nekro_agent",
        ]

        # 兼容旧逻辑，避免历史无前缀数据目录漏备份。
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

    def preview_backup_exists(self):
        distro = DISTRO_NAME
        return self._wsl_exec(distro, f"test -f {shlex.quote(PREVIEW_BACKUP_ARCHIVE_PATH)} && echo yes").strip() == "yes"

    def run_remote_update(self):
        """从远端 JSON 拉取更新步骤并执行，optional 步骤通过信号询问用户确认"""
        from core.update_runner import parse_update_commands

        distro = DISTRO_NAME
        deploy_dir = "/root/nekro_agent"

        def _exec(cmd, timeout=300):
            proc = subprocess.run(
                ["wsl", "-d", distro, "--", "bash", "-c", f"cd {deploy_dir} && {cmd}"],
                capture_output=True,
                timeout=timeout,
                creationflags=self._creation_flags(),
            )
            out = self._clean_command_output(self._safe_decode(proc.stdout) + self._safe_decode(proc.stderr))
            return proc.returncode, out.strip()

        def _do_update():
            self._stop_event.clear()
            self.status_changed.emit("更新中...")
            steps = parse_update_commands(self.log_received.emit)
            if not steps:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "无法获取更新配置，请检查网络。")
                return

            for step in steps:
                step_type = step.get("type")
                label = step.get("label", "")
                cmd = step.get("_cmd")

                if step_type == "notify":
                    self.log_received.emit(step.get("message", label), "info")
                    continue

                if step.get("optional"):
                    size_cmd = step.get("optional_size_cmd", "")
                    backup_size = "未知"
                    if size_cmd:
                        _, size_out = _exec(size_cmd, timeout=30)
                        if size_out:
                            backup_size = size_out.splitlines()[0].strip()
                    prompt = step.get("optional_prompt", f"是否执行：{label}？")
                    prompt = prompt.replace("{backup_size}", backup_size)

                    self._update_optional_reply = None
                    self.update_optional_confirm.emit(label, prompt)
                    import time as _time

                    deadline = _time.time() + 120
                    while self._update_optional_reply is None and _time.time() < deadline:
                        _time.sleep(0.1)
                    if self._update_optional_reply is None:
                        self.log_received.emit(f"[更新] 可选步骤确认超时，已跳过: {label}", "warning")
                        continue
                    if not self._update_optional_reply:
                        self.log_received.emit(f"[更新] 已跳过可选步骤: {label}", "info")
                        continue

                if cmd is None:
                    continue

                self.log_received.emit(f"[更新] 执行: {label}", "info")
                self._emit_pull_progress("stage", label)

                if step_type == "pull":
                    image = step.get("image", "")
                    ok = self._pull_images(distro, [image])
                    if not ok:
                        self.status_changed.emit("更新失败")
                        self.update_finished.emit(False, f"镜像拉取失败: {image}")
                        return
                    continue

                try:
                    rc, out = _exec(cmd, timeout=300)
                except subprocess.TimeoutExpired:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"步骤超时: {label}")
                    return
                except Exception as e:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"步骤异常: {e}")
                    return

                if out:
                    self.log_received.emit(out, "info")
                if rc != 0:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"步骤失败（返回码 {rc}）: {label}")
                    return

                self.log_received.emit(f"[更新] ✓ {label}", "info")

            self._emit_pull_progress("done", "更新完成")
            self.is_running = True
            if self._log_process is None or self._log_process.poll() is not None:
                threading.Thread(target=self._log_reader, args=(distro, deploy_dir), daemon=True).start()
            threading.Thread(target=self._health_check, daemon=True).start()
            self.update_finished.emit(True, "Nekro Agent 更新完成，正在等待服务重新就绪。")

        threading.Thread(target=_do_update, daemon=True).start()

    def switch_to_preview(self, create_backup=True):
        """备份数据与配置后，将 Nekro Agent 主容器切换到预览版镜像。"""
        distro = DISTRO_NAME
        deploy_dir = "/root/nekro_agent"

        def _exec(cmd, timeout=300):
            proc = subprocess.run(
                ["wsl", "-d", distro, "--", "bash", "-c", f"cd {deploy_dir} && {cmd}"],
                capture_output=True,
                timeout=timeout,
                creationflags=self._creation_flags(),
            )
            out = self._clean_command_output(self._safe_decode(proc.stdout) + self._safe_decode(proc.stderr))
            return proc.returncode, out.strip()

        def _emit_cmd_failure(prefix, rc, output):
            self.status_changed.emit("更新失败")
            message = f"{prefix}（返回码 {rc}）"
            if output:
                message += f"\n{output}"
            self.update_finished.emit(False, message)

        def _rewrite_compose_to_preview():
            compose_path = f"{deploy_dir}/docker-compose.yml"
            self._emit_pull_progress("stage", "写入预览版镜像配置")
            compose_content = self._wsl_exec(distro, f"cat {compose_path}")
            if PREVIEW_IMAGE in compose_content:
                return True
            if PREVIEW_COMPOSE_IMAGE not in compose_content:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "未找到稳定版镜像引用，无法切换到预览版。")
                return False

            updated_content = compose_content.replace(PREVIEW_COMPOSE_IMAGE, PREVIEW_IMAGE)
            self._write_to_wsl(distro, updated_content, compose_path)
            return True

        def _do_switch():
            self._stop_event.clear()
            self.status_changed.emit("更新中...")
            self.log_received.emit("[预览版] 开始切换到预览版 Nekro Agent", "info")

            try:
                if create_backup:
                    self._emit_pull_progress("stage", "备份数据与配置")
                    ok, backup_message = self._backup_nekro_archive(distro, PREVIEW_BACKUP_ARCHIVE_PATH)
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
                rc, out = _exec("docker compose -f docker-compose.yml --env-file .env up -d --no-deps --force-recreate nekro_agent")
                if rc != 0:
                    _emit_cmd_failure("重建 Nekro Agent 主容器失败", rc, out)
                    return
                if out:
                    self.log_received.emit(out, "info")

                self._emit_pull_progress("done", "预览版切换完成")
                self.is_running = True
                if self.config:
                    self.config.set("release_channel", "preview")
                    self.config.set("preview_backup_available", bool(create_backup))
                if self._log_process is None or self._log_process.poll() is not None:
                    threading.Thread(target=self._log_reader, args=(distro, deploy_dir), daemon=True).start()
                threading.Thread(target=self._health_check, daemon=True).start()
                self.update_finished.emit(True, "预览版切换完成，正在等待服务重新就绪。")
            except subprocess.TimeoutExpired:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "切换到预览版超时。")
            except Exception as e:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, f"切换到预览版异常: {e}")

        threading.Thread(target=_do_switch, daemon=True).start()

    def restore_stable_from_backup(self):
        """从预览版备份恢复正式版。"""
        distro = DISTRO_NAME
        deploy_dir = "/root/nekro_agent"

        def _exec(cmd, timeout=300):
            proc = subprocess.run(
                ["wsl", "-d", distro, "--", "bash", "-c", f"cd {deploy_dir} && {cmd}"],
                capture_output=True,
                timeout=timeout,
                creationflags=self._creation_flags(),
            )
            out = self._clean_command_output(self._safe_decode(proc.stdout) + self._safe_decode(proc.stderr))
            return proc.returncode, out.strip()

        def _rewrite_compose_to_stable():
            compose_path = f"{deploy_dir}/docker-compose.yml"
            self._emit_pull_progress("stage", "写回正式版镜像配置")
            compose_content = self._wsl_exec(distro, f"cat {compose_path}")
            if STABLE_IMAGE in compose_content and PREVIEW_IMAGE not in compose_content:
                return True
            if PREVIEW_IMAGE not in compose_content:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "未找到预览版镜像引用，无法恢复正式版。")
                return False
            updated_content = compose_content.replace(PREVIEW_IMAGE, STABLE_IMAGE)
            self._write_to_wsl(distro, updated_content, compose_path)
            return True

        def _do_restore():
            self._stop_event.clear()
            self.status_changed.emit("更新中...")
            self.log_received.emit("[恢复正式版] 开始从预览版备份恢复正式版", "info")

            if self.config and not self.config.get("preview_backup_available"):
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "当前预览版是在未备份的情况下切换的，无法恢复到正式版。")
                return

            if not self.preview_backup_exists():
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, f"未找到备份文件：{PREVIEW_BACKUP_ARCHIVE_PATH}")
                return

            try:
                self._emit_pull_progress("stage", "停止相关服务")
                rc, out = _exec(
                    "docker compose -f docker-compose.yml stop nekro_agent nekro_postgres nekro_qdrant nekro_napcat 2>/dev/null || true",
                    timeout=120,
                )
                if out:
                    self.log_received.emit(out, "info")

                self._emit_pull_progress("stage", "恢复备份数据")
                rc, out = _exec(f"tar -xzf {shlex.quote(PREVIEW_BACKUP_ARCHIVE_PATH)} -C /", timeout=600)
                if rc != 0:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"恢复备份失败\n{out}" if out else "恢复备份失败")
                    return

                self.log_received.emit("[恢复正式版] 拉取正式版镜像", "info")
                self._emit_pull_progress("stage", "拉取正式版镜像")
                if not self._pull_images(distro, [STABLE_IMAGE]):
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"镜像拉取失败: {STABLE_IMAGE}")
                    return

                if not _rewrite_compose_to_stable():
                    return

                self._emit_pull_progress("stage", "重建并启动服务")
                rc, out = _exec("docker compose -f docker-compose.yml --env-file .env up -d", timeout=300)
                if rc != 0:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(False, f"恢复正式版失败\n{out}" if out else "恢复正式版失败")
                    return
                if out:
                    self.log_received.emit(out, "info")

                self._emit_pull_progress("done", "正式版恢复完成")
                self.is_running = True
                if self.config:
                    self.config.set("release_channel", "stable")
                    self.config.set("preview_backup_available", False)
                if self._log_process is None or self._log_process.poll() is not None:
                    threading.Thread(target=self._log_reader, args=(distro, deploy_dir), daemon=True).start()
                threading.Thread(target=self._health_check, daemon=True).start()
                self.update_finished.emit(True, "正式版恢复完成，正在等待服务重新就绪。")
            except subprocess.TimeoutExpired:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "恢复正式版超时。")
            except Exception as e:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, f"恢复正式版异常: {e}")

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
        rc = subprocess.run(
            ["wsl", "-d", distro, "--", "bash", "-c", cmd],
            capture_output=True,
            timeout=600,
            creationflags=self._creation_flags(),
        )
        out = self._safe_decode(rc.stdout) + self._safe_decode(rc.stderr)
        if rc.returncode != 0:
            message = "备份失败"
            cleaned = out.strip()
            if cleaned:
                message += f"\n{cleaned}"
            return False, message
        return True, f"已生成备份归档：{archive_path}"
