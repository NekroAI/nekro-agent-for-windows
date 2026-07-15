import posixpath
import re
import shlex
import threading
import time
import urllib.error
import urllib.request
import json
from datetime import datetime, timezone

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

        # NA_BACKUP_TARGETS 是默认实例的历史路径；命名实例的归档若包含
        # 这些路径，恢复时会把默认实例的数据一并覆盖，必须排除。
        if not instance_name:
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

    def _run_exclusive_ui_operation(self, name, worker):
        """在互斥槽保护下执行 UI 触发的更新类操作（工作线程内调用）。

        与 WebUI daemon 任务共用同一互斥槽，避免两侧并发执行 compose 操作。
        """
        if not self.acquire_exclusive_operation(name):
            other = self.exclusive_operation_name() or "其他更新操作"
            self.status_changed.emit("更新失败")
            self.update_finished.emit(
                False,
                f"已有互斥操作正在执行（{other}），请等待完成后重试。",
            )
            return
        try:
            worker()
        finally:
            self.release_exclusive_operation()

    def run_remote_update(self):
        """执行内置升级流程，optional 步骤通过信号询问用户确认。"""
        from core.update_runner import build_update_plan, log_update_plan

        distro = DISTRO_NAME
        # UI 发起操作时就固定目标上下文。互斥工作线程可能稍后才真正运行，
        # 期间 active instance 不应改变本次更新的目录、端口或镜像渠道。
        inst_id = self.config.get_active_instance_id() if self.config else ""
        inst = self.config.get_instance(inst_id) if self.config and inst_id else None
        inst = inst or {}
        deploy_dir, data_dir, instance_name = self._get_active_deploy_paths()
        nekro_port = normalize_port(
            inst.get("nekro_port")
            or (self.config.get("nekro_port") if self.config else None),
            8021,
        )
        release_channel = str(
            inst.get("release_channel")
            or (self.config.get("release_channel") if self.config else "stable")
            or "stable"
        )
        agent_image = self.get_agent_image_ref(release_channel=release_channel)

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
                        backup_size = self._get_backup_size_hint_for_paths(
                            distro, deploy_dir, data_dir, instance_name
                        )
                        prompt = step.get("optional_prompt", f"是否执行：{label}？").replace("{backup_size}", backup_size)
                        reply = _wait_optional_reply(label, prompt)
                        if reply is None:
                            self.log_received.emit(f"[更新] 可选步骤确认超时，已跳过: {label}", "warning")
                            continue
                        if not reply:
                            self.log_received.emit(f"[更新] 已跳过可选步骤: {label}", "info")
                            continue

                    ok, backup_message = self._backup_nekro_archive_for_paths(
                        distro,
                        step.get("archive_path", UPDATE_BACKUP_ARCHIVE_PATH),
                        deploy_dir,
                        data_dir,
                        instance_name,
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
            inst_display = inst_id if inst_id and inst_id != "default" else ""
            log_prefix = f"[{inst_display}] " if inst_display else ""
            if self._log_process is None or self._log_process.poll() is not None:
                threading.Thread(
                    target=self._log_reader,
                    args=(distro, deploy_dir, log_prefix, inst_id),
                    daemon=True,
                ).start()
            threading.Thread(target=self._health_check, args=(nekro_port,), daemon=True).start()
            self.update_finished.emit(True, "Nekro Agent 更新完成，正在等待服务重新就绪。")

        threading.Thread(
            target=lambda: self._run_exclusive_ui_operation("远程更新", _do_update),
            daemon=True,
        ).start()

    def _daemon_context(self, request: dict):
        inst_id = str(request.get("_launcher_inst_id") or "")
        inst = self.config.get_instance(inst_id) if self.config and inst_id else None
        inst = inst or {}
        return {
            "inst_id": inst_id,
            "deploy_dir": str(request.get("_deploy_dir") or inst.get("deploy_dir") or "/root/nekro_agent"),
            "data_dir": str(request.get("_data_dir") or inst.get("data_dir") or "/root/nekro_agent_data"),
            "instance_name": str(request.get("_instance_name") or inst.get("instance_name") or ""),
            "nekro_port": normalize_port(request.get("_nekro_port") or inst.get("nekro_port"), 8021),
            "channel": str(inst.get("release_channel") or request.get("_current_channel") or "stable"),
        }

    def _daemon_instance_slug(self, request: dict):
        raw = str(request.get("instance_id") or "instance")
        if ":" in raw:
            raw = raw.split(":", 1)[1]
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", raw).strip("._-")
        return (slug or "instance")[:80]

    def _daemon_backup_dir(self, request: dict):
        return f"/root/.na-tools/backups/{self._daemon_instance_slug(request)}"

    def _daemon_backup_name_from_filename(self, filename):
        match = re.match(
            r"^nekro_agent_backup_(?P<name>.+)_(?P<stamp>\d{8}_\d{6})\.tar\.gz$",
            filename,
        )
        if match:
            return match.group("name")
        if filename.endswith("_preview_backup.tar.gz") or filename == "na_preview_backup.tar.gz":
            return "pre-preview"
        if filename == "na_update_backup.tar.gz":
            return "stable-update"
        return "manual"

    def _daemon_backup_summary(self, path, name=None):
        filename = posixpath.basename(path)
        stat = self._wsl_exec(
            DISTRO_NAME,
            f"test -f {shlex.quote(path)} && stat -c '%s\t%Y' {shlex.quote(path)}",
            timeout=15,
        ).strip()
        size_bytes = 0
        created_at = datetime.now(timezone.utc).isoformat()
        if stat:
            parts = stat.split("\t", 1)
            if parts:
                try:
                    size_bytes = int(float(parts[0]))
                except ValueError:
                    size_bytes = 0
            if len(parts) == 2:
                try:
                    created_at = datetime.fromtimestamp(float(parts[1]), timezone.utc).isoformat()
                except ValueError:
                    pass
        return {
            "backup_id": filename,
            "filename": filename,
            "name": name or self._daemon_backup_name_from_filename(filename),
            "created_at": created_at,
            "size_bytes": size_bytes,
        }

    def _daemon_make_backup_path(self, request: dict, name: str):
        backup_dir = self._daemon_backup_dir(request)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name).strip("._-") or "manual"
        filename = f"nekro_agent_backup_{safe_name}_{stamp}.tar.gz"
        self._run_wsl_checked(
            DISTRO_NAME,
            f"mkdir -p {shlex.quote(backup_dir)}",
            action="[daemon 备份] 创建备份目录失败",
            timeout=30,
        )
        return f"{backup_dir}/{filename}"

    def _daemon_resolve_backup_path(self, request: dict, backup_id: str):
        if "/" in backup_id or "\\" in backup_id or ".." in backup_id:
            return ""
        backup_dir = self._daemon_backup_dir(request)
        candidate = f"{backup_dir}/{backup_id}"
        if self._wsl_exec(
            DISTRO_NAME,
            f"test -f {shlex.quote(candidate)} && echo yes",
            timeout=15,
        ).strip() == "yes":
            return candidate
        return ""

    def _daemon_latest_backup_path(self, request: dict, name: str):
        backups = self.list_daemon_backups(request, name=name, limit=1)
        if not backups:
            return ""
        return self._daemon_resolve_backup_path(request, backups[0]["backup_id"])

    def list_daemon_backups(self, request: dict, name="", limit=50):
        backup_dir = self._daemon_backup_dir(request)
        cmd = (
            f"mkdir -p {shlex.quote(backup_dir)} && "
            f"find {shlex.quote(backup_dir)} -maxdepth 1 -type f -name '*.tar.gz' "
            "-printf '%f\t%s\t%T@\\n' 2>/dev/null"
        )
        raw = self._wsl_exec(DISTRO_NAME, cmd, timeout=30)
        items = []
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            filename, size_raw, mtime_raw = parts
            item_name = self._daemon_backup_name_from_filename(filename)
            if name and item_name != name:
                continue
            try:
                size_bytes = int(size_raw)
                mtime = float(mtime_raw)
            except ValueError:
                continue
            items.append(
                {
                    "backup_id": filename,
                    "filename": filename,
                    "name": item_name,
                    "created_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                    "size_bytes": size_bytes,
                    "_mtime": mtime,
                }
            )

        items.sort(key=lambda item: item.get("_mtime", 0.0), reverse=True)
        result = []
        for item in items[:limit]:
            item = dict(item)
            item.pop("_mtime", None)
            result.append(item)
        return result

    def _backup_target_candidates_for_paths(self, deploy_dir, data_dir, instance_name):
        volume_prefix = instance_name
        targets = [
            f"/var/lib/docker/volumes/{volume_prefix}nekro_postgres_data",
            f"/var/lib/docker/volumes/{volume_prefix}nekro_qdrant_data",
            data_dir,
            deploy_dir,
        ]
        # 同 _backup_target_candidates：默认实例的历史路径不得混入命名实例的
        # 备份归档，否则恢复（tar -xzf -C /）会覆盖默认实例的在线数据。
        if not instance_name:
            for target in NA_BACKUP_TARGETS:
                if target not in targets:
                    targets.append(target)
        return targets

    def _existing_backup_targets_for_paths(self, distro, deploy_dir, data_dir, instance_name):
        existing = []
        for target in self._backup_target_candidates_for_paths(deploy_dir, data_dir, instance_name):
            if self._wsl_exec(distro, f"test -d {shlex.quote(target)} && echo yes").strip() == "yes":
                existing.append(target)
        return existing

    def _get_backup_size_hint_for_paths(self, distro, deploy_dir, data_dir, instance_name):
        existing_targets = self._existing_backup_targets_for_paths(
            distro, deploy_dir, data_dir, instance_name
        )
        if not existing_targets:
            return "未知"
        cmd = (
            "du -shc "
            + " ".join(shlex.quote(target) for target in existing_targets)
            + " | tail -n1 | cut -f1"
        )
        size = self._wsl_exec(distro, cmd, timeout=60).strip()
        return size or "未知"

    def _compose_running_services(self, distro, deploy_dir, *, action):
        proc = self._run_wsl_checked(
            distro,
            "docker compose -f docker-compose.yml --env-file .env "
            "ps --status running --services",
            action=action,
            cwd=deploy_dir,
            timeout=30,
        )
        output = self._clean_command_output(self._safe_decode(proc.stdout))
        services = []
        for line in output.splitlines():
            service = line.strip()
            if not service:
                continue
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service):
                raise RuntimeError(f"{action}：Compose 返回了无效服务名: {service}")
            if service not in services:
                services.append(service)
        return services

    def _stop_running_compose_services(self, distro, deploy_dir, *, action):
        services = self._compose_running_services(
            distro,
            deploy_dir,
            action=f"{action}（读取运行服务失败）",
        )
        if not services:
            return []
        service_args = " ".join(shlex.quote(service) for service in services)
        try:
            self._run_wsl_checked(
                distro,
                "docker compose -f docker-compose.yml --env-file .env "
                f"stop {service_args}",
                action=action,
                cwd=deploy_dir,
                timeout=120,
            )
        except Exception as stop_error:
            # Compose 可能已经停止了部分服务才返回非零。无论失败发生在哪个
            # 服务，都按停服前快照恢复完整集合，避免操作失败后留下半停机状态。
            try:
                self._start_compose_services(
                    distro,
                    deploy_dir,
                    services,
                    action=f"{action}后恢复原运行服务失败",
                )
            except Exception as restart_error:
                raise RuntimeError(
                    f"{stop_error}\n停止服务失败后恢复原运行状态也失败:\n{restart_error}"
                ) from restart_error
            raise
        return services

    def _start_compose_services(self, distro, deploy_dir, services, *, action):
        if not services:
            return
        service_args = " ".join(shlex.quote(service) for service in services)
        self._run_wsl_checked(
            distro,
            "docker compose -f docker-compose.yml --env-file .env "
            f"start {service_args}",
            action=action,
            cwd=deploy_dir,
            timeout=120,
        )

    def _sync_compose_running_state(self, distro, deploy_dir, *, action):
        """按指定实例的 Compose 状态同步 is_running，不依赖当前 active 实例。"""
        try:
            services = self._compose_running_services(
                distro,
                deploy_dir,
                action=action,
            )
        except Exception as e:
            self.log_received.emit(f"{action}\n{e}", "debug")
            return
        self.is_running = bool(services)

    def _backup_nekro_archive_for_paths(self, distro, archive_path, deploy_dir, data_dir, instance_name):
        existing_targets = self._existing_backup_targets_for_paths(distro, deploy_dir, data_dir, instance_name)
        if not existing_targets:
            return False, "未找到可备份的目录。"

        archive_dir = posixpath.dirname(archive_path.rstrip("/")) or "/root"
        target_args = " ".join(shlex.quote(target.lstrip("/")) for target in existing_targets)
        running_services = []
        try:
            running_services = self._stop_running_compose_services(
                distro,
                deploy_dir,
                action="创建备份前停止相关服务失败",
            )
            self._run_wsl_checked(
                distro,
                f"mkdir -p {shlex.quote(archive_dir)} && "
                f"rm -f {shlex.quote(archive_path)} && "
                f"tar -czf {shlex.quote(archive_path)} -C / {target_args}",
                action="创建备份归档失败",
                timeout=600,
            )
        except Exception as e:
            safe_error = self._daemon_redact_text(
                str(e),
                sensitive_paths=[archive_path, *existing_targets],
            )
            result = False, (
                "备份失败\n"
                f"{safe_error}"
            )
        else:
            result = True, f"已生成备份归档：{posixpath.basename(archive_path)}"
        finally:
            if running_services:
                try:
                    self._start_compose_services(
                        distro,
                        deploy_dir,
                        running_services,
                        action="备份后恢复原运行服务失败",
                    )
                except Exception as e:
                    safe_error = self._daemon_redact_text(
                        str(e), sensitive_paths=[archive_path, *existing_targets]
                    )
                    result = False, f"备份后恢复原运行服务失败\n{safe_error}"
        return result

    def _daemon_redact_text(self, text, *, ctx=None, sensitive_paths=None):
        result = self._redact_for_log(text)
        replacements = []
        for path in sensitive_paths or []:
            if path:
                replacements.append((str(path), "<path>"))
        if ctx:
            for key in ("deploy_dir", "data_dir"):
                path = str(ctx.get(key) or "")
                if path:
                    replacements.append((path, f"<{key}>"))
            port = normalize_port(ctx.get("nekro_port"), 8021)
            replacements.append((f"http://127.0.0.1:{port}/api/health", "<health_url>"))

        for needle, replacement in sorted(set(replacements), key=lambda item: len(item[0]), reverse=True):
            result = result.replace(needle, replacement)
        result = re.sub(
            r"/root/\.na-tools/backups/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.tar\.gz",
            "<backup_file>",
            result,
        )
        result = re.sub(
            r"/root/[A-Za-z0-9_.-]*(?:preview|update)_backup\.tar\.gz",
            "<backup_file>",
            result,
        )
        result = re.sub(
            r"(NA_TOOLS_DAEMON_TOKEN_FILE\s*[:=]\s*)([^\s,\n\"']+)",
            r"\1<redacted>",
            result,
            flags=re.IGNORECASE,
        )
        return result

    def _daemon_redact_details(self, details, *, ctx=None, sensitive_paths=None):
        if isinstance(details, str):
            return self._daemon_redact_text(details, ctx=ctx, sensitive_paths=sensitive_paths)
        if isinstance(details, dict):
            return {
                key: self._daemon_redact_details(value, ctx=ctx, sensitive_paths=sensitive_paths)
                for key, value in details.items()
            }
        if isinstance(details, list):
            return [
                self._daemon_redact_details(value, ctx=ctx, sensitive_paths=sensitive_paths)
                for value in details
            ]
        return details

    def _daemon_add_log(self, job, line, level="info", stream="system", *, ctx=None, sensitive_paths=None):
        job.add_log(
            self._daemon_redact_text(line, ctx=ctx, sensitive_paths=sensitive_paths),
            level=level,
            stream=stream,
        )

    def _daemon_fail(self, job, code, message, details=None, *, ctx=None, sensitive_paths=None):
        job.fail(
            code,
            self._daemon_redact_text(message, ctx=ctx, sensitive_paths=sensitive_paths),
            self._daemon_redact_details(details or {}, ctx=ctx, sensitive_paths=sensitive_paths),
        )

    def _daemon_safe_restore_targets(self, ctx):
        candidates = self._backup_target_candidates_for_paths(
            ctx["deploy_dir"],
            ctx["data_dir"],
            ctx["instance_name"],
        )
        targets = []
        allowed_prefixes = ("/root/", "/home/", "/opt/", "/srv/", "/var/lib/docker/volumes/")
        for target in candidates:
            normalized = posixpath.normpath("/" + str(target or "").lstrip("/"))
            lower = normalized.lower()
            if (
                normalized
                and normalized != "/"
                and normalized.startswith(allowed_prefixes)
                and "nekro" in lower
                and normalized not in targets
            ):
                targets.append(normalized)
        return targets

    def _daemon_restore_targets_in_archive(self, ctx, archive_path):
        proc = self._run_wsl_checked(
            DISTRO_NAME,
            f"tar -tzf {shlex.quote(archive_path)}",
            action="校验备份归档失败",
            timeout=600,
            user="root",
        )
        raw = self._clean_command_output(self._safe_decode(proc.stdout))
        archive_entries = []
        for line in raw.splitlines():
            raw_entry = line.strip()
            while raw_entry.startswith("./"):
                raw_entry = raw_entry[2:]
            if not raw_entry or raw_entry.startswith("/"):
                continue
            entry = posixpath.normpath(raw_entry)
            if entry == ".." or entry.startswith("../"):
                continue
            if entry and entry != ".":
                archive_entries.append(entry)
        if not archive_entries:
            return []

        targets = []
        for target in self._daemon_safe_restore_targets(ctx):
            rel_target = target.lstrip("/")
            if any(entry == rel_target or entry.startswith(rel_target.rstrip("/") + "/") for entry in archive_entries):
                targets.append(target)
        return targets

    def _daemon_cleanup_restore_targets(self, ctx, job, targets):
        safe_targets = set(self._daemon_safe_restore_targets(ctx))
        for target in targets:
            normalized = posixpath.normpath("/" + str(target or "").lstrip("/"))
            if normalized not in safe_targets:
                raise RuntimeError("拒绝清理不在当前实例白名单中的恢复目标。")
            cleanup_cmd = (
                f"mkdir -p -- {shlex.quote(normalized)} && "
                f"find {shlex.quote(normalized)} -mindepth 1 -maxdepth 1 "
                "-exec rm -rf -- {} +"
            )
            self._run_wsl_checked(
                DISTRO_NAME,
                cleanup_cmd,
                action="[daemon 还原] 清理恢复目标失败",
                timeout=300,
                user="root",
            )
            if job is not None:
                self._daemon_add_log(
                    job,
                    "已清理一个恢复目标目录",
                    ctx=ctx,
                    sensitive_paths=[normalized],
                )

    def _daemon_restore_archive(self, ctx, job, archive_path, *, action, targets=None):
        # 修复前生成的混合归档可能包含其它实例（尤其默认实例）的路径，
        # 一律按当前实例的白名单成员显式解包，绝不整包解压到 /。
        if targets is None:
            targets = self._daemon_restore_targets_in_archive(ctx, archive_path)
        else:
            safe_targets = set(self._daemon_safe_restore_targets(ctx))
            targets = [target for target in targets if target in safe_targets]
        if not targets:
            raise RuntimeError(
                "备份归档中未找到当前实例的可恢复目录（归档损坏或不属于该实例），已拒绝解包。"
            )
        member_args = " ".join(shlex.quote(target.lstrip("/")) for target in targets)
        cmd = f"tar -xzf {shlex.quote(archive_path)} -C / {member_args}"
        try:
            self._daemon_cleanup_restore_targets(ctx, job, targets)
            self._daemon_exec(ctx, job, cmd, timeout=600, action=action)
            return
        except Exception as first_error:
            self._daemon_add_log(
                job,
                "清理或恢复失败，重试清理目标目录后再次恢复",
                level="warning",
                ctx=ctx,
                sensitive_paths=[archive_path],
            )
            try:
                self._daemon_cleanup_restore_targets(ctx, job, targets)
                self._daemon_exec(ctx, job, cmd, timeout=600, action=action)
            except Exception as second_error:
                sensitive_paths = [archive_path, *targets]
                first = self._daemon_redact_text(str(first_error), ctx=ctx, sensitive_paths=sensitive_paths)
                second = self._daemon_redact_text(str(second_error), ctx=ctx, sensitive_paths=sensitive_paths)
                raise RuntimeError(
                    f"{first}\n已清理部分恢复数据并重试，但仍失败:\n{second}"
                ) from second_error

    def _daemon_validate_instance(self, ctx, job):
        checks = [
            (
                f"test -d {shlex.quote(ctx['data_dir'])}",
                "instance_not_bound",
                f"实例数据目录不存在: {ctx['data_dir']}",
            ),
            (
                "test -f docker-compose.yml",
                "compose_missing",
                f"Compose 文件不存在: {ctx['deploy_dir']}/docker-compose.yml",
            ),
            (
                "test -f .env",
                "env_missing",
                f"env 文件不存在: {ctx['deploy_dir']}/.env",
            ),
        ]
        for cmd, code, message in checks:
            try:
                self._run_wsl_checked(
                    DISTRO_NAME,
                    cmd,
                    action=f"[daemon 更新] {message}",
                    cwd=ctx["deploy_dir"] if "docker-compose" in cmd or ".env" in cmd else None,
                    timeout=30,
                )
            except Exception as e:
                self._daemon_fail(job, code, str(e), ctx=ctx)
                return False
        try:
            self._run_wsl_checked(
                DISTRO_NAME,
                "systemctl start docker && docker version >/dev/null && docker compose version >/dev/null",
                action="[daemon 更新] Docker 或 Compose 不可用",
                timeout=60,
            )
        except Exception as e:
            self._daemon_fail(job, "docker_unavailable", str(e), ctx=ctx)
            return False
        return True

    def _daemon_exec(self, ctx, job, cmd, *, timeout=300, action="[daemon 更新] 命令执行失败"):
        proc = self._run_wsl_checked(
            DISTRO_NAME,
            cmd,
            action=action,
            cwd=ctx["deploy_dir"],
            timeout=timeout,
        )
        out = self._clean_command_output(
            self._safe_decode(proc.stdout) + self._safe_decode(proc.stderr)
        ).strip()
        if out:
            self._daemon_add_log(job, out, stream="stdout", ctx=ctx)
        return proc.returncode, out

    def _rewrite_daemon_compose_channel(self, ctx, channel):
        compose_path = f"{ctx['deploy_dir']}/docker-compose.yml"
        compose_content = self._wsl_exec_checked(
            DISTRO_NAME,
            f"cat {shlex.quote(compose_path)}",
            timeout=30,
        )
        if channel == "preview":
            if PREVIEW_IMAGE in compose_content:
                return
            if PREVIEW_COMPOSE_IMAGE not in compose_content:
                raise RuntimeError("未找到稳定版镜像引用，无法切换到预览版。")
            updated_content = compose_content.replace(PREVIEW_COMPOSE_IMAGE, PREVIEW_IMAGE)
        else:
            if STABLE_IMAGE in compose_content and PREVIEW_IMAGE not in compose_content:
                return
            if PREVIEW_IMAGE not in compose_content:
                raise RuntimeError("未找到预览版镜像引用，无法切回稳定版。")
            updated_content = compose_content.replace(PREVIEW_IMAGE, STABLE_IMAGE)
        self._write_to_wsl(DISTRO_NAME, updated_content, compose_path)

    def _daemon_mark_instance_channel(self, ctx, channel, preview_backup_available=False):
        if not self.config:
            return
        updates = {
            "release_channel": channel,
            "preview_backup_available": bool(preview_backup_available),
        }
        if ctx["inst_id"]:
            self.config.update_instance_with_globals(
                ctx["inst_id"],
                instance_updates=updates,
                global_updates=updates,
            )
        else:
            self.config.set_many(updates)

    def _daemon_preview_backup_available(self, ctx):
        if not self.config:
            return False
        if ctx["inst_id"]:
            inst = self.config.get_instance(ctx["inst_id"]) or {}
            if "preview_backup_available" in inst:
                return bool(inst.get("preview_backup_available"))
        return bool(self.config.get("preview_backup_available", False))

    def _daemon_attach_logs(self, ctx):
        inst_id = ctx["inst_id"]
        inst_display = inst_id if inst_id and inst_id != "default" else ""
        log_prefix = f"[{inst_display}] " if inst_display else ""
        if self._log_process is None or self._log_process.poll() is not None:
            threading.Thread(
                target=self._log_reader,
                args=(DISTRO_NAME, ctx["deploy_dir"], log_prefix, inst_id),
                daemon=True,
            ).start()

    def _daemon_cancelled(self, job):
        if job.is_cancel_requested():
            job.cancel("任务已在安全阶段边界取消")
            return True
        return False

    def _daemon_emit_cancelled_status(self):
        """任务取消后按当前运行状态复位状态栏，避免停留在「更新中...」。"""
        self.status_changed.emit("运行中" if self.is_running else "已停止")

    def run_daemon_backup_job(self, request: dict, job):
        """执行 daemon facade 的手动备份任务。"""
        ctx = self._daemon_context(request)
        self._stop_event.clear()
        if not job.start("validate_instance", "正在校验实例与 Docker 环境"):
            return
        if not self._daemon_validate_instance(ctx, job):
            return
        if self._daemon_cancelled(job):
            return
        name = str(request.get("name") or "manual")
        archive_path = self._daemon_make_backup_path(request, name)
        job.set_progress("backup", 1, 2, "正在创建备份归档")
        ok, backup_message = self._backup_nekro_archive_for_paths(
            DISTRO_NAME,
            archive_path,
            ctx["deploy_dir"],
            ctx["data_dir"],
            ctx["instance_name"],
        )
        if not ok:
            self._daemon_fail(job, "backup_failed", backup_message, ctx=ctx, sensitive_paths=[archive_path])
            return
        summary = self._daemon_backup_summary(archive_path, name=name)
        self._daemon_add_log(job, backup_message, ctx=ctx, sensitive_paths=[archive_path])
        job.succeed("备份完成", {"backup": summary})

    def run_daemon_restore_job(self, request: dict, job):
        """执行 daemon facade 的手动还原任务。"""
        ctx = self._daemon_context(request)
        backup_id = str(request.get("backup_id") or "")
        self._stop_event.clear()
        if not job.start("validate_instance", "正在校验实例与 Docker 环境"):
            return
        self.status_changed.emit("更新中...")
        if not self._daemon_validate_instance(ctx, job):
            self.status_changed.emit("更新失败")
            return
        archive_path = self._daemon_resolve_backup_path(request, backup_id)
        if not archive_path:
            self.status_changed.emit("更新失败")
            self._daemon_fail(job, "backup_not_found", f"未找到备份: {backup_id}", ctx=ctx)
            return
        if self._daemon_cancelled(job):
            self._daemon_emit_cancelled_status()
            return
        job.set_progress("restore", 1, 4, "正在停止服务并恢复备份")
        restore_completed = False
        try:
            restore_targets = self._daemon_restore_targets_in_archive(ctx, archive_path)
            if not restore_targets:
                raise RuntimeError(
                    "备份归档中未找到当前实例的可恢复目录（归档损坏或不属于该实例），已中止恢复。"
                )
            self._stop_running_compose_services(
                DISTRO_NAME,
                ctx["deploy_dir"],
                action="[daemon 还原] 停止服务失败",
            )
            self.is_running = False
            self._daemon_restore_archive(
                ctx,
                job,
                action="[daemon 还原] 恢复备份数据失败",
                archive_path=archive_path,
                targets=restore_targets,
            )
            job.set_progress("restart_services", 2, 4, "正在重建并启动服务")
            self._daemon_exec(
                ctx,
                job,
                "docker compose -f docker-compose.yml --env-file .env up -d",
                timeout=300,
                action="[daemon 还原] 重建并启动服务失败",
            )
            job.set_progress("verify", 3, 4, "等待服务健康检查通过")
            health = self._wait_daemon_update_health(job, ctx["nekro_port"])
            if health is None:
                self._sync_compose_running_state(
                    DISTRO_NAME,
                    ctx["deploy_dir"],
                    action="[daemon 还原] 刷新服务运行状态失败",
                )
                self._daemon_emit_cancelled_status()
                return
            if not health:
                self.status_changed.emit("更新失败")
                self._daemon_fail(job, "verify_timeout", "服务健康检查超时", ctx=ctx)
                return
            self.is_running = True
            self._daemon_attach_logs(ctx)
            self.status_changed.emit("运行中")
            job.succeed(
                "备份还原完成",
                {"backup": self._daemon_backup_summary(archive_path), "app_health": "ok"},
            )
            restore_completed = True
        except Exception as e:
            self.status_changed.emit("更新失败")
            self._daemon_fail(job, "launcher_update_failed", str(e), ctx=ctx, sensitive_paths=[archive_path])
        finally:
            if not restore_completed:
                self._sync_compose_running_state(
                    DISTRO_NAME,
                    ctx["deploy_dir"],
                    action="[daemon 还原] 刷新服务运行状态失败",
                )

    def run_daemon_update_job(self, request: dict, job):
        """执行 daemon facade 的非交互更新任务。"""
        ctx = self._daemon_context(request)
        channel = request.get("channel") or "stable"
        if channel not in {"stable", "preview", "rollback"}:
            self._daemon_fail(job, "invalid_channel", "channel 必须是 stable、preview 或 rollback")
            return

        update_sandbox = bool(request.get("update_sandbox", True))
        update_cc_sandbox = bool(request.get("update_cc_sandbox", False))
        restore_pre_preview = bool(request.get("restore_pre_preview", False))
        backup_requested = bool(request.get("backup", True))
        target_channel = "preview" if channel == "preview" else "stable"
        agent_image = self.get_agent_image_ref(release_channel=target_channel)
        already_preview = ctx["channel"] == "preview"
        steps = []

        if channel == "stable":
            if ctx["channel"] == "preview":
                steps.append({"type": "switch_channel", "channel": "stable", "label": "写回稳定版镜像配置"})
            if backup_requested:
                steps.append({"type": "backup", "name": "stable-update", "label": "创建 stable 更新前备份"})
            steps.extend(
                [
                    {"type": "pull", "image": agent_image, "label": "拉取最新 Nekro Agent 镜像"},
                    {"type": "compose_up", "services": ["nekro_agent"], "label": "重建 Nekro Agent 容器"},
                ]
            )
        elif channel == "preview":
            if not already_preview:
                steps.append({"type": "backup", "name": "pre-preview", "label": "创建预览版切换前备份"})
            steps.extend(
                [
                    {"type": "switch_channel", "channel": "preview", "label": "写入预览版镜像配置"},
                    {"type": "pull", "image": agent_image, "label": "拉取预览版 Nekro Agent 镜像"},
                    {"type": "compose_up", "services": ["nekro_agent"], "label": "重建 Nekro Agent 容器"},
                ]
            )
        else:
            if restore_pre_preview:
                steps.append({"type": "restore_pre_preview", "label": "恢复预览版切换前备份"})
            else:
                steps.append({"type": "switch_channel", "channel": "stable", "label": "写回稳定版镜像配置"})
                steps.append({"type": "pull", "image": agent_image, "label": "拉取稳定版 Nekro Agent 镜像"})
            steps.append(
                {
                    "type": "compose_up",
                    "services": [] if restore_pre_preview else ["nekro_agent"],
                    "all_services": restore_pre_preview,
                    "label": "重建并启动服务" if restore_pre_preview else "重建 Nekro Agent 容器",
                }
            )

        if update_sandbox and channel != "rollback":
            steps.append(
                {
                    "type": "pull",
                    "label": "拉取 Nekro Agent 沙盒镜像",
                    "image": "kromiose/nekro-agent-sandbox",
                    "phase": "pull_sandbox",
                }
            )
        if update_cc_sandbox and channel != "rollback":
            steps.append(
                {
                    "type": "pull",
                    "label": "拉取 Claude Code 沙盒镜像",
                    "image": CC_SANDBOX_IMAGE,
                    "phase": "pull_sandbox",
                }
            )
        steps.append({"type": "verify", "label": "等待服务健康检查通过"})

        self._stop_event.clear()
        if not job.start("validate_instance", "正在校验实例与 Docker 环境"):
            return
        self.status_changed.emit("更新中...")

        if not self._daemon_validate_instance(ctx, job):
            self.status_changed.emit("更新失败")
            return

        total = len(steps) + 1
        backup_summary = None
        restore_flow_active = False
        for index, step in enumerate(steps, start=2):
            if self._daemon_cancelled(job):
                if restore_flow_active:
                    self._sync_compose_running_state(
                        DISTRO_NAME,
                        ctx["deploy_dir"],
                        action="[daemon 更新] 刷新服务运行状态失败",
                    )
                self._daemon_emit_cancelled_status()
                return
            step_type = str(step.get("type") or "")
            label = str(step.get("label") or "")
            phase = str(step.get("phase") or "") or {
                "backup": "backup",
                "switch_channel": "switch_channel",
                "pull": "pull_images",
                "compose_up": "restart_services",
                "restore_pre_preview": "backup",
                "verify": "verify",
            }.get(step_type, "validate_instance")
            job.set_progress(phase, index, total, label)
            self.log_received.emit(f"[daemon 更新] {label}", "info")
            self._emit_pull_progress("stage", label)

            try:
                if step_type == "backup":
                    archive_path = self._daemon_make_backup_path(request, str(step.get("name") or "manual"))
                    ok, backup_message = self._backup_nekro_archive_for_paths(
                        DISTRO_NAME,
                        archive_path,
                        ctx["deploy_dir"],
                        ctx["data_dir"],
                        ctx["instance_name"],
                    )
                    if not ok:
                        self.status_changed.emit("更新失败")
                        self._daemon_fail(job, "backup_failed", backup_message, ctx=ctx)
                        return
                    backup_summary = self._daemon_backup_summary(
                        archive_path,
                        name=str(step.get("name") or "manual"),
                    )
                    self._daemon_add_log(job, backup_message, ctx=ctx, sensitive_paths=[archive_path])
                    continue

                if step_type == "switch_channel":
                    self._rewrite_daemon_compose_channel(ctx, str(step.get("channel") or "stable"))
                    continue

                if step_type == "restore_pre_preview":
                    archive_path = self._daemon_latest_backup_path(request, "pre-preview")
                    if not archive_path:
                        self.status_changed.emit("更新失败")
                        self._daemon_fail(job, "backup_not_found", "未找到 pre-preview 备份", ctx=ctx)
                        return
                    restore_flow_active = True
                    restore_targets = self._daemon_restore_targets_in_archive(ctx, archive_path)
                    if not restore_targets:
                        raise RuntimeError(
                            "pre-preview 备份中未找到当前实例的可恢复目录"
                            "（归档损坏或不属于该实例），已中止恢复。"
                        )
                    self._stop_running_compose_services(
                        DISTRO_NAME,
                        ctx["deploy_dir"],
                        action="[daemon 更新] 停止服务失败",
                    )
                    self.is_running = False
                    self._daemon_restore_archive(
                        ctx,
                        job,
                        action="[daemon 更新] 恢复 pre-preview 备份失败",
                        archive_path=archive_path,
                        targets=restore_targets,
                    )
                    self._rewrite_daemon_compose_channel(ctx, "stable")
                    backup_summary = self._daemon_backup_summary(archive_path, name="pre-preview")
                    continue

                if step_type == "pull":
                    image = str(step.get("image") or "")
                    if not self._pull_images(DISTRO_NAME, [image]):
                        code = "sandbox_pull_failed" if phase == "pull_sandbox" else "pull_failed"
                        self.status_changed.emit("更新失败")
                        self._daemon_fail(job, code, f"镜像拉取失败: {image}", {"image": image}, ctx=ctx)
                        return
                    continue

                if step_type == "compose_up":
                    if step.get("all_services"):
                        cmd = "docker compose -f docker-compose.yml --env-file .env up -d"
                    else:
                        services = step.get("services", [])
                        service_args = " ".join(shlex.quote(service) for service in services)
                        cmd = (
                            "docker compose -f docker-compose.yml --env-file .env "
                            f"up -d --no-deps --force-recreate {service_args}"
                        ).strip()
                    self._daemon_exec(
                        ctx,
                        job,
                        cmd,
                        timeout=300,
                        action=f"[daemon 更新] {label}失败",
                    )
                    continue

                if step_type == "verify":
                    health = self._wait_daemon_update_health(job, ctx["nekro_port"])
                    if health is None:
                        if restore_flow_active:
                            self._sync_compose_running_state(
                                DISTRO_NAME,
                                ctx["deploy_dir"],
                                action="[daemon 更新] 刷新服务运行状态失败",
                            )
                        self._daemon_emit_cancelled_status()
                        return
                    if not health:
                        if restore_flow_active:
                            self._sync_compose_running_state(
                                DISTRO_NAME,
                                ctx["deploy_dir"],
                                action="[daemon 更新] 刷新服务运行状态失败",
                            )
                        self.status_changed.emit("更新失败")
                        self._daemon_fail(job, "verify_timeout", "服务健康检查超时", ctx=ctx)
                        return
                    continue
            except Exception as e:
                if restore_flow_active:
                    self._sync_compose_running_state(
                        DISTRO_NAME,
                        ctx["deploy_dir"],
                        action="[daemon 更新] 刷新服务运行状态失败",
                    )
                self.status_changed.emit("更新失败")
                code = "restart_failed" if step_type == "compose_up" else "launcher_update_failed"
                sensitive_paths = [archive_path] if "archive_path" in locals() else None
                self._daemon_fail(job, code, str(e), ctx=ctx, sensitive_paths=sensitive_paths)
                return

        self._emit_pull_progress("done", "更新完成")
        self.is_running = True
        self._daemon_attach_logs(ctx)
        if channel == "preview":
            self._daemon_mark_instance_channel(
                ctx,
                "preview",
                preview_backup_available=bool(backup_summary) or self._daemon_preview_backup_available(ctx),
            )
        elif channel in {"stable", "rollback"}:
            self._daemon_mark_instance_channel(ctx, "stable", preview_backup_available=False)
        self.status_changed.emit("运行中")
        result = {
            "channel": "preview" if channel == "preview" else "stable",
            "image": agent_image,
            "app_health": "ok",
        }
        if backup_summary:
            result["backup"] = backup_summary
        job.succeed("Nekro Agent 更新完成", result)

    def _wait_daemon_update_health(self, job, port=None, timeout=120):
        port = normalize_port(port, 8021)
        url = f"http://127.0.0.1:{port}/api/health"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if job.is_cancel_requested():
                job.cancel("任务已在健康检查阶段取消")
                return None
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    body = response.read(4096).decode("utf-8", errors="replace")
                    if response.status == 200:
                        try:
                            payload = json.loads(body or "{}")
                        except json.JSONDecodeError:
                            payload = {}
                        if payload.get("ok") is True:
                            self._daemon_add_log(job, "健康检查通过", ctx={"nekro_port": port})
                            return True
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(2)
        return False

    def switch_to_preview(self, create_backup=True):
        """备份数据与配置后，将 Nekro Agent 主容器切换到预览版镜像。"""
        distro = DISTRO_NAME
        inst_id = self.config.get_active_instance_id() if self.config else ""
        inst = self.config.get_instance(inst_id) if self.config and inst_id else None
        inst = inst or {}
        deploy_dir, data_dir, instance_name = self._get_active_deploy_paths()
        nekro_port = normalize_port(
            inst.get("nekro_port")
            or (self.config.get("nekro_port") if self.config else None),
            8021,
        )

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
            # 锁定发起时的实例与端口，防止切换实例后写错目标（见 run_remote_update）。
            try:
                if create_backup:
                    self._emit_pull_progress("stage", "备份数据与配置")
                    archive_path = self._preview_backup_archive_path(inst_id=inst_id)
                    ok, backup_message = self._backup_nekro_archive_for_paths(
                        distro,
                        archive_path,
                        deploy_dir,
                        data_dir,
                        instance_name,
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
                inst_display = inst_id if inst_id and inst_id != "default" else ""
                log_prefix = f"[{inst_display}] " if inst_display else ""
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

        threading.Thread(
            target=lambda: self._run_exclusive_ui_operation("切换预览版", _do_switch),
            daemon=True,
        ).start()

    def restore_stable_from_backup(self):
        """从预览版备份恢复正式版。"""
        distro = DISTRO_NAME
        inst_id = self.config.get_active_instance_id() if self.config else ""
        inst = self.config.get_instance(inst_id) if self.config and inst_id else None
        inst = inst or {}
        deploy_dir, data_dir, instance_name = self._get_active_deploy_paths()
        nekro_port = normalize_port(
            inst.get("nekro_port")
            or (self.config.get("nekro_port") if self.config else None),
            8021,
        )
        if "preview_backup_available" in inst:
            preview_backup_available = bool(inst.get("preview_backup_available"))
        else:
            preview_backup_available = bool(
                self.config.get("preview_backup_available", False) if self.config else False
            )

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
            # 锁定发起时的实例与端口，防止切换实例后写错目标（见 run_remote_update）。
            archive_path = self._preview_backup_archive_path(inst_id=inst_id)

            if self.config and not preview_backup_available:
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, "当前预览版是在未备份的情况下切换的，无法恢复到正式版。")
                return

            if not self.preview_backup_exists(inst_id=inst_id):
                self.status_changed.emit("更新失败")
                self.update_finished.emit(False, f"未找到备份文件：{archive_path}")
                return

            restore_completed = False
            try:
                # 必须在停服和清理真实数据前完整读取归档并确认当前实例成员。
                restore_ctx = {
                    "deploy_dir": deploy_dir,
                    "data_dir": data_dir,
                    "instance_name": instance_name,
                }
                restore_targets = self._daemon_restore_targets_in_archive(
                    restore_ctx, archive_path
                )
                if not restore_targets:
                    self.status_changed.emit("更新失败")
                    self.update_finished.emit(
                        False,
                        "备份归档中未找到当前实例的可恢复目录（归档损坏或不属于该实例），已中止恢复。",
                    )
                    return

                self._emit_pull_progress("stage", "停止相关服务")
                self._stop_running_compose_services(
                    distro,
                    deploy_dir,
                    action="[恢复正式版] 停止相关服务失败",
                )
                self.is_running = False

                self._emit_pull_progress("stage", "恢复备份数据")
                # 旧混合归档可能带有其它实例的路径，按当前实例白名单显式解包。
                member_args = " ".join(
                    shlex.quote(target.lstrip("/")) for target in restore_targets
                )
                try:
                    self._daemon_cleanup_restore_targets(
                        restore_ctx, None, restore_targets
                    )
                    _rc, out = _exec(
                        f"tar -xzf {shlex.quote(archive_path)} -C / {member_args}",
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
                restore_completed = True
                inst_display = inst_id if inst_id and inst_id != "default" else ""
                log_prefix = f"[{inst_display}] " if inst_display else ""
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
            finally:
                if not restore_completed:
                    self._sync_compose_running_state(
                        distro,
                        deploy_dir,
                        action="[恢复正式版] 刷新服务运行状态失败",
                    )

        threading.Thread(
            target=lambda: self._run_exclusive_ui_operation("恢复正式版", _do_restore),
            daemon=True,
        ).start()

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
