import subprocess
import unittest
from unittest import mock

from core.wsl.discovery import WSLDiscoveryMixin
from core.wsl.shell import WSLShellMixin


class _Signal:
    def __init__(self):
        self.messages = []

    def emit(self, *args):
        self.messages.append(args)


class _StopFailureTakeoverDummy(WSLDiscoveryMixin):
    def __init__(self):
        self.log_received = _Signal()
        self.progress_updated = _Signal()
        self.continued_after_stop = False

    def _distro_exists(self):
        return True

    def _check_migration_destination_conflicts(self, _paths):
        return True

    def _get_running_source_services(self, _distro, _deploy_dir):
        return ["nekro_agent", "nekro_postgres"]

    def _stop_source_instance(self, _distro, _deploy_dir):
        return False

    def _migrate_images(self, _src_distro):
        self.continued_after_stop = True
        return True


class _MigrationFlowDummy(WSLDiscoveryMixin):
    def __init__(self, *, running_services=None, pack_ok=True, conflicts=None):
        self.log_received = _Signal()
        self.progress_updated = _Signal()
        self.running_services = list(running_services or [])
        self.pack_ok = pack_ok
        self.stop_calls = []
        self.start_calls = []
        self.conflicts = list(conflicts or [])

    def _distro_exists(self):
        return True

    def _find_migration_destination_conflicts(self, _paths):
        return self.conflicts

    def _get_running_source_services(self, distro, deploy_dir):
        self.probed = (distro, deploy_dir)
        return self.running_services

    def _stop_source_instance(self, distro, deploy_dir):
        self.stop_calls.append((distro, deploy_dir))
        return True

    def _start_source_instance(self, distro, deploy_dir, services):
        self.start_calls.append((distro, deploy_dir, services))
        return True

    def _migrate_images(self, _src_distro):
        return True

    def _pack_source_data(self, *_args):
        return self.pack_ok

    def _pack_via_windows_temp(self, *_args):
        return ""

    def _create_migration_staging_dir(self):
        return "/root/.nekro-agent-migrate.test"

    def _prepare_target_docker_for_migration(self):
        return False

    def _restore_data(self, _archive_path, _staging_dir):
        return True

    def _relocate_dir(self, _src, _dest, timeout=120):
        return True

    def _cleanup_archive(self, _src_distro, _archive_path):
        return None

    def _cleanup_migration_staging_dir(self, _staging_dir):
        return None

    def _wsl_path_exists(self, _distro, _path, *, user="root"):
        return False

    def _sync_config_from_env(self, *_args):
        return True


class _RollbackMigrationDummy(_MigrationFlowDummy):
    def __init__(self):
        super().__init__(running_services=["nekro_agent"])
        self.paths = set()
        self.move_calls = []
        self.deploy_move_failed = False
        self.docker_restore_calls = 0
        self.staging_cleanup_calls = 0

    def _find_migration_destination_conflicts(self, paths):
        return [path for path in paths if path in self.paths]

    def _prepare_target_docker_for_migration(self):
        return True

    def _restore_data(self, _archive_path, staging_dir):
        self.paths.update(
            {
                self._staged_migration_path(staging_dir, "/root/nekro_agent_data"),
                self._staged_migration_path(staging_dir, "/root/nekro_agent"),
            }
        )
        return True

    def _relocate_dir(self, src, dest, timeout=120):
        self.move_calls.append((src, dest, timeout))
        if dest == "/root/nekro_agent" and not self.deploy_move_failed:
            self.deploy_move_failed = True
            return False
        if src not in self.paths or dest in self.paths:
            return False
        self.paths.remove(src)
        self.paths.add(dest)
        return True

    def _wsl_path_exists(self, _distro, path, *, user="root"):
        return path in self.paths

    def _cleanup_migration_staging_dir(self, staging_dir):
        self.staging_cleanup_calls += 1
        self.paths = {path for path in self.paths if not path.startswith(staging_dir + "/")}

    def _restore_target_docker_after_migration(self):
        self.docker_restore_calls += 1
        return True


class _RestoreDummy(WSLDiscoveryMixin, WSLShellMixin):
    def __init__(self, *, docker_active=True, restore_returncode=0, start_returncode=0):
        self.log_received = _Signal()
        self.commands = []
        self.docker_active = docker_active
        self.restore_returncode = restore_returncode
        self.start_returncode = start_returncode

    def _wsl_run(self, distro, cmd, timeout=60, user=None):
        self.commands.append((distro, cmd, timeout, user))
        if cmd == "systemctl is-active docker":
            return subprocess.CompletedProcess(
                [],
                0 if self.docker_active else 3,
                stdout=b"active\n" if self.docker_active else b"inactive\n",
                stderr=b"",
            )
        if cmd == "systemctl start docker":
            return subprocess.CompletedProcess(
                [],
                self.start_returncode,
                stdout=b"",
                stderr=b"start failed\n" if self.start_returncode else b"",
            )
        if cmd.startswith("tar -xzf "):
            return subprocess.CompletedProcess(
                [],
                self.restore_returncode,
                stdout=b"",
                stderr=b"archive corrupt\n" if self.restore_returncode else b"",
            )
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")


class _RelocateDummy(WSLDiscoveryMixin, WSLShellMixin):
    def __init__(self, existing_paths):
        self.log_received = _Signal()
        self.existing_paths = set(existing_paths)
        self.commands = []

    def _wsl_path_exists(self, _distro, path, *, user="root"):
        return path in self.existing_paths

    def _wsl_run(self, distro, cmd, timeout=60, user=None):
        self.commands.append((distro, cmd, timeout, user))
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")


class WSLDiscoveryMigrationTests(unittest.TestCase):
    def test_config_sync_failure_is_reported(self):
        class Config:
            last_save_error = "配置目录只读"

            def get_instance(self, _inst_id):
                return None

            def next_instance_id(self):
                return "inst_2"

            def get_default_instance_id(self):
                return "default"

            def update_instance_with_globals(self, *_args, **_kwargs):
                return False

        backend = WSLDiscoveryMixin()
        backend.config = Config()
        backend.log_received = _Signal()

        saved = backend._sync_config_from_env({}, "lite")

        self.assertFalse(saved)
        message, level = backend.log_received.messages[-1]
        self.assertEqual(level, "error")
        self.assertIn("配置目录只读", message)

    def test_destination_conflict_aborts_before_source_is_stopped(self):
        backend = _MigrationFlowDummy(
            running_services=["nekro_agent"],
            conflicts=["/root/nekro_agent", "/var/lib/docker/volumes/nekro_postgres_data"],
        )
        instance = {
            "distro": "Ubuntu",
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "env": {},
            "deploy_mode": "lite",
        }

        self.assertFalse(backend._takeover_foreign(instance))

        self.assertEqual(backend.stop_calls, [])
        self.assertFalse(hasattr(backend, "probed"))
        message, level = backend.log_received.messages[-1]
        self.assertEqual(level, "error")
        self.assertIn("避免覆盖或合并", message)
        self.assertIn("/root/nekro_agent", message)
        self.assertIn("INSTANCE_NAME", message)

    def test_deploy_move_failure_rolls_back_data_and_allows_retry(self):
        backend = _RollbackMigrationDummy()
        instance = {
            "distro": "Ubuntu",
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "env": {},
            "deploy_mode": "lite",
        }

        self.assertFalse(backend._takeover_foreign(instance))

        staging = "/root/.nekro-agent-migrate.test"
        staged_data = f"{staging}/root/nekro_agent_data"
        staged_deploy = f"{staging}/root/nekro_agent"
        self.assertEqual(
            backend.move_calls[:3],
            [
                (staged_data, "/root/nekro_agent_data", 300),
                (staged_deploy, "/root/nekro_agent", 120),
                ("/root/nekro_agent_data", staged_data, 300),
            ],
        )
        self.assertNotIn("/root/nekro_agent_data", backend.paths)
        self.assertNotIn("/root/nekro_agent", backend.paths)
        self.assertEqual(backend.docker_restore_calls, 1)
        self.assertEqual(backend.staging_cleanup_calls, 1)

        dest_paths = backend._migration_destination_paths(instance)
        self.assertEqual(
            backend._find_migration_destination_conflicts(
                [dest_paths[0], dest_paths[1], *dest_paths[2]]
            ),
            [],
        )
        self.assertTrue(backend._takeover_foreign(instance))

    def test_docker_restart_failure_keeps_committed_target_and_source_stopped(self):
        backend = _RollbackMigrationDummy()
        backend.deploy_move_failed = True
        backend._restore_target_docker_after_migration = lambda: False
        instance = {
            "distro": "Ubuntu",
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "env": {},
            "deploy_mode": "lite",
        }

        self.assertFalse(backend._takeover_foreign(instance))

        self.assertIn("/root/nekro_agent_data", backend.paths)
        self.assertIn("/root/nekro_agent", backend.paths)
        self.assertEqual(backend.start_calls, [])
        self.assertEqual(backend.staging_cleanup_calls, 1)
        self.assertTrue(
            any(
                "数据与启动器配置已完成迁移" in message and level == "error"
                for message, level in backend.log_received.messages
            )
        )

    def test_stale_stopped_status_still_stops_actually_running_source(self):
        backend = _MigrationFlowDummy(running_services=["nekro_agent"])
        instance = {
            "distro": "Ubuntu",
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "status": "stopped",
            "env": {},
            "deploy_mode": "lite",
        }

        self.assertTrue(backend._takeover_foreign(instance))

        self.assertEqual(backend.probed, ("Ubuntu", "/root/nekro_agent"))
        self.assertEqual(backend.stop_calls, [("Ubuntu", "/root/nekro_agent")])
        self.assertEqual(backend.start_calls, [])

    def test_failure_after_source_stop_restores_only_previously_running_services(self):
        backend = _MigrationFlowDummy(
            running_services=["nekro_agent", "nekro_postgres"],
            pack_ok=False,
        )
        instance = {
            "distro": "Ubuntu",
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "status": "running",
            "env": {},
            "deploy_mode": "lite",
        }

        self.assertFalse(backend._takeover_foreign(instance))

        self.assertEqual(
            backend.start_calls,
            [
                (
                    "Ubuntu",
                    "/root/nekro_agent",
                    ["nekro_agent", "nekro_postgres"],
                )
            ],
        )

    def test_running_source_stop_failure_aborts_before_copying_data(self):
        backend = _StopFailureTakeoverDummy()
        instance = {
            "distro": "Ubuntu",
            "deploy_dir": "/root/nekro_agent",
            "data_dir": "/root/nekro_agent_data",
            "status": "running",
        }

        self.assertFalse(backend._takeover_foreign(instance))

        self.assertFalse(backend.continued_after_stop)
        self.assertTrue(
            any(
                "已中止迁移" in message and level == "error"
                for message, level in backend.log_received.messages
            )
        )

    def test_stop_source_failure_log_includes_command_context(self):
        backend = _RestoreDummy()
        failed = subprocess.CompletedProcess(
            [],
            17,
            stdout=b"",
            stderr=b"compose stop failed\n",
        )

        with mock.patch("core.wsl.discovery.subprocess.run", return_value=failed):
            self.assertFalse(
                backend._stop_source_instance("Ubuntu-Test", "/root/nekro_agent")
            )

        message, level = backend.log_received.messages[-1]
        self.assertEqual(level, "error")
        self.assertIn("发行版: Ubuntu-Test", message)
        self.assertIn("返回码: 17", message)
        self.assertIn("docker compose", message)
        self.assertIn("compose stop failed", message)

    def test_restore_failure_only_extracts_into_staging_directory(self):
        backend = _RestoreDummy(docker_active=True, restore_returncode=2)

        self.assertFalse(
            backend._restore_data(
                "/mnt/wsl/na_migrate.tar.gz",
                "/root/.nekro-agent-migrate.test",
            )
        )

        commands = [cmd for _distro, cmd, _timeout, _user in backend.commands]
        self.assertTrue(commands[0].startswith("tar -tzf /mnt/wsl/na_migrate.tar.gz"))
        self.assertEqual(
            commands[1],
            "tar -xzf /mnt/wsl/na_migrate.tar.gz "
            "-C /root/.nekro-agent-migrate.test --keep-old-files",
        )
        self.assertNotIn("-C / ", commands[1])
        message, level = backend.log_received.messages[-1]
        self.assertEqual(level, "error")
        self.assertIn("数据还原失败", message)

    def test_prepare_target_docker_only_stops_it_when_active(self):
        active_backend = _RestoreDummy(docker_active=True)
        inactive_backend = _RestoreDummy(docker_active=False)

        self.assertTrue(active_backend._prepare_target_docker_for_migration())
        self.assertFalse(inactive_backend._prepare_target_docker_for_migration())

        active_commands = [cmd for _distro, cmd, _timeout, _user in active_backend.commands]
        inactive_commands = [cmd for _distro, cmd, _timeout, _user in inactive_backend.commands]
        self.assertEqual(
            active_commands,
            ["systemctl is-active docker", "systemctl stop docker"],
        )
        self.assertEqual(inactive_commands, ["systemctl is-active docker"])

    def test_restore_succeeds_in_empty_staging_directory(self):
        backend = _RestoreDummy(docker_active=False)

        self.assertTrue(
            backend._restore_data(
                "/mnt/wsl/na_migrate.tar.gz",
                "/root/.nekro-agent-migrate.test",
            )
        )

        commands = [cmd for _distro, cmd, _timeout, _user in backend.commands]
        self.assertEqual(len(commands), 2)
        self.assertIn("-C /root/.nekro-agent-migrate.test", commands[1])

    def test_relocate_refuses_existing_destination_without_running_move(self):
        src = "/root/.nekro-agent-migrate.test/root/nekro_agent"
        dest = "/root/nekro_agent"
        backend = _RelocateDummy({src, dest})

        self.assertFalse(backend._relocate_dir(src, dest))

        self.assertEqual(backend.commands, [])
        message, level = backend.log_received.messages[-1]
        self.assertEqual(level, "error")
        self.assertIn("拒绝覆盖或合并", message)

    @mock.patch("core.wsl.discovery.time.sleep")
    def test_restore_reports_failure_when_original_docker_state_cannot_be_restored(
        self,
        _sleep,
    ):
        backend = _RestoreDummy(docker_active=True, start_returncode=1)

        self.assertFalse(backend._restore_target_docker_after_migration())

        message, level = backend.log_received.messages[-1]
        self.assertEqual(level, "error")
        self.assertIn("恢复目标 Docker 原运行状态失败", message)
        self.assertIn("返回码: 1", message)
        self.assertIn("start failed", message)


if __name__ == "__main__":
    unittest.main()
