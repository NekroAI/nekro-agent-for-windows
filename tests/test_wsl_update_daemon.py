import threading
import unittest

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

    def _daemon_restore_archive(self, _ctx, _job, archive_path, *, action):
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

    def _daemon_restore_archive(self, ctx, job, archive_path, *, action):
        WSLUpdateMixin._daemon_restore_archive(self, ctx, job, archive_path, action=action)

    def _run_wsl_checked(self, _distro, cmd, **_kwargs):
        self.cleanup_commands.append(cmd)
        return None

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

    def test_restore_archive_retries_with_alpine_cleanup_on_extract_failure(self):
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
        self.assertTrue(any("docker run --rm" in cmd and "alpine" in cmd for cmd in backend.cleanup_commands))
        self.assertTrue(any(cmd.startswith("tar -xzf ") for cmd in backend.commands))


if __name__ == "__main__":
    unittest.main()
