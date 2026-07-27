import io
import os
import subprocess
import tarfile
import tempfile
import unittest
import zlib
from unittest import mock

import requests

from core.wsl.constants import DISTRO_NAME
from core.wsl.environment import WSLEnvironmentMixin
from core.wsl.prerequisites import is_wsl_virtualization_error
from core.wsl.runtime import WSLRuntimeMixin


class _Signal:
    def __init__(self):
        self.messages = []

    def emit(self, *args):
        self.messages.append(args)


class _Config:
    def __init__(self, fail_once_key=None):
        self.fail_once_key = fail_once_key
        self.failed = False
        self.last_save_error = "simulated save failure"
        self.calls = []

    def set(self, key, value):
        self.calls.append((key, value))
        if key == self.fail_once_key and not self.failed:
            self.failed = True
            return False
        return True


class _RuntimeDummy(WSLRuntimeMixin):
    def __init__(self, *, distro_exists=False, config=None, docker_results=None):
        self.log_received = _Signal()
        self.progress_updated = _Signal()
        self.install_error = _Signal()
        self.config = config
        self.distro_exists = distro_exists
        self.guest_marker = ""
        self.fail_wsl_conf_once = False
        self.fail_guest_marker_once = False
        self.download_calls = 0
        self.docker_results = list(docker_results or [True])
        self.docker_calls = 0
        self.wsl_components_ready = True
        self.wsl_component_checks = 0

    def _creation_flags(self):
        return 0

    def _distro_exists(self):
        return self.distro_exists

    def _download_rootfs(self, _dest_path):
        self.download_calls += 1
        return True

    def _ensure_wsl2_components(self):
        self.wsl_component_checks += 1
        return self.wsl_components_ready

    @staticmethod
    def _safe_decode(data):
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data or "")

    @staticmethod
    def _clean_command_output(text, max_len=0):
        result = str(text or "").strip()
        return result[:max_len] if max_len else result

    def _write_to_wsl(self, _distro, content, wsl_path):
        if wsl_path == self._RUNTIME_GUEST_MARKER:
            if self.fail_guest_marker_once:
                self.fail_guest_marker_once = False
                raise RuntimeError("simulated guest marker failure")
            self.guest_marker = content.strip()
            return
        if wsl_path == "/etc/wsl.conf" and self.fail_wsl_conf_once:
            self.fail_wsl_conf_once = False
            raise RuntimeError("simulated wsl.conf failure")

    def _wsl_exec(self, _distro, cmd, timeout=60, user=None):
        if cmd.startswith("cat "):
            return self.guest_marker
        return ""

    def _wsl_run(self, _distro, cmd, timeout=60, user=None):
        if cmd.startswith("rm -f -- "):
            self.guest_marker = ""
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    def _install_docker_sync(self):
        result = self.docker_results[min(self.docker_calls, len(self.docker_results) - 1)]
        self.docker_calls += 1
        return result

    @staticmethod
    def _format_command_failure(action, **_kwargs):
        return action


def _successful_wsl_run(backend, calls):
    def _run(args, **_kwargs):
        calls.append(args)
        if args[:2] == ["wsl", "--import"]:
            backend.distro_exists = True
        elif args[:2] == ["wsl", "--unregister"]:
            backend.distro_exists = False
            backend.guest_marker = ""
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    return _run


class ParseWSLInstallOutcomeTests(unittest.TestCase):
    def test_exit_zero_without_reboot_marker_is_done(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome("0", "正在安装: 适用于 Linux 的 Windows 子系统\n操作成功完成。"),
            "done",
        )

    def test_exit_zero_with_chinese_reboot_marker_requires_reboot(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome("0", "请求的操作成功。直到重新启动系统前更改将不会生效。"),
            "reboot",
        )

    def test_nonzero_with_english_reboot_marker_requires_reboot(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome(
                "1", "No action was taken as a system reboot is required."
            ),
            "reboot",
        )
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome(
                "4294967295", "Please restart your computer to complete installation."
            ),
            "reboot",
        )

    def test_denied_is_fail_even_with_marker(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome("DENIED", "please reboot"),
            "fail",
        )

    def test_nonzero_without_marker_is_fail(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome("4294967295", "未知错误"),
            "fail",
        )

    def test_missing_exit_token_is_fail(self):
        self.assertEqual(WSLRuntimeMixin._parse_wsl_install_outcome("", ""), "fail")

    def test_windows_success_reboot_exit_codes_require_reboot(self):
        self.assertEqual(WSLRuntimeMixin._parse_wsl_install_outcome("3010", ""), "reboot")
        self.assertEqual(WSLRuntimeMixin._parse_wsl_install_outcome("1641", ""), "reboot")

    def test_already_installed_nonzero_exit_is_ready(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome(
                "1", "Windows Subsystem for Linux is already installed."
            ),
            "done",
        )

    def test_chinese_reboot_output_requires_reboot(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome(
                "0", "请求的操作成功。直到重新启动系统前更改将不会生效。"
            ),
            "reboot",
        )

    def test_no_reboot_output_is_not_treated_as_reboot(self):
        self.assertEqual(
            WSLRuntimeMixin._parse_wsl_install_outcome(
                "0", "Restart required: No"
            ),
            "done",
        )

    def test_elevated_preflight_explicitly_enables_both_windows_features(self):
        backend = _RuntimeDummy()
        completed = subprocess.CompletedProcess([], 0, stdout=b"NA_EXIT=0\r\n", stderr=b"")

        with mock.patch("core.wsl.runtime.subprocess.run", return_value=completed) as run, mock.patch(
            "builtins.open", mock.mock_open(read_data=b"The operation completed successfully.")
        ), mock.patch("core.wsl.runtime.os.remove"):
            outcome, _output, _detail = WSLRuntimeMixin._run_elevated_wsl_install(
                backend
            )

        ps_script = run.call_args.args[0][-1]
        script = WSLRuntimeMixin._ELEVATED_INSTALL_SCRIPT
        self.assertEqual(outcome, "done")
        self.assertIn("Microsoft-Windows-Subsystem-Linux", script)
        self.assertIn("VirtualMachinePlatform", script)
        self.assertIn("wsl.exe --install --no-distribution", script)
        # 脚本以 ascii 写盘，正文混入非 ASCII 会在运行期直接写失败
        script.encode("ascii")
        # 提权目标必须是生成的批处理文件，而非内联命令串
        self.assertRegex(ps_script, r"-FilePath '[^']+\.cmd'")
        self.assertIn("-Verb RunAs", ps_script)

    def test_elevated_preflight_detects_bomless_utf16_chinese_reboot(self):
        backend = _RuntimeDummy()
        completed = subprocess.CompletedProcess([], 0, stdout=b"NA_EXIT=0\r\n", stderr=b"")
        install_output = "请求的操作成功。直到重新启动系统前更改将不会生效。".encode(
            "utf-16-le"
        )

        with mock.patch("core.wsl.runtime.subprocess.run", return_value=completed), mock.patch(
            "builtins.open", mock.mock_open(read_data=install_output)
        ), mock.patch("core.wsl.runtime.os.remove"):
            outcome, output, _detail = WSLRuntimeMixin._run_elevated_wsl_install(
                backend
            )

        self.assertEqual(outcome, "reboot")
        self.assertIn("重新启动", output)


@unittest.skipUnless(os.name == "nt", "提权安装脚本需要 cmd.exe 才能验证")
class ElevatedInstallScriptTests(unittest.TestCase):
    """真实执行生成的批处理，锁定 dism 退出码的传出行为。

    早期实现把分支逻辑拼成单行 cmd 串，经 PowerShell -ArgumentList 转义后会
    被割裂，dism 的 3010（需重启）被吞成 0 并误判为"组件已就绪"，随后在全新
    机器上继续导入发行版并失败。
    """

    _STUB_ANCHORS = (
        "dism.exe /online /enable-feature "
        "/featurename:Microsoft-Windows-Subsystem-Linux /all /norestart",
        "dism.exe /online /enable-feature "
        "/featurename:VirtualMachinePlatform /all /norestart",
        "wsl.exe --install --no-distribution",
    )

    def _outcome_for(self, rc_wsl, rc_vmp, rc_install):
        script = WSLRuntimeMixin._ELEVATED_INSTALL_SCRIPT
        for anchor, rc in zip(self._STUB_ANCHORS, (rc_wsl, rc_vmp, rc_install)):
            self.assertIn(anchor, script)
            script = script.replace(anchor, f"cmd.exe /c exit {rc}")

        work_dir = tempfile.mkdtemp()
        base = os.path.join(work_dir, "na_elevated_probe")
        script_path = f"{base}.cmd"
        log_path = f"{base}.log"
        try:
            with open(script_path, "w", encoding="ascii", newline="\r\n") as f:
                f.write(script)
            proc = subprocess.run(
                ["cmd.exe", "/d", "/c", script_path],
                capture_output=True,
                timeout=120,
            )
            output = ""
            if os.path.exists(log_path):
                with open(log_path, "rb") as f:
                    output = WSLRuntimeMixin._decode_wsl_install_output(f.read())
            return WSLRuntimeMixin._parse_wsl_install_outcome(
                str(proc.returncode), output
            )
        finally:
            for path in (script_path, log_path):
                if os.path.exists(path):
                    os.remove(path)
            os.rmdir(work_dir)

    def test_all_components_ready_is_done(self):
        self.assertEqual(self._outcome_for(0, 0, 0), "done")

    def test_wsl_feature_reboot_code_survives(self):
        self.assertEqual(self._outcome_for(3010, 0, 0), "reboot")

    def test_virtual_machine_platform_reboot_code_survives(self):
        self.assertEqual(self._outcome_for(0, 3010, 0), "reboot")

    def test_windows_1641_reboot_code_survives(self):
        self.assertEqual(self._outcome_for(1641, 0, 0), "reboot")

    def test_dism_hard_failure_is_reported(self):
        self.assertEqual(self._outcome_for(50, 0, 0), "fail")
        self.assertEqual(self._outcome_for(0, 50, 0), "fail")

    def test_wsl_install_exit_code_is_propagated(self):
        self.assertEqual(self._outcome_for(0, 0, 3010), "reboot")
        self.assertEqual(self._outcome_for(0, 0, 1), "fail")


class WSLPrerequisiteTests(unittest.TestCase):
    def test_virtualization_errors_are_recognized(self):
        for output in (
            "Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_HYPERV_NOT_INSTALLED",
            "Error code: 0x80370102",
            "请启用“虚拟机平台”可选组件",
        ):
            with self.subTest(output=output):
                self.assertTrue(is_wsl_virtualization_error(output))

    def test_create_stops_before_download_when_components_need_reboot(self):
        backend = _RuntimeDummy()
        backend.wsl_components_ready = False

        with tempfile.TemporaryDirectory() as install_dir:
            self.assertFalse(backend.create_runtime(install_dir))

        self.assertEqual(backend.wsl_component_checks, 1)
        self.assertEqual(backend.download_calls, 0)

    def test_real_preflight_surfaces_reboot_before_creation(self):
        backend = _RuntimeDummy()

        with mock.patch.object(
            backend, "_wsl2_components_ready", return_value=False
        ), mock.patch.object(
            backend,
            "_run_elevated_wsl_install",
            return_value=("reboot", "需要重启", "安装进程退出码: 3010"),
        ):
            self.assertFalse(WSLRuntimeMixin._ensure_wsl2_components(backend))

        message = backend.install_error.messages[-1][0]
        self.assertIn("虚拟机平台", message)
        self.assertIn("重启电脑", message)

    def test_ready_components_skip_the_uac_prompt(self):
        """组件早已就绪的机器不该每次创建运行环境都被提权打断。"""
        backend = _RuntimeDummy()

        with mock.patch.object(
            backend, "_wsl2_components_ready", return_value=True
        ), mock.patch.object(backend, "_run_elevated_wsl_install") as elevated:
            self.assertTrue(WSLRuntimeMixin._ensure_wsl2_components(backend))

        elevated.assert_not_called()

    def test_components_ready_probe_is_conservative(self):
        backend = _RuntimeDummy()
        ready = subprocess.CompletedProcess([], 0, stdout=b"WSL version: 2.0", stderr=b"")
        blocked = subprocess.CompletedProcess(
            [], 0, stdout="请启用“虚拟机平台”".encode("utf-8"), stderr=b""
        )
        failed = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"")

        for label, completed, expected in (
            ("状态正常", ready, True),
            ("提示虚拟化未启用", blocked, False),
            ("wsl --status 非零退出", failed, False),
        ):
            with self.subTest(label=label):
                with mock.patch(
                    "core.wsl.runtime.subprocess.run", return_value=completed
                ):
                    self.assertIs(
                        WSLRuntimeMixin._wsl2_components_ready(backend), expected
                    )

        with self.subTest(label="wsl 不存在"):
            with mock.patch(
                "core.wsl.runtime.subprocess.run", side_effect=FileNotFoundError()
            ):
                self.assertFalse(WSLRuntimeMixin._wsl2_components_ready(backend))


class _EnvironmentDummy(WSLEnvironmentMixin):
    def __init__(self):
        self.log_received = _Signal()

    @staticmethod
    def _creation_flags():
        return 0

    @staticmethod
    def _safe_decode(data):
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data or "")

    @staticmethod
    def _format_command_failure(action, **_kwargs):
        return action

    @staticmethod
    def _distro_exists():
        return False


class WSLEnvironmentDetectionTests(unittest.TestCase):
    def test_version_fallback_does_not_hide_virtualization_failure(self):
        backend = _EnvironmentDummy()
        status = subprocess.CompletedProcess(
            [], 1, stdout=b"", stderr=b"HCS_E_HYPERV_NOT_INSTALLED"
        )
        version = subprocess.CompletedProcess([], 0, stdout=b"WSL 2.6", stderr=b"")

        with mock.patch(
            "core.wsl.environment.subprocess.run", side_effect=[status, version]
        ):
            passed, detail = backend.get_check_funcs()[0]()

        self.assertFalse(passed)
        self.assertIn("虚拟化前置条件", detail)

    def test_version_success_does_not_override_status_failure(self):
        backend = _EnvironmentDummy()
        status = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"store startup")
        version = subprocess.CompletedProcess([], 0, stdout=b"WSL 2.6", stderr=b"")

        with mock.patch(
            "core.wsl.environment.subprocess.run", side_effect=[status, version]
        ):
            passed, detail = backend.get_check_funcs()[0]()

        self.assertFalse(passed)
        self.assertIn("WSL2 检测失败", detail)


class _DownloadResponse:
    def __init__(
        self,
        chunks=None,
        *,
        error=None,
        content_encoding="",
        content_length=None,
    ):
        self._chunks = chunks or []
        self._error = error
        length = (
            sum(len(chunk) for chunk in self._chunks)
            if content_length is None
            else content_length
        )
        self.headers = {"content-length": str(length)}
        if content_encoding:
            self.headers["content-encoding"] = content_encoding

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def iter_content(self, chunk_size):
        self.chunk_size = chunk_size
        return iter(self._chunks)


def _rootfs_archive_bytes():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = b"rootfs"
        info = tarfile.TarInfo("etc/issue")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class RootfsDownloadTests(unittest.TestCase):
    @staticmethod
    def _bitflipped_archive_bytes():
        """载荷被翻转一个比特的归档：只有读满 gzip 流才会撞上 CRC 校验位。"""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            payload = b"nekro-agent-rootfs-payload" * 4096
            info = tarfile.TarInfo("etc/blob")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        data = bytearray(buffer.getvalue())
        data[len(data) // 2] ^= 0xFF
        return bytes(data)

    def test_bitflipped_archive_is_rejected_by_crc(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "rootfs.tar.gz")
            with open(path, "wb") as archive_file:
                archive_file.write(self._bitflipped_archive_bytes())
            with self.assertRaises(ValueError):
                WSLRuntimeMixin._validate_rootfs_archive(path)

    def test_truncated_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "rootfs.tar.gz")
            full = _rootfs_archive_bytes()
            with open(path, "wb") as archive_file:
                archive_file.write(full[: len(full) // 2])
            with self.assertRaises(ValueError):
                WSLRuntimeMixin._validate_rootfs_archive(path)

    def test_bitflipped_archive_falls_back_to_next_source(self):
        backend = _RuntimeDummy()
        urls = ["https://mirror-a.test/rootfs", "https://official.test/rootfs"]

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "core.wsl.runtime.ROOTFS_URLS", urls
        ), mock.patch(
            "core.wsl.runtime.requests.get",
            side_effect=[
                _DownloadResponse([self._bitflipped_archive_bytes()]),
                _DownloadResponse([_rootfs_archive_bytes()]),
            ],
        ) as request_get:
            dest_path = os.path.join(temp_dir, "rootfs.tar.gz")
            self.assertTrue(WSLRuntimeMixin._download_rootfs(backend, dest_path))
            self.assertTrue(tarfile.is_tarfile(dest_path))

        self.assertEqual([call.args[0] for call in request_get.call_args_list], urls)

    def test_validate_converts_zlib_error_to_value_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "rootfs.tar.gz")
            with open(path, "wb") as archive_file:
                archive_file.write(_rootfs_archive_bytes())
            with mock.patch(
                "core.wsl.runtime.tarfile.open",
                side_effect=zlib.error("Error -3 while decompressing data"),
            ):
                with self.assertRaises(ValueError):
                    WSLRuntimeMixin._validate_rootfs_archive(path)

    def test_validate_converts_read_oserror_to_value_error(self):
        """gzip.BadGzipFile 是 OSError 子类，不能被当成磁盘写入失败。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "rootfs.tar.gz")
            with open(path, "wb") as archive_file:
                archive_file.write(_rootfs_archive_bytes())
            with mock.patch(
                "core.wsl.runtime.tarfile.open",
                side_effect=OSError("CRC check failed"),
            ):
                with self.assertRaises(ValueError):
                    WSLRuntimeMixin._validate_rootfs_archive(path)

    def test_corrupt_archive_falls_back_to_next_source(self):
        """zlib.error 曾直接逃出下载循环、终结创建线程，而不是切换下一个源。"""
        backend = _RuntimeDummy()
        urls = ["https://mirror-a.test/rootfs", "https://official.test/rootfs"]
        real_tarfile_open = tarfile.open
        attempts = []
        # 归档字节必须在打补丁前构造：core.wsl.runtime.tarfile 就是全局
        # tarfile 模块，补丁会同时影响这里的测试辅助函数。
        archive_bytes = _rootfs_archive_bytes()

        def flaky_open(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise zlib.error("Error -3 while decompressing data")
            return real_tarfile_open(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "core.wsl.runtime.ROOTFS_URLS", urls
        ), mock.patch(
            "core.wsl.runtime.tarfile.open", side_effect=flaky_open
        ), mock.patch(
            "core.wsl.runtime.requests.get",
            side_effect=[
                _DownloadResponse([archive_bytes]),
                _DownloadResponse([archive_bytes]),
            ],
        ) as request_get:
            dest_path = os.path.join(temp_dir, "rootfs.tar.gz")
            self.assertTrue(WSLRuntimeMixin._download_rootfs(backend, dest_path))
            self.assertTrue(tarfile.is_tarfile(dest_path))

        self.assertEqual([call.args[0] for call in request_get.call_args_list], urls)

    def test_failed_download_leaves_no_partial_file(self):
        backend = _RuntimeDummy()

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "core.wsl.runtime.ROOTFS_URLS", ["https://mirror-a.test/rootfs"]
        ), mock.patch(
            "core.wsl.runtime.requests.get",
            return_value=_DownloadResponse([b"<html>mirror blocked</html>"]),
        ):
            dest_path = os.path.join(temp_dir, "rootfs.tar.gz")
            self.assertFalse(WSLRuntimeMixin._download_rootfs(backend, dest_path))
            self.assertFalse(os.path.exists(dest_path))

    def test_connection_error_reports_actionable_cause(self):
        backend = _RuntimeDummy()
        error = requests.ConnectionError(
            "HTTPSConnectionPool(host='mirror-a.test', port=443): Max retries "
            "exceeded (Caused by NameResolutionError('<urllib3.connection."
            "HTTPSConnection object>: Failed to resolve 'mirror-a.test' "
            "([Errno 11001] getaddrinfo failed)'))"
        )

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "core.wsl.runtime.ROOTFS_URLS", ["https://mirror-a.test/rootfs"]
        ), mock.patch("core.wsl.runtime.requests.get", side_effect=error):
            dest_path = os.path.join(temp_dir, "rootfs.tar.gz")
            self.assertFalse(WSLRuntimeMixin._download_rootfs(backend, dest_path))

        self.assertTrue(
            any(
                "DNS 解析失败" in message[0]
                for message in backend.install_error.messages
            )
        )

    def test_retries_domestic_sources_before_official_and_reports_success(self):
        backend = _RuntimeDummy()
        urls = [
            "https://mirror-a.test/rootfs.tar.gz",
            "https://mirror-b.test/rootfs.tar.gz",
            "https://official.test/rootfs.tar.gz",
        ]
        responses = [
            mock.Mock(side_effect=requests.ConnectionError("a failed")),
            mock.Mock(return_value=_DownloadResponse([_rootfs_archive_bytes()])),
        ]

        def get(url, **kwargs):
            response = responses.pop(0)
            return response(url, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "core.wsl.runtime.ROOTFS_URLS", urls
        ), mock.patch("core.wsl.runtime.requests.get", side_effect=get) as request_get:
            dest_path = os.path.join(temp_dir, "rootfs.tar.gz")
            self.assertTrue(WSLRuntimeMixin._download_rootfs(backend, dest_path))
            self.assertTrue(tarfile.is_tarfile(dest_path))

        self.assertEqual(
            [call.args[0] for call in request_get.call_args_list], urls[:2]
        )
        self.assertTrue(any("mirror-a.test" in msg[0] for msg in backend.install_error.messages))
        self.assertTrue(any("mirror-b.test" in msg[0] for msg in backend.log_received.messages))

    def test_http_200_html_error_page_falls_back_to_next_source(self):
        backend = _RuntimeDummy()
        urls = ["https://mirror-a.test/rootfs", "https://official.test/rootfs"]
        archive = _rootfs_archive_bytes()

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "core.wsl.runtime.ROOTFS_URLS", urls
        ), mock.patch(
            "core.wsl.runtime.requests.get",
            side_effect=[
                _DownloadResponse([b"<html>mirror blocked</html>"]),
                _DownloadResponse([archive]),
            ],
        ) as request_get:
            dest_path = os.path.join(temp_dir, "rootfs.tar.gz")
            self.assertTrue(WSLRuntimeMixin._download_rootfs(backend, dest_path))

        self.assertEqual(
            [call.args[0] for call in request_get.call_args_list], urls
        )
        self.assertTrue(
            any("内容校验失败" in message[0] for message in backend.install_error.messages)
        )

    def test_content_encoding_length_does_not_reject_decoded_body(self):
        backend = _RuntimeDummy()
        archive = _rootfs_archive_bytes()

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "core.wsl.runtime.ROOTFS_URLS", ["https://mirror.test/rootfs"]
        ), mock.patch(
            "core.wsl.runtime.requests.get",
            return_value=_DownloadResponse(
                [archive], content_encoding="gzip", content_length=12
            ),
        ) as request_get:
            dest_path = os.path.join(temp_dir, "rootfs.tar.gz")
            self.assertTrue(WSLRuntimeMixin._download_rootfs(backend, dest_path))

        request_headers = request_get.call_args.kwargs["headers"]
        self.assertEqual(request_headers["Accept-Encoding"], "identity")

    def test_all_failures_list_every_attempted_source(self):
        backend = _RuntimeDummy()
        urls = ["https://mirror-a.test/rootfs", "https://official.test/rootfs"]
        connection_error = requests.ConnectionError("offline")

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "core.wsl.runtime.ROOTFS_URLS", urls
        ), mock.patch(
            "core.wsl.runtime.requests.get", side_effect=connection_error
        ):
            dest_path = os.path.join(temp_dir, "rootfs.tar.gz")
            self.assertFalse(WSLRuntimeMixin._download_rootfs(backend, dest_path))

        final_error = backend.install_error.messages[-1][0]
        self.assertIn("已依次尝试", final_error)
        self.assertIn("mirror-a.test", final_error)
        self.assertIn("official.test", final_error)


class CreateRuntimeRecoveryTests(unittest.TestCase):
    def test_import_virtualization_failure_has_actionable_guidance_and_uses_wsl2(self):
        backend = _RuntimeDummy()
        import_failure = subprocess.CompletedProcess(
            [],
            1,
            stdout=b"",
            stderr=b"Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_HYPERV_NOT_INSTALLED",
        )

        with tempfile.TemporaryDirectory() as install_dir, mock.patch(
            "core.wsl.runtime.subprocess.run", return_value=import_failure
        ) as run:
            self.assertFalse(backend.create_runtime(install_dir))

        import_args = run.call_args_list[0].args[0]
        self.assertEqual(import_args[-2:], ["--version", "2"])
        message = backend.install_error.messages[-1][0]
        self.assertIn("wsl --install --no-distribution", message)
        self.assertIn("BIOS/UEFI", message)
        self.assertIn("重启电脑", message)

    @mock.patch("core.wsl.runtime.time.sleep")
    def test_retry_after_wsl_conf_failure_skips_second_import(self, _sleep):
        backend = _RuntimeDummy()
        backend.fail_wsl_conf_once = True
        subprocess_calls = []

        with tempfile.TemporaryDirectory() as install_dir, mock.patch(
            "core.wsl.runtime.subprocess.run",
            side_effect=_successful_wsl_run(backend, subprocess_calls),
        ):
            self.assertFalse(backend.create_runtime(install_dir))
            self.assertTrue(os.path.exists(backend._runtime_install_marker_path(install_dir)))

            self.assertTrue(backend.create_runtime(install_dir))
            self.assertFalse(os.path.exists(backend._runtime_install_marker_path(install_dir)))

        import_calls = [args for args in subprocess_calls if args[:2] == ["wsl", "--import"]]
        self.assertEqual(len(import_calls), 1)
        self.assertEqual(backend.download_calls, 1)
        self.assertEqual(backend.docker_calls, 1)

    @mock.patch("core.wsl.runtime.time.sleep")
    def test_retry_after_config_save_failure_continues_existing_partial_distro(self, _sleep):
        config = _Config(fail_once_key="wsl_install_dir")
        backend = _RuntimeDummy(config=config)
        subprocess_calls = []

        with tempfile.TemporaryDirectory() as install_dir, mock.patch(
            "core.wsl.runtime.subprocess.run",
            side_effect=_successful_wsl_run(backend, subprocess_calls),
        ):
            self.assertFalse(backend.create_runtime(install_dir))
            self.assertTrue(backend.create_runtime(install_dir))

        import_calls = [args for args in subprocess_calls if args[:2] == ["wsl", "--import"]]
        self.assertEqual(len(import_calls), 1)
        self.assertEqual(backend.download_calls, 1)
        self.assertEqual(backend.docker_calls, 1)
        self.assertGreaterEqual(config.calls.count(("wsl_install_dir", install_dir)), 2)

    @mock.patch("core.wsl.runtime.time.sleep")
    def test_retry_after_docker_failure_reuses_matching_partial_distro(self, _sleep):
        backend = _RuntimeDummy(docker_results=[False, True])
        subprocess_calls = []

        with tempfile.TemporaryDirectory() as install_dir, mock.patch(
            "core.wsl.runtime.subprocess.run",
            side_effect=_successful_wsl_run(backend, subprocess_calls),
        ):
            self.assertFalse(backend.create_runtime(install_dir))
            self.assertTrue(backend.create_runtime(install_dir))

        import_calls = [args for args in subprocess_calls if args[:2] == ["wsl", "--import"]]
        self.assertEqual(len(import_calls), 1)
        self.assertEqual(backend.docker_calls, 2)

    def test_existing_unmarked_distro_is_not_reused_or_reimported(self):
        backend = _RuntimeDummy(distro_exists=True)

        with tempfile.TemporaryDirectory() as install_dir, mock.patch(
            "core.wsl.runtime.subprocess.run"
        ) as run:
            self.assertFalse(backend.create_runtime(install_dir))

        run.assert_not_called()
        self.assertEqual(backend.download_calls, 0)
        self.assertEqual(backend.docker_calls, 0)
        message = backend.install_error.messages[-1][0]
        self.assertIn(DISTRO_NAME, message)
        self.assertIn("不会再次导入或继续配置", message)

    @mock.patch("core.wsl.runtime.time.sleep")
    def test_guest_marker_failure_does_not_unregister_unproven_distro(self, _sleep):
        backend = _RuntimeDummy()
        backend.fail_guest_marker_once = True
        subprocess_calls = []

        with tempfile.TemporaryDirectory() as install_dir, mock.patch(
            "core.wsl.runtime.subprocess.run",
            side_effect=_successful_wsl_run(backend, subprocess_calls),
        ):
            self.assertFalse(backend.create_runtime(install_dir))
            self.assertTrue(backend.distro_exists)

        import_calls = [args for args in subprocess_calls if args[:2] == ["wsl", "--import"]]
        unregister_calls = [
            args for args in subprocess_calls if args[:2] == ["wsl", "--unregister"]
        ]
        self.assertEqual(len(import_calls), 1)
        self.assertEqual(unregister_calls, [])
        self.assertTrue(
            any(
                "不会自动注销" in message[0]
                for message in backend.install_error.messages
            )
        )

    def test_import_false_negative_never_unregisters_existing_unmarked_distro(self):
        backend = _RuntimeDummy()
        import_failure = subprocess.CompletedProcess(
            [],
            1,
            stdout=b"",
            stderr=b"A distribution with the supplied name already exists.\n",
        )

        with tempfile.TemporaryDirectory() as install_dir, mock.patch.object(
            backend,
            "_distro_exists",
            side_effect=[False, True],
        ), mock.patch(
            "core.wsl.runtime.subprocess.run",
            return_value=import_failure,
        ) as run:
            self.assertFalse(backend.create_runtime(install_dir))

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len([args for args in commands if args[:2] == ["wsl", "--import"]]), 1)
        self.assertEqual([args for args in commands if args[:2] == ["wsl", "--unregister"]], [])

    def test_discard_refuses_mismatched_guest_marker(self):
        backend = _RuntimeDummy(distro_exists=True)
        backend.guest_marker = "another-install-token"

        with mock.patch("core.wsl.runtime.subprocess.run") as run:
            self.assertFalse(backend._discard_failed_runtime_import("current-install-token"))

        run.assert_not_called()
        message = backend.install_error.messages[-1][0]
        self.assertIn("恢复标记与本次创建任务不匹配", message)


if __name__ == "__main__":
    unittest.main()
