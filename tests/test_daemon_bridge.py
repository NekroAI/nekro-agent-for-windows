import io
import os
import socket
import subprocess
import sys
import tempfile
import unittest

from core.daemon_bridge import (
    BRIDGE_MAGIC,
    BRIDGE_SCRIPT,
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_HEADER,
    FRAME_LOG,
    FRAME_OPEN,
    encode_frame,
    read_frame,
    wait_for_magic,
)


class DaemonBridgeFrameTests(unittest.TestCase):
    def test_frame_roundtrip(self):
        payload = b"\x00\x01hello\xff"
        stream = io.BytesIO(
            encode_frame(FRAME_OPEN, 7)
            + encode_frame(FRAME_DATA, 7, payload)
            + encode_frame(FRAME_CLOSE, 7)
        )

        self.assertEqual(read_frame(stream), (FRAME_OPEN, 7, b""))
        self.assertEqual(read_frame(stream), (FRAME_DATA, 7, payload))
        self.assertEqual(read_frame(stream), (FRAME_CLOSE, 7, b""))
        self.assertIsNone(read_frame(stream))

    def test_read_frame_rejects_oversized_payload(self):
        header = FRAME_HEADER.pack(FRAME_DATA, 1, 64 * 1024 * 1024)
        self.assertIsNone(read_frame(io.BytesIO(header + b"x" * 16)))

    def test_read_frame_returns_none_on_truncated_payload(self):
        frame = encode_frame(FRAME_DATA, 3, b"abcdef")
        self.assertIsNone(read_frame(io.BytesIO(frame[:-2])))

    def test_encode_frame_rejects_oversized_payload(self):
        with self.assertRaises(ValueError):
            encode_frame(FRAME_DATA, 1, b"x" * (2 * 1024 * 1024))

    def test_wait_for_magic_skips_startup_noise(self):
        noisy = "wsl: 检测到 localhost 代理配置\r\n".encode("utf-16-le") + BRIDGE_MAGIC
        self.assertTrue(wait_for_magic(io.BytesIO(noisy)))

    def test_wait_for_magic_fails_without_magic(self):
        self.assertFalse(wait_for_magic(io.BytesIO(b"no magic here")))

    def test_bridge_script_compiles(self):
        compile(BRIDGE_SCRIPT, "<daemon_bridge>", "exec")


class DaemonBridgeScriptEndToEndTests(unittest.TestCase):
    """用本机 Python 直接运行桥接脚本，验证 stdio 帧协议全链路。"""

    def test_bridge_script_relays_tcp_over_stdio(self):
        script_path = ""
        proc = None
        client = None
        try:
            fd, script_path = tempfile.mkstemp(suffix=".py")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(BRIDGE_SCRIPT)
            proc = subprocess.Popen(
                [sys.executable, "-u", script_path, "0", "127.0.0.1"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            assert proc.stdin is not None and proc.stdout is not None

            self.assertTrue(wait_for_magic(proc.stdout))

            frame = read_frame(proc.stdout)
            self.assertIsNotNone(frame)
            assert frame is not None
            ftype, _sid, payload = frame
            self.assertEqual(ftype, FRAME_LOG)
            text = payload.decode("utf-8")
            self.assertIn("listening", text)
            port = int(text.rsplit(":", 1)[1])

            client = socket.create_connection(("127.0.0.1", port), timeout=5)
            client.settimeout(5)

            frame = read_frame(proc.stdout)
            assert frame is not None
            self.assertEqual(frame[0], FRAME_OPEN)
            sid = frame[1]

            client.sendall(b"hello-from-container")
            frame = read_frame(proc.stdout)
            self.assertEqual(frame, (FRAME_DATA, sid, b"hello-from-container"))

            proc.stdin.write(encode_frame(FRAME_DATA, sid, b"hello-from-windows"))
            proc.stdin.flush()
            self.assertEqual(client.recv(1024), b"hello-from-windows")

            client.close()
            client = None
            frame = read_frame(proc.stdout)
            self.assertEqual(frame, (FRAME_CLOSE, sid, b""))

            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=10), 0)
        finally:
            if client is not None:
                client.close()
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                for stream in (proc.stdin, proc.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            if script_path and os.path.exists(script_path):
                os.remove(script_path)


if __name__ == "__main__":
    unittest.main()
