"""WSL 内 SOCKS 桥接通道。

Nekro Agent 容器通过 `host.docker.internal:18082`（在本项目的 docker-in-WSL 架构下
解析为 WSL 内 docker0 网关）访问 daemon 控制通道；而 daemon facade 的 SOCKS/HTTP
监听都在 Windows 进程内。默认的 WSL2 NAT 网络下，容器发往 WSL 网关的流量不会
到达 Windows 进程，Windows 防火墙也可能拦截 WSL 到 Windows 的入站连接。

本模块在 WSL 发行版内运行一个由启动器托管的小型桥接进程：

    容器 -> WSL 0.0.0.0:18082 (桥接进程) -> wsl.exe stdio 管道 -> Windows 127.0.0.1:18082 (SOCKS)

桥接进程与启动器只通过 stdio 帧协议通信，不建立任何 WSL 到 Windows 的 TCP 连接，
因此不依赖防火墙规则，也不需要探测随每次开机变化的 NAT 网关地址。启动器退出时
stdio 管道关闭，桥接进程读到 EOF 后自行退出，生命周期与 facade 严格一致。
"""

import shlex
import socket
import struct
import subprocess
import threading
from collections import deque

from core.wsl.constants import DISTRO_NAME

FRAME_OPEN = 1
FRAME_DATA = 2
FRAME_CLOSE = 3
FRAME_LOG = 4

FRAME_HEADER = struct.Struct("!BII")
MAX_FRAME_PAYLOAD = 1024 * 1024
BRIDGE_MAGIC = b"NA-BRIDGE-V1\n"
BRIDGE_REMOTE_DIR = "/root/.nekro_launcher"
BRIDGE_REMOTE_PATH = f"{BRIDGE_REMOTE_DIR}/daemon_bridge.py"

# WSL 侧桥接进程源码。只用 python3 标准库；stdout 专用于帧协议，
# 启动后先输出 BRIDGE_MAGIC，让 Windows 侧跳过 wsl.exe 可能混入的启动噪音。
BRIDGE_SCRIPT = r'''
import socket
import struct
import sys
import threading

FRAME_OPEN = 1
FRAME_DATA = 2
FRAME_CLOSE = 3
FRAME_LOG = 4
HEADER = struct.Struct("!BII")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18082
    host = sys.argv[2] if len(sys.argv) > 2 else "0.0.0.0"
    out = sys.stdout.buffer
    out_lock = threading.Lock()
    conns = {}
    conns_lock = threading.Lock()

    def send_frame(ftype, sid, payload=b""):
        with out_lock:
            out.write(HEADER.pack(ftype, sid, len(payload)))
            if payload:
                out.write(payload)
            out.flush()

    def log(message):
        send_frame(FRAME_LOG, 0, message.encode("utf-8", "replace"))

    out.write(b"NA-BRIDGE-V1\n")
    out.flush()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((host, port))
    except OSError as exc:
        log("bind_failed %s:%s %s" % (host, port, exc))
        sys.exit(3)
    server.listen(16)
    log("listening %s:%s" % server.getsockname()[:2])

    def close_conn(sid):
        with conns_lock:
            conn = conns.pop(sid, None)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        return conn

    def downstream(sid, conn):
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                send_frame(FRAME_DATA, sid, data)
        except OSError:
            pass
        finally:
            if close_conn(sid) is not None:
                send_frame(FRAME_CLOSE, sid)

    def acceptor():
        next_sid = 0
        while True:
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            next_sid += 1
            sid = next_sid
            with conns_lock:
                conns[sid] = conn
            send_frame(FRAME_OPEN, sid)
            threading.Thread(target=downstream, args=(sid, conn), daemon=True).start()

    threading.Thread(target=acceptor, daemon=True).start()

    stdin = sys.stdin.buffer

    def read_exact(size):
        buf = b""
        while len(buf) < size:
            chunk = stdin.read(size - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    while True:
        header = read_exact(HEADER.size)
        if header is None:
            break
        ftype, sid, length = HEADER.unpack(header)
        payload = b""
        if length:
            payload = read_exact(length)
            if payload is None:
                break
        if ftype == FRAME_DATA:
            with conns_lock:
                conn = conns.get(sid)
            if conn is not None:
                try:
                    conn.sendall(payload)
                except OSError:
                    pass
        elif ftype == FRAME_CLOSE:
            close_conn(sid)

    # stdin EOF：启动器已退出，结束全部连接线程
    try:
        server.close()
    except OSError:
        pass


if __name__ == "__main__":
    main()
'''


def encode_frame(ftype, stream_id, payload=b""):
    if len(payload) > MAX_FRAME_PAYLOAD:
        raise ValueError(f"帧负载超过上限: {len(payload)}")
    return FRAME_HEADER.pack(ftype, stream_id, len(payload)) + payload


def read_exact(stream, size):
    """从流中读满 size 字节；EOF 或流被关闭返回 None。"""
    buf = b""
    while len(buf) < size:
        try:
            chunk = stream.read(size - len(buf))
        except (OSError, ValueError):
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def read_frame(stream):
    """读取一个完整帧，返回 (ftype, stream_id, payload)；EOF 或帧损坏返回 None。"""
    header = read_exact(stream, FRAME_HEADER.size)
    if header is None:
        return None
    ftype, stream_id, length = FRAME_HEADER.unpack(header)
    if length > MAX_FRAME_PAYLOAD:
        return None
    payload = b""
    if length:
        payload = read_exact(stream, length)
        if payload is None:
            return None
    return ftype, stream_id, payload


def wait_for_magic(stream, magic=BRIDGE_MAGIC, max_noise=8192):
    """丢弃 wsl.exe 启动噪音直到读到桥接进程的握手串；失败返回 False。"""
    window = b""
    consumed = 0
    while consumed < max_noise + len(magic):
        try:
            chunk = stream.read(1)
        except (OSError, ValueError):
            return False
        if not chunk:
            return False
        window = (window + chunk)[-len(magic):]
        consumed += 1
        if window == magic:
            return True
    return False


class DaemonSocksBridge:
    """在 WSL 内维护桥接进程，把容器侧连接经 stdio 转回 Windows 本机 SOCKS 监听。"""

    def __init__(self, backend, *, listen_port, target_host, target_port):
        self.backend = backend
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._proc = None
        self._proc_lock = threading.Lock()
        self._stdin_lock = threading.Lock()
        self._streams = {}
        self._streams_lock = threading.Lock()
        self._watchdog_thread = None
        self._last_wait_reason = ""
        self._stderr_tail = deque(maxlen=60)

    def start(self):
        if self._watchdog_thread is not None:
            return
        self._stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog,
            name="LauncherDaemonBridge",
            daemon=True,
        )
        self._watchdog_thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        with self._proc_lock:
            proc = self._proc
            self._proc = None
        if proc is not None:
            self._terminate_proc(proc)
        self._close_all_streams()

    def poke(self):
        """外部状态变化（如发行版刚部署完成）时提前唤醒重试。"""
        self._wake.set()

    def _log(self, message, level="info"):
        emitter = getattr(self.backend, "log_received", None)
        if emitter is not None:
            emitter.emit(f"[daemon 桥接] {message}", level)

    def _log_wait_reason(self, reason):
        if reason != self._last_wait_reason:
            self._last_wait_reason = reason
            self._log(reason, "debug")

    def _watchdog(self):
        backoff = 5
        while not self._stop.is_set():
            proc = None
            try:
                proc = self._launch_once()
            except Exception as exc:
                self._log(f"桥接进程启动失败: {type(exc).__name__}: {exc}", "warn")
            if proc is not None:
                self._last_wait_reason = ""
                backoff = 5
                self._pump(proc)
                self._close_all_streams()
                if self._stop.is_set():
                    self._terminate_proc(proc)
                    return
                stderr_tail = self._collect_exit(proc)
                detail = f"，输出: {stderr_tail}" if stderr_tail else ""
                self._log(f"WSL 桥接进程退出，将自动重启{detail}", "warn")
            self._wake.clear()
            self._wake.wait(backoff)
            if self._stop.is_set():
                return
            backoff = min(backoff * 2, 60)

    def _launch_once(self):
        if not self.backend.runtime_exists():
            self._log_wait_reason("等待 WSL 发行版可用，WebUI 在线更新通道暂不可用")
            return None
        python_path = self.backend._wsl_exec(
            DISTRO_NAME, "command -v python3", timeout=15
        ).strip()
        if not python_path:
            self._log_wait_reason(
                "WSL 发行版内缺少 python3，无法启动 WebUI 在线更新通道"
            )
            return None
        self.backend._wsl_exec_checked(
            DISTRO_NAME,
            f"mkdir -p {shlex.quote(BRIDGE_REMOTE_DIR)}",
            timeout=15,
        )
        self.backend._write_to_wsl(DISTRO_NAME, BRIDGE_SCRIPT, BRIDGE_REMOTE_PATH)
        proc = subprocess.Popen(
            [
                "wsl",
                "-d",
                DISTRO_NAME,
                "--",
                "python3",
                "-u",
                BRIDGE_REMOTE_PATH,
                str(self.listen_port),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=self.backend._creation_flags(),
        )
        with self._proc_lock:
            if self._stop.is_set():
                self._terminate_proc(proc)
                return None
            self._proc = proc
        self._stderr_tail.clear()
        threading.Thread(
            target=self._drain_stderr_loop,
            args=(proc,),
            name="LauncherDaemonBridgeStderr",
            daemon=True,
        ).start()
        assert proc.stdout is not None
        # wsl.exe 卡死时握手读会无限阻塞，用定时器兜底杀掉以便 watchdog 重试
        handshake_timer = threading.Timer(30, lambda: self._terminate_proc(proc))
        handshake_timer.daemon = True
        handshake_timer.start()
        try:
            handshake_ok = wait_for_magic(proc.stdout)
        finally:
            handshake_timer.cancel()
        if not handshake_ok:
            self._log("桥接进程握手失败，未收到协议头", "warn")
            self._terminate_proc(proc)
            with self._proc_lock:
                if self._proc is proc:
                    self._proc = None
            return None
        self._log(
            f"WSL 桥接通道已启动: 容器 host.docker.internal:{self.listen_port} "
            f"-> Windows {self.target_host}:{self.target_port}",
            "debug",
        )
        return proc

    def _pump(self, proc):
        stdout = proc.stdout
        assert stdout is not None
        while not self._stop.is_set():
            frame = read_frame(stdout)
            if frame is None:
                break
            ftype, stream_id, payload = frame
            if ftype == FRAME_LOG:
                self._log(payload.decode("utf-8", "replace"), "debug")
            elif ftype == FRAME_OPEN:
                self._open_stream(proc, stream_id)
            elif ftype == FRAME_DATA:
                self._stream_data(stream_id, payload)
            elif ftype == FRAME_CLOSE:
                self._close_stream(stream_id, notify_bridge=False)
        with self._proc_lock:
            if self._proc is proc:
                self._proc = None

    def _open_stream(self, proc, stream_id):
        try:
            sock = socket.create_connection(
                (self.target_host, self.target_port), timeout=5
            )
        except OSError as exc:
            self._log(f"本机 SOCKS 连接失败: {exc}", "warn")
            self._send_frame(proc, FRAME_CLOSE, stream_id)
            return
        with self._streams_lock:
            self._streams[stream_id] = sock
        threading.Thread(
            target=self._upstream_reader,
            args=(proc, stream_id, sock),
            name=f"LauncherDaemonBridge-{stream_id}",
            daemon=True,
        ).start()

    def _stream_data(self, stream_id, payload):
        with self._streams_lock:
            sock = self._streams.get(stream_id)
        if sock is None:
            return
        try:
            sock.sendall(payload)
        except OSError:
            self._close_stream(stream_id, notify_bridge=True)

    def _upstream_reader(self, proc, stream_id, sock):
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                self._send_frame(proc, FRAME_DATA, stream_id, data)
        except OSError:
            pass
        finally:
            removed = self._remove_stream(stream_id)
            if removed is not None:
                self._send_frame(proc, FRAME_CLOSE, stream_id)

    def _remove_stream(self, stream_id):
        with self._streams_lock:
            sock = self._streams.pop(stream_id, None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        return sock

    def _close_stream(self, stream_id, *, notify_bridge):
        sock = self._remove_stream(stream_id)
        if sock is not None and notify_bridge:
            with self._proc_lock:
                proc = self._proc
            if proc is not None:
                self._send_frame(proc, FRAME_CLOSE, stream_id)

    def _close_all_streams(self):
        with self._streams_lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for sock in streams:
            try:
                sock.close()
            except OSError:
                pass

    def _send_frame(self, proc, ftype, stream_id, payload=b""):
        stdin = proc.stdin
        if stdin is None:
            return
        data = encode_frame(ftype, stream_id, payload)
        try:
            with self._stdin_lock:
                stdin.write(data)
                stdin.flush()
        except (OSError, ValueError):
            return

    def _drain_stderr_loop(self, proc):
        """持续排空 stderr，防止 64KB 管道缓冲写满阻塞桥接进程；留存尾部供诊断。"""
        stderr = proc.stderr
        if stderr is None:
            return
        while True:
            try:
                line = stderr.readline()
            except (OSError, ValueError):
                return
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text:
                self._stderr_tail.append(text)

    def _collect_exit(self, proc, limit=800):
        """等待桥接进程退出并整理 stderr 尾部用于诊断日志。"""
        try:
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            self._terminate_proc(proc)
            return ""
        raw = "\n".join(self._stderr_tail)
        self._terminate_proc(proc)
        cleaner = getattr(self.backend, "_clean_stderr", None)
        if cleaner is not None:
            return cleaner(raw, max_len=limit)
        return raw[:limit]

    def _terminate_proc(self, proc):
        for closer in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if closer is not None:
                    closer.close()
            except (OSError, ValueError):
                pass
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass
