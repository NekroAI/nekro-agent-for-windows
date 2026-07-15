import threading
import unittest
from types import SimpleNamespace

from core.launcher_daemon import DaemonJob
from core.wsl.shell import WSLShellMixin
from core.wsl.update import WSLUpdateMixin


class _Signal:
    def __init__(self):
        self.items = []

    def emit(self, *args):
        self.items.append(args)


class _DummyDaemonUpdate(WSLUpdateMixin, WSLShellMixin):
    config = None

    def __init__(self):
        self._stop_event = threading.Event()
        self.status_changed = _Signal()
        self.log_received = _Signal()
        self.commands = []
        self.pulls = []
        self.restore_calls = []
        self.cleanup_commands = []
        self.restore_attempts = 0
        self.is_running = False

    def get_agent_image_ref(self, release_channel="stable", *_args, **_kwargs):
        return f"kromiose/nekro-agent:{'preview' if release_channel == 'preview' else 'latest'}"

    def _emit_pull_progress(self, *_args):
        return None

    def _daemon_validate_instance(self, _ctx, _job):
        return True

    def _rewrite_daemon_compose_channel(self, _ctx, channel):
        self.commands.append(f"switch:{channel}")

    def _daemon_latest_backup_path(self, _request, _name):
        return "/root/.na-tools/backups/test/nekro_agent_backup_pre-preview_20260620_123000.tar.gz"

    def _daemon_exec(self, _ctx, _job, cmd, **_kwargs):
        self.commands.append(cmd)
        return 0, ""

    def _run_wsl_checked(self, _distro, cmd, **_kwargs):
        self.commands.append(cmd)
        stdout = b""
        if cmd.startswith("tar -tzf "):
            stdout = b"root/nekro_agent/docker-compose.yml\nroot/nekro_agent_data/config.json\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    def _daemon_restore_archive(self, _ctx, _job, archive_path, *, action, targets=None):
        self.restore_calls.append((archive_path, action))

    def _pull_images(self, _distro, images):
        self.pulls.extend(images)
        return True

    def _wait_daemon_update_health(self, _job, _port=None, _timeout=120):
        return True

    def _daemon_attach_logs(self, _ctx):
        return None

    def _daemon_mark_instance_channel(self, _ctx, channel, preview_backup_available=False):
        self.commands.append(f"mark:{channel}:{preview_backup_available}")

    def _daemon_backup_summary(self, path, name=None):
        return {
            "backup_id": path.rsplit("/", 1)[-1],
            "filename": path.rsplit("/", 1)[-1],
            "name": name or "manual",
            "created_at": "2026-06-20T00:00:00+00:00",
            "size_bytes": 1,
        }


class _BackupScopeDummy(_DummyDaemonUpdate):
    def _wsl_exec(self, _distro, cmd, timeout=60, user=None):
        self.commands.append(cmd)
        if "/root/.na-tools/backups/test/good.tar.gz" in cmd:
            return "yes\n"
        if "/root/na_update_backup.tar.gz" in cmd:
            return "yes\n"
        if "find /root/.na-tools/backups/test" in cmd:
            return ""
        return ""


class _RestoreFallbackDummy(_DummyDaemonUpdate):
    def _wsl_exec(self, _distro, cmd, timeout=60, user=None):
        self.commands.append(cmd)
        if cmd.startswith("tar -tzf "):
            return "root/nekro_agent/docker-compose.yml\nroot/nekro_agent_data/config.json\n"
        return ""

    def _daemon_restore_archive(self, ctx, job, archive_path, *, action, targets=None):
        WSLUpdateMixin._daemon_restore_archive(
            self, ctx, job, archive_path, action=action, targets=targets
        )

    def _run_wsl_checked(self, _distro, cmd, **_kwargs):
        if cmd.startswith("tar -tzf "):
            self.commands.append(cmd)
            return SimpleNamespace(
                returncode=0,
                stdout=b"root/nekro_agent/docker-compose.yml\nroot/nekro_agent_data/config.json\n",
                stderr=b"",
            )
        self.cleanup_commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def _daemon_exec(self, ctx, job, cmd, **_kwargs):
        self.commands.append(cmd)
        if cmd.startswith("tar -xzf ") and self.restore_attempts == 0:
            self.restore_attempts += 1
            raise RuntimeError(
                "恢复失败 "
                f"{ctx['deploy_dir']} {ctx['data_dir']} "
                "NEKRO_ADMIN_PASSWORD=secret"
            )
        return 0, ""


class _MixedArchiveRestoreDummy(_DummyDaemonUpdate):
    """模拟修复前生成的混合归档：命名实例的备份里混入了默认实例的路径。"""

    archive_listing = (
        "root/foo_nekro_agent/docker-compose.yml\n"
        "root/foo_nekro_agent_data/config.json\n"
        "root/nekro_agent/docker-compose.yml\n"
        "root/nekro_agent_data/config.json\n"
        "var/lib/docker/volumes/nekro_postgres_data/_data/x\n"
    )

    def _wsl_exec(self, _distro, cmd, timeout=60, user=None):
        self.commands.append(cmd)
        if cmd.startswith("tar -tzf "):
            return self.archive_listing
        return ""

    def _daemon_restore_archive(self, ctx, job, archive_path, *, action, targets=None):
        WSLUpdateMixin._daemon_restore_archive(
            self, ctx, job, archive_path, action=action, targets=targets
        )

    def _daemon_exec(self, ctx, job, cmd, **_kwargs):
        self.commands.append(cmd)
        return 0, ""

    def _run_wsl_checked(self, _distro, cmd, **_kwargs):
        if cmd.startswith("tar -tzf "):
            self.commands.append(cmd)
            return SimpleNamespace(
                returncode=0,
                stdout=self.archive_listing.encode("utf-8"),
                stderr=b"",
            )
        self.cleanup_commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


class _ConsistentBackupDummy(_DummyDaemonUpdate):
    def _existing_backup_targets_for_paths(self, *_args):
        return ["/var/lib/docker/volumes/nekro_postgres_data", "/root/nekro_agent"]

    def _run_wsl_checked(self, _distro, cmd, **_kwargs):
        self.commands.append(cmd)
        stdout = b"nekro_agent\nnekro_postgres\n" if "ps --status running --services" in cmd else b""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")


class _StopFailureBackupDummy(_ConsistentBackupDummy):
    def _run_wsl_checked(self, _distro, cmd, **_kwargs):
        if " stop " in cmd:
            self.commands.append(cmd)
            raise RuntimeError("compose stop failed")
        return super()._run_wsl_checked(_distro, cmd, **_kwargs)


class _CancelDuringValidateDummy(_DummyDaemonUpdate):
    def _daemon_validate_instance(self, _ctx, job):
        job.request_cancel()
        return True


class _RestoreJobOrderDummy(_DummyDaemonUpdate):
    def __init__(self, archive_listing=None, restore_error=None):
        super().__init__()
        self.archive_listing = archive_listing or (
            "root/nekro_agent/docker-compose.yml\n"
            "root/nekro_agent_data/config.json\n"
        )
        self.restore_error = restore_error
        self.running_services = ["nekro_agent", "nekro_postgres"]

    def _daemon_resolve_backup_path(self, _request, _backup_id):
        return "/root/.na-tools/backups/test/good.tar.gz"

    def _run_wsl_checked(self, _distro, cmd, **_kwargs):
        self.commands.append(cmd)
        if cmd.startswith("tar -tzf "):
            return SimpleNamespace(
                returncode=0,
                stdout=self.archive_listing.encode("utf-8"),
                stderr=b"",
            )
        if "ps --status running --services" in cmd:
            stdout = ("\n".join(self.running_services) + "\n").encode("utf-8")
            return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")
        if " stop " in cmd:
            self.running_services = []
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def _daemon_restore_archive(self, _ctx, _job, archive_path, *, action, targets=None):
        self.restore_calls.append((archive_path, action))
        if self.restore_error:
            raise RuntimeError(self.restore_error)


class WSLDaemonUpdateTests(unittest.TestCase):
    def test_rollback_restore_pre_preview_skips_stable_pull(self):
        backend = _DummyDaemonUpdate()
        request = {
            "instance_id": "sha256:test",
            "channel": "rollback",
            "restore_pre_preview": True,
            "_launcher_inst_id": "default",
            "_deploy_dir": "/root/nekro_agent",
            "_data_dir": "/root/nekro_agent_data",
            "_nekro_port": 8021,
            "_current_channel": "preview",
        }
        job = DaemonJob("upd_test", "update", "sha256:test", request)

        backend.run_daemon_update_job(request, job)

        self.assertEqual(backend.pulls, [])
        self.assertTrue(backend.restore_calls)
        self.assertEqual(job.snapshot()["status"], "succeeded")

    def test_named_instance_backup_targets_exclude_default_instance_paths(self):
        backend = _DummyDaemonUpdate()

        targets = backend._backup_target_candidates_for_paths(
            "/root/foo_nekro_agent", "/root/foo_nekro_agent_data", "foo_"
        )

        self.assertIn("/var/lib/docker/volumes/foo_nekro_postgres_data", targets)
        self.assertIn("/root/foo_nekro_agent_data", targets)
        self.assertNotIn("/root/nekro_agent", targets)
        self.assertNotIn("/root/nekro_agent_data", targets)
        self.assertNotIn("/var/lib/docker/volumes/nekro_postgres_data", targets)
        self.assertNotIn("/var/lib/docker/volumes/nekro_qdrant_data", targets)

    def test_default_instance_backup_targets_keep_legacy_paths(self):
        backend = _DummyDaemonUpdate()

        targets = backend._backup_target_candidates_for_paths(
            "/root/nekro_agent", "/root/nekro_agent_data", ""
        )

        self.assertIn("/var/lib/docker/volumes/nekro_postgres_data", targets)
        self.assertIn("/var/lib/docker/volumes/nekro_qdrant_data", targets)
        self.assertIn("/root/nekro_agent", targets)
        self.assertIn("/root/nekro_agent_data", targets)

    def test_daemon_restore_backup_id_is_limited_to_current_instance_dir(self):
        backend = _BackupScopeDummy()
        request = {"instance_id": "sha256:test"}

        self.assertEqual(
            backend._daemon_resolve_backup_path(request, "good.tar.gz"),
            "/root/.na-tools/backups/test/good.tar.gz",
        )
        self.assertEqual(backend._daemon_resolve_backup_path(request, "na_update_backup.tar.gz"), "")
        self.assertEqual(backend.list_daemon_backups(request), [])
        self.assertFalse(any("/root/na_update_backup.tar.gz" in cmd for cmd in backend.commands))

    def test_daemon_log_redaction_removes_paths_health_url_and_secrets(self):
        backend = _DummyDaemonUpdate()
        ctx = {
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "nekro_port": 8021,
        }

        redacted = backend._daemon_redact_text(
            "done /root/.na-tools/backups/test/a.tar.gz "
            "/root/nekro_agent /root/nekro_agent_data "
            "http://127.0.0.1:8021/api/health "
            "JWT_SECRET_KEY=abc NA_TOOLS_DAEMON_TOKEN_FILE=/root/nekro_agent_data/.na-tools/daemon.token",
            ctx=ctx,
        )

        self.assertNotIn("/root/.na-tools", redacted)
        self.assertNotIn("/root/nekro_agent", redacted)
        self.assertNotIn("127.0.0.1:8021", redacted)
        self.assertNotIn("abc", redacted)
        self.assertNotIn("daemon.token", redacted)

    def test_restore_archive_retries_with_root_cleanup_on_extract_failure(self):
        backend = _RestoreFallbackDummy()
        ctx = {
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "instance_name": "",
            "nekro_port": 8021,
        }
        job = DaemonJob("upd_test", "restore", "sha256:test", {})

        backend._daemon_restore_archive(
            ctx,
            job,
            "/root/.na-tools/backups/test/a.tar.gz",
            action="[daemon 还原] 恢复备份数据失败",
        )

        self.assertEqual(backend.restore_attempts, 1)
        self.assertTrue(any(cmd.startswith("mkdir -p -- ") and "find " in cmd for cmd in backend.cleanup_commands))
        self.assertFalse(any("docker run" in cmd or "alpine" in cmd for cmd in backend.cleanup_commands))
        self.assertTrue(any(cmd.startswith("tar -xzf ") for cmd in backend.commands))

    def test_restore_mixed_archive_extracts_only_whitelisted_members(self):
        backend = _MixedArchiveRestoreDummy()
        ctx = {
            "deploy_dir": "/root/foo_nekro_agent",
            "data_dir": "/root/foo_nekro_agent_data",
            "instance_name": "foo_",
            "nekro_port": 8021,
        }
        job = DaemonJob("upd_test", "restore", "sha256:test", {})

        backend._daemon_restore_archive(
            ctx,
            job,
            "/root/.na-tools/backups/foo/a.tar.gz",
            action="[daemon 还原] 恢复备份数据失败",
        )

        tar_cmds = [cmd for cmd in backend.commands if cmd.startswith("tar -xzf ")]
        self.assertEqual(len(tar_cmds), 1)
        tar_cmd = tar_cmds[0]
        self.assertIn("root/foo_nekro_agent_data", tar_cmd)
        self.assertIn("root/foo_nekro_agent", tar_cmd)
        # 混入的默认实例路径不得被解包
        self.assertNotIn(" root/nekro_agent", tar_cmd)
        self.assertNotIn("var/lib/docker/volumes/nekro_postgres_data", tar_cmd)
        # 也不允许退化成整包解压
        self.assertFalse(tar_cmd.rstrip().endswith("-C /"))
        self.assertEqual(len(backend.cleanup_commands), 2)
        self.assertTrue(all(cmd.startswith("mkdir -p -- ") for cmd in backend.cleanup_commands))
        self.assertTrue(all("find " in cmd for cmd in backend.cleanup_commands))
        self.assertFalse(any("docker run" in cmd or "alpine" in cmd for cmd in backend.cleanup_commands))

    def test_backup_stops_running_database_services_and_restores_original_state(self):
        backend = _ConsistentBackupDummy()

        ok, _message = backend._backup_nekro_archive_for_paths(
            "NekroAgent",
            "/root/backup.tar.gz",
            "/root/nekro_agent",
            "/root/nekro_agent_data",
            "",
        )

        self.assertTrue(ok)
        stop_index = next(i for i, cmd in enumerate(backend.commands) if " compose " in cmd and " stop " in cmd)
        tar_index = next(i for i, cmd in enumerate(backend.commands) if "tar -czf" in cmd)
        start_index = next(i for i, cmd in enumerate(backend.commands) if " compose " in cmd and " start " in cmd)
        self.assertLess(stop_index, tar_index)
        self.assertLess(tar_index, start_index)
        self.assertIn("nekro_postgres", backend.commands[stop_index])
        self.assertIn("nekro_agent", backend.commands[start_index])

    def test_backup_stop_failure_aborts_before_archive_creation(self):
        backend = _StopFailureBackupDummy()

        ok, message = backend._backup_nekro_archive_for_paths(
            "NekroAgent",
            "/root/backup.tar.gz",
            "/root/nekro_agent",
            "/root/nekro_agent_data",
            "",
        )

        self.assertFalse(ok)
        self.assertIn("compose stop failed", message)
        self.assertFalse(any("tar -czf" in cmd for cmd in backend.commands))

    def test_compose_service_discovery_does_not_assume_napcat_exists(self):
        backend = _ConsistentBackupDummy()

        services = backend._compose_running_services(
            "NekroAgent", "/root/nekro_agent", action="读取服务失败"
        )

        self.assertEqual(services, ["nekro_agent", "nekro_postgres"])
        ps_cmd = next(cmd for cmd in backend.commands if "ps --status running --services" in cmd)
        self.assertNotIn("nekro_napcat", ps_cmd)

    def test_restore_archive_without_instance_members_is_rejected(self):
        backend = _MixedArchiveRestoreDummy()
        backend.archive_listing = "root/other_nekro_agent/docker-compose.yml\n"
        ctx = {
            "deploy_dir": "/root/foo_nekro_agent",
            "data_dir": "/root/foo_nekro_agent_data",
            "instance_name": "foo_",
            "nekro_port": 8021,
        }
        job = DaemonJob("upd_test", "restore", "sha256:test", {})

        with self.assertRaises(RuntimeError):
            backend._daemon_restore_archive(
                ctx,
                job,
                "/root/.na-tools/backups/foo/a.tar.gz",
                action="[daemon 还原] 恢复备份数据失败",
            )
        self.assertFalse(any(cmd.startswith("tar -xzf ") for cmd in backend.commands))

    def test_daemon_update_job_cancel_resets_status_bar(self):
        backend = _CancelDuringValidateDummy()
        backend.is_running = True
        request = {
            "instance_id": "sha256:test",
            "channel": "stable",
            "backup": False,
            "_launcher_inst_id": "default",
            "_deploy_dir": "/root/nekro_agent",
            "_data_dir": "/root/nekro_agent_data",
            "_nekro_port": 8021,
            "_current_channel": "stable",
        }
        job = DaemonJob("upd_cancel", "update", "sha256:test", request)

        backend.run_daemon_update_job(request, job)

        self.assertEqual(job.snapshot()["status"], "cancelled")
        # 状态栏不能停在「更新中...」，取消后要按真实运行状态复位
        self.assertEqual(backend.status_changed.items[-1], ("运行中",))

    def test_daemon_update_job_cancel_emits_stopped_when_not_running(self):
        backend = _CancelDuringValidateDummy()
        backend.is_running = False
        request = {
            "instance_id": "sha256:test",
            "channel": "stable",
            "backup": False,
            "_launcher_inst_id": "default",
            "_deploy_dir": "/root/nekro_agent",
            "_data_dir": "/root/nekro_agent_data",
            "_nekro_port": 8021,
            "_current_channel": "stable",
        }
        job = DaemonJob("upd_cancel2", "update", "sha256:test", request)

        backend.run_daemon_update_job(request, job)

        self.assertEqual(job.snapshot()["status"], "cancelled")
        self.assertEqual(backend.status_changed.items[-1], ("已停止",))


if __name__ == "__main__":
    unittest.main()
