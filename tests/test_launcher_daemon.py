import hashlib
import hmac
import json
import tempfile
import time
import unittest

from core.launcher_daemon import DaemonBinding, JobStore, LauncherDaemonFacade


class _Backend:
    config = None


class _BusyBackend:
    config = None
    _deploying = False

    def exclusive_operation_name(self):
        return "远程更新"


def _test_binding():
    return DaemonBinding(
        launcher_inst_id="default",
        instance_id="sha256:test",
        token="secret",
        data_dir="/root/nekro_agent_data",
        deploy_dir="/root/nekro_agent",
        compose_file="/root/nekro_agent/docker-compose.yml",
        env_file="/root/nekro_agent/.env",
        channel="stable",
        nekro_port=8021,
        instance_name="",
    )


def _signed_headers(token, instance_id, method, path, body, nonce="abc"):
    timestamp = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(body).hexdigest()
    text = "\n".join([method, path, timestamp, nonce, body_hash])
    signature = hmac.new(
        token.encode("utf-8"),
        text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "x-na-instance": instance_id,
        "x-na-timestamp": timestamp,
        "x-na-nonce": nonce,
        "x-na-signature": f"v1={signature}",
    }


class LauncherDaemonTests(unittest.TestCase):
    def test_hmac_auth_accepts_valid_signature_and_rejects_replay(self):
        facade = LauncherDaemonFacade(_Backend())
        binding = DaemonBinding(
            launcher_inst_id="default",
            instance_id="sha256:test",
            token="secret",
            data_dir="/root/nekro_agent_data",
            deploy_dir="/root/nekro_agent",
            compose_file="/root/nekro_agent/docker-compose.yml",
            env_file="/root/nekro_agent/.env",
            channel="stable",
            nekro_port=8021,
            instance_name="",
        )
        facade._bindings_by_instance[binding.instance_id] = binding

        body = b"{}"
        headers = _signed_headers(binding.token, binding.instance_id, "GET", "/v1/health", body)
        authed, error = facade._validate_auth("GET", "/v1/health", headers, body)

        self.assertIs(authed, binding)
        self.assertIsNone(error)

        authed, error = facade._validate_auth("GET", "/v1/health", headers, body)

        self.assertIsNone(authed)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error["error"]["code"], "request_replayed")

    def test_hmac_auth_rejects_invalid_signature(self):
        facade = LauncherDaemonFacade(_Backend())
        binding = DaemonBinding(
            launcher_inst_id="default",
            instance_id="sha256:test",
            token="secret",
            data_dir="/root/nekro_agent_data",
            deploy_dir="/root/nekro_agent",
            compose_file="/root/nekro_agent/docker-compose.yml",
            env_file="/root/nekro_agent/.env",
            channel="stable",
            nekro_port=8021,
            instance_name="",
        )
        facade._bindings_by_instance[binding.instance_id] = binding
        headers = _signed_headers("wrong", binding.instance_id, "GET", "/v1/health", b"{}")

        authed, error = facade._validate_auth("GET", "/v1/health", headers, b"{}")

        self.assertIsNone(authed)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error["error"]["code"], "auth_failed")

    def test_hmac_auth_does_not_consume_nonce_on_invalid_signature(self):
        facade = LauncherDaemonFacade(_Backend())
        binding = DaemonBinding(
            launcher_inst_id="default",
            instance_id="sha256:test",
            token="secret",
            data_dir="/root/nekro_agent_data",
            deploy_dir="/root/nekro_agent",
            compose_file="/root/nekro_agent/docker-compose.yml",
            env_file="/root/nekro_agent/.env",
            channel="stable",
            nekro_port=8021,
            instance_name="",
        )
        facade._bindings_by_instance[binding.instance_id] = binding

        body = b"{}"
        headers = _signed_headers("wrong", binding.instance_id, "GET", "/v1/health", body)
        authed, error = facade._validate_auth("GET", "/v1/health", headers, body)

        self.assertIsNone(authed)
        self.assertIsNotNone(error)

        valid_headers = _signed_headers(
            binding.token,
            binding.instance_id,
            "GET",
            "/v1/health",
            body,
        )
        valid_headers["x-na-timestamp"] = headers["x-na-timestamp"]
        valid_headers["x-na-nonce"] = headers["x-na-nonce"]
        body_hash = hashlib.sha256(body).hexdigest()
        text = "\n".join(
            [
                "GET",
                "/v1/health",
                valid_headers["x-na-timestamp"],
                valid_headers["x-na-nonce"],
                body_hash,
            ]
        )
        signature = hmac.new(
            binding.token.encode("utf-8"),
            text.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        valid_headers["x-na-signature"] = f"v1={signature}"

        authed, error = facade._validate_auth("GET", "/v1/health", valid_headers, body)

        self.assertIs(authed, binding)
        self.assertIsNone(error)

    def test_job_store_rejects_parallel_update_for_same_instance(self):
        store = JobStore()
        first, created = store.create_update_job("sha256:test", {"client_request_id": "one"})
        second, second_created = store.create_update_job("sha256:test", {"client_request_id": "two"})

        self.assertIsNotNone(first)
        self.assertTrue(created)
        self.assertIsNone(second)
        self.assertFalse(second_created)

        first.succeed("done")
        third, third_created = store.create_update_job("sha256:test", {"client_request_id": "two"})

        self.assertIsNotNone(third)
        self.assertTrue(third_created)

    def test_job_store_rejects_parallel_jobs_across_types(self):
        store = JobStore()
        first, created = store.create_update_job("sha256:test", {"client_request_id": "one"})
        second, second_created = store.create_backup_job("sha256:test", {"client_request_id": "two"})

        self.assertIsNotNone(first)
        self.assertTrue(created)
        self.assertIsNone(second)
        self.assertFalse(second_created)

        first.cancel()
        third, third_created = store.create_backup_job("sha256:test", {"client_request_id": "two"})

        self.assertIsNotNone(third)
        self.assertTrue(third_created)

    def test_job_cancel_request_marks_running_job_cancel_requested(self):
        store = JobStore()
        job, created = store.create_update_job("sha256:test", {"client_request_id": "one"})

        self.assertIsNotNone(job)
        self.assertTrue(created)
        job.start()
        changed = job.request_cancel()

        self.assertTrue(changed)
        self.assertEqual(job.snapshot()["status"], "cancel_requested")
        self.assertTrue(job.is_cancel_requested())

    def test_cancelled_queued_job_is_not_restarted_or_overwritten(self):
        store = JobStore()
        job, created = store.create_update_job("sha256:test", {"client_request_id": "one"})

        self.assertIsNotNone(job)
        self.assertTrue(created)
        assert job is not None
        job.request_cancel()

        self.assertFalse(job.start())
        job.fail("unexpected", "should not overwrite cancellation")
        job.succeed("should not overwrite cancellation")

        snapshot = job.snapshot()
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertEqual(snapshot["exit_code"], 130)

    def test_job_store_persists_finished_job_and_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(storage_dir=tmpdir)
            job, created = store.create_update_job("sha256:test", {"client_request_id": "one"})

            self.assertIsNotNone(job)
            self.assertTrue(created)
            job.start()
            job.add_log("pull complete")
            job.succeed("done", {"app_health": "ok"})

            reloaded = JobStore(storage_dir=tmpdir)
            loaded = reloaded.get(job.job_id)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.snapshot()["status"], "succeeded")
            self.assertEqual(loaded.snapshot()["result"], {"app_health": "ok"})
            log_lines = [item["line"] for item in loaded.log_snapshot(limit=10)["logs"]]
            self.assertIn("pull complete", log_lines)

    def test_create_jobs_reject_when_launcher_is_busy(self):
        facade = LauncherDaemonFacade(_BusyBackend())
        binding = _test_binding()
        facade._bindings_by_instance[binding.instance_id] = binding

        update_body = json.dumps(
            {"instance_id": binding.instance_id, "channel": "stable"}
        ).encode("utf-8")
        status, payload = facade._create_update_job(binding, update_body)

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "launcher_busy")
        self.assertEqual(payload["error"]["details"], {"operation": "远程更新"})

        backup_body = json.dumps(
            {"instance_id": binding.instance_id, "name": "manual"}
        ).encode("utf-8")
        status, payload = facade._create_backup_job(binding, backup_body)

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "launcher_busy")

        restore_body = json.dumps(
            {"instance_id": binding.instance_id, "backup_id": "a.tar.gz"}
        ).encode("utf-8")
        status, payload = facade._create_restore_job(binding, restore_body)

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "launcher_busy")

    def test_run_update_job_fails_when_exclusive_slot_unavailable(self):
        class _LockedBackend:
            config = None

            def acquire_exclusive_operation(self, _name):
                return False

            def release_exclusive_operation(self):
                return None

        facade = LauncherDaemonFacade(_LockedBackend())
        store = JobStore()
        job, created = store.create_update_job("sha256:test", {})

        self.assertIsNotNone(job)
        self.assertTrue(created)
        assert job is not None
        facade._run_update_job(job, {})

        snapshot = job.snapshot()
        self.assertEqual(snapshot["status"], "failed")
        assert snapshot["error"] is not None
        self.assertEqual(snapshot["error"]["code"], "launcher_busy")

    def test_handle_request_maps_auth_errors_to_401_and_403(self):
        facade = LauncherDaemonFacade(_Backend())

        status, payload = facade._handle_request("GET", "/v1/health", {}, b"")

        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "auth_failed")

        headers = _signed_headers("secret", "sha256:unknown", "GET", "/v1/health", b"")
        status, payload = facade._handle_request("GET", "/v1/health", headers, b"")

        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "instance_not_bound")


if __name__ == "__main__":
    unittest.main()
