import threading
import unittest
from unittest import mock

import core.wsl.images as images_module
from core.wsl.images import WSLImageMixin
from core.wsl.shell import WSLShellMixin


class _Signal:
    def __init__(self):
        self.messages = []

    def emit(self, *args):
        self.messages.append(args)


class _SilentPullProc:
    """模拟 docker pull 启动后长时间不输出任何内容的进程。"""

    def __init__(self):
        self._killed = threading.Event()
        self.stdout = self

    def readline(self):
        self._killed.wait()
        return b""

    def poll(self):
        return 0 if self._killed.is_set() else None

    def kill(self):
        self._killed.set()

    def wait(self, timeout=None):
        return 0


class _SilentPullDummy(WSLImageMixin, WSLShellMixin):
    _PULL_TIMEOUT = 1

    def __init__(self):
        self.log_received = _Signal()
        self.progress = []

    def _emit_pull_progress(self, phase, message):
        self.progress.append((phase, message))


class _DummyImages(WSLImageMixin):
    _DOCKER_PROXY_REGISTRIES = ("slow.test", "fast.test")

    def __init__(self):
        self.log_received = _Signal()
        self.progress = []
        self.pulls = []
        self.retags = []

    def _emit_pull_progress(self, phase, message):
        self.progress.append((phase, message))

    def _probe_pull_candidate(self, distro, candidate):
        latencies = {
            "slow.test": 200,
            "fast.test": 50,
            "Docker Hub": 500,
        }
        return True, latencies[candidate.source], "", True

    def _pull_image_once(self, distro, image_ref):
        self.pulls.append(image_ref)
        return True, ["pull ok"]

    def _retag_pulled_image(self, distro, source_ref, target_ref):
        self.retags.append((source_ref, target_ref))
        return True, ""


class WSLImageMixinTests(unittest.TestCase):
    def test_normalize_image_ref_adds_latest_tag(self):
        self.assertEqual(
            WSLImageMixin._normalize_image_ref("mlikiowa/napcat-docker"),
            "mlikiowa/napcat-docker:latest",
        )

    def test_docker_hub_repo_tag_adds_library_namespace(self):
        self.assertEqual(
            WSLImageMixin._docker_hub_repo_tag("postgres:14"),
            "library/postgres:14",
        )

    def test_docker_hub_repo_tag_keeps_namespace_and_latest(self):
        self.assertEqual(
            WSLImageMixin._docker_hub_repo_tag("kromiose/nekro-agent-sandbox"),
            "kromiose/nekro-agent-sandbox:latest",
        )

    def test_docker_hub_repo_tag_strips_docker_io_registry(self):
        self.assertEqual(
            WSLImageMixin._docker_hub_repo_tag("docker.io/library/postgres:14"),
            "library/postgres:14",
        )

    def test_docker_hub_repo_tag_rejects_non_hub_registry(self):
        self.assertEqual(
            WSLImageMixin._docker_hub_repo_tag("ghcr.io/example/app:1"),
            "",
        )

    def test_proxy_image_ref_concatenates_registry_and_repo_tag(self):
        self.assertEqual(
            WSLImageMixin._proxy_image_ref("docker.1ms.run", "postgres:14"),
            "docker.1ms.run/library/postgres:14",
        )

    def test_registry_manifest_target_handles_registry_with_port(self):
        self.assertEqual(
            WSLImageMixin._registry_manifest_target("localhost:5000/team/app:1.2.3"),
            ("localhost:5000", "team/app", "1.2.3"),
        )

    def test_registry_manifest_target_handles_digest_ref(self):
        self.assertEqual(
            WSLImageMixin._registry_manifest_target(
                "registry.example.com/team/app@sha256:abc123"
            ),
            ("registry.example.com", "team/app", "sha256:abc123"),
        )

    def test_parse_auth_challenge_extracts_realm_service_scope(self):
        params = WSLImageMixin._parse_auth_challenge(
            'Bearer realm="https://auth.docker.io/token",'
            'service="registry.docker.io",scope="repository:library/postgres:pull"'
        )
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params["realm"], "https://auth.docker.io/token")
        self.assertEqual(params["service"], "registry.docker.io")
        self.assertEqual(params["scope"], "repository:library/postgres:pull")

    def test_parse_auth_challenge_ignores_non_bearer(self):
        self.assertIsNone(WSLImageMixin._parse_auth_challenge('Basic realm="x"'))
        self.assertIsNone(WSLImageMixin._parse_auth_challenge(""))
        self.assertIsNone(WSLImageMixin._parse_auth_challenge(None))

    def test_local_digest_cmd_uses_valid_go_template(self):
        cmd = WSLImageMixin._local_digest_cmd("postgres:14")

        self.assertIn("'{{index .RepoDigests 0}}'", cmd)
        self.assertNotIn("{{{{", cmd)

    def test_rank_pull_candidates_prefers_fastest_probe(self):
        candidates = _DummyImages()._rank_pull_candidates("NekroAgent", "postgres:14")

        self.assertEqual(candidates[0].source, "fast.test")
        self.assertEqual(candidates[0].pull_ref, "fast.test/library/postgres:14")
        self.assertEqual(candidates[0].final_ref, "postgres:14")

    def test_pull_images_emits_speedtest_before_pull_stage(self):
        backend = _DummyImages()

        self.assertTrue(backend._pull_images("NekroAgent", ["postgres:14"]))

        self.assertEqual(
            backend.progress[0],
            ("speedtest", "1/1|测速镜像源 (1/1): postgres:14"),
        )
        self.assertEqual(backend.progress[1][0], "stage")
        self.assertIn(
            "拉取镜像 (1/1) [fast.test (50ms)]: postgres:14",
            backend.progress[1][1],
        )
        self.assertEqual(backend.progress[-1], ("done", "所有镜像拉取完成"))
        self.assertEqual(backend.pulls, ["fast.test/library/postgres:14"])
        self.assertEqual(
            backend.retags,
            [("fast.test/library/postgres:14", "postgres:14")],
        )

    def test_speedtest_results_are_reused_by_pull_images(self):
        backend = _DummyImages()

        result = backend.speedtest_pull_sources("NekroAgent", ["postgres:14"])
        backend.progress.clear()

        self.assertEqual(result["images"][0]["best_source"], "fast.test")
        self.assertTrue(backend._pull_images("NekroAgent", ["postgres:14"]))
        self.assertEqual(backend.progress[0][0], "stage")
        self.assertNotIn("speedtest", [phase for phase, _message in backend.progress])

    def test_pull_image_once_times_out_when_docker_pull_stays_silent(self):
        backend = _SilentPullDummy()
        proc = _SilentPullProc()

        # 旧实现阻塞在 readline() 上，无输出时永远走不到超时分支
        with mock.patch.object(images_module.subprocess, "Popen", return_value=proc):
            ok, lines = backend._pull_image_once(
                "NekroAgent", "kromiose/nekro-agent:latest"
            )

        self.assertFalse(ok)
        self.assertTrue(proc._killed.is_set())
        self.assertTrue(any("镜像拉取超时" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
