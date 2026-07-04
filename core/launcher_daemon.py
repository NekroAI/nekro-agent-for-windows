import hashlib
import hmac
import json
import os
import re
import secrets
import select
import shlex
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core.daemon_bridge import DaemonSocksBridge
from core.wsl.constants import DISTRO_NAME


PROTOCOL_VERSION = "na-tools.daemon.v1"
PROVIDER = "na-windows-launcher"
API_BASE = "http://na-tools.local/v1"
SOCKS_URL = "socks5h://host.docker.internal:18082"
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 18081
# SOCKS 只绑定本机回环；容器侧流量由 WSL 内桥接进程经 stdio 转发进来，
# 不需要（也不应该）把 SOCKS 暴露到非回环接口。
SOCKS_HOST = "127.0.0.1"
SOCKS_PORT = 18082
MAX_REQUEST_BODY_BYTES = 1024 * 1024
FINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}
ACTIVE_JOB_STATUSES = {"queued", "running", "cancel_requested"}
BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.tar\.gz$")


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _error(code, message, details=None):
    err = {"code": code, "message": message}
    if details:
        err["details"] = details
    return {"error": err}


@dataclass
class DaemonBinding:
    launcher_inst_id: str
    instance_id: str
    token: str
    data_dir: str
    deploy_dir: str
    compose_file: str
    env_file: str
    channel: str
    nekro_port: int
    instance_name: str


class DaemonJob:
    def __init__(self, job_id, job_type, instance_id, request, on_change=None):
        self.job_id = job_id
        self.type = job_type
        self.instance_id = instance_id
        self.request = request
        self.status = "queued"
        self.phase = "validate_instance"
        self.message = "任务已加入队列"
        self.progress = {"current": 0, "total": 0, "label": ""}
        self.created_at = _utc_now()
        self.started_at = None
        self.finished_at = None
        self.exit_code = None
        self.error = None
        self.result = None
        self.logs = []
        self._next_seq = 1
        self._condition = threading.Condition()
        self._on_change = on_change

    def _notify_change(self):
        if self._on_change is None:
            return
        try:
            self._on_change(self)
        except OSError:
            return

    @classmethod
    def from_record(cls, record, logs=None, on_change=None):
        job = cls(
            str(record.get("job_id") or ""),
            str(record.get("type") or "update"),
            str(record.get("instance_id") or ""),
            record.get("request") if isinstance(record.get("request"), dict) else {},
            on_change=on_change,
        )
        job.status = str(record.get("status") or "failed")
        job.phase = str(record.get("phase") or "finished")
        job.message = str(record.get("message") or "")
        progress = record.get("progress")
        if isinstance(progress, dict):
            job.progress = progress
        job.created_at = record.get("created_at")
        job.started_at = record.get("started_at")
        job.finished_at = record.get("finished_at")
        job.exit_code = record.get("exit_code")
        job.error = record.get("error")
        job.result = record.get("result")
        job.logs = logs or []
        job._next_seq = max((int(item.get("seq", 0)) for item in job.logs), default=0) + 1
        return job

    def snapshot(self):
        with self._condition:
            return {
                "job_id": self.job_id,
                "type": self.type,
                "instance_id": self.instance_id,
                "status": self.status,
                "phase": self.phase,
                "message": self.message,
                "progress": dict(self.progress),
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "exit_code": self.exit_code,
                "error": self.error,
                "result": self.result,
            }

    def log_snapshot(self, limit=200, after_seq=0):
        with self._condition:
            logs = [item for item in self.logs if item["seq"] > after_seq]
            if limit > 0:
                logs = logs[-limit:]
            next_after_seq = logs[-1]["seq"] if logs else after_seq
            return {"job_id": self.job_id, "logs": logs, "next_after_seq": next_after_seq}

    def add_log(self, line, level="info", stream="system"):
        with self._condition:
            entry = {
                "seq": self._next_seq,
                "ts": _utc_now(),
                "level": level,
                "stream": stream,
                "line": str(line),
            }
            self._next_seq += 1
            self.logs.append(entry)
            if len(self.logs) > 1000:
                self.logs = self.logs[-1000:]
            self._condition.notify_all()
        self._notify_change()
        return entry

    def state_record(self):
        with self._condition:
            return {
                **self.snapshot(),
                "request": self.request,
            }

    def log_records(self):
        with self._condition:
            return list(self.logs)

    def start(self, phase="validate_instance", message="开始执行任务"):
        with self._condition:
            if self.status in FINAL_JOB_STATUSES:
                return False
            self.status = "running"
            self.phase = phase
            self.message = message
            self.started_at = self.started_at or _utc_now()
            self._condition.notify_all()
        self.add_log(message)
        return True

    def set_progress(self, phase, current, total, label):
        with self._condition:
            self.phase = phase
            self.message = label
            self.progress = {"current": current, "total": total, "label": label}
            self._condition.notify_all()
        self.add_log(label)

    def request_cancel(self):
        with self._condition:
            if self.status in FINAL_JOB_STATUSES:
                return False
            if self.status == "queued":
                self.status = "cancelled"
                self.phase = "finished"
                self.message = "任务已取消"
                self.finished_at = _utc_now()
                self.exit_code = 130
                self._condition.notify_all()
                log_message = "任务已取消"
            else:
                self.status = "cancel_requested"
                self.message = "已请求取消，任务将在安全阶段边界停止"
                self._condition.notify_all()
                log_message = self.message
        self.add_log(log_message, level="warning")
        return True

    def is_cancel_requested(self):
        with self._condition:
            return self.status == "cancel_requested"

    def cancel(self, message="任务已取消"):
        with self._condition:
            if self.status in FINAL_JOB_STATUSES:
                return
            self.status = "cancelled"
            self.phase = "finished"
            self.message = message
            self.finished_at = _utc_now()
            self.exit_code = 130
            self._condition.notify_all()
        self.add_log(message, level="warning")

    def succeed(self, message, result=None):
        with self._condition:
            if self.status in FINAL_JOB_STATUSES:
                return
            self.status = "succeeded"
            self.phase = "finished"
            self.message = message
            self.progress = {"current": 1, "total": 1, "label": message}
            self.finished_at = _utc_now()
            self.exit_code = 0
            self.result = result or {}
            self._condition.notify_all()
        self.add_log(message)

    def fail(self, code, message, details=None):
        with self._condition:
            if self.status in FINAL_JOB_STATUSES:
                return
            self.status = "failed"
            self.message = message
            self.finished_at = _utc_now()
            self.exit_code = 1
            self.error = {"code": code, "message": message, "details": details or {}}
            self._condition.notify_all()
        self.add_log(message, level="error")

    def wait_for_event(self, after_seq, timeout=30):
        deadline = time.time() + timeout
        with self._condition:
            while time.time() < deadline:
                if self.logs and self.logs[-1]["seq"] > after_seq:
                    return True
                if self.status in FINAL_JOB_STATUSES:
                    return True
                remaining = max(0.1, deadline - time.time())
                self._condition.wait(min(remaining, 1.0))
        return False


class JobStore:
    def __init__(self, storage_dir=None, retention_days=7):
        self._jobs = {}
        self._client_requests = {}
        self._lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self.storage_dir = storage_dir
        self.retention_seconds = max(1, retention_days) * 24 * 60 * 60
        if self.storage_dir:
            os.makedirs(self.storage_dir, exist_ok=True)
            self._load_jobs()
            self._prune_old_jobs()

    def create_job(self, job_type, instance_id, request, id_prefix):
        client_request_id = str(request.get("client_request_id") or "").strip()
        request_key = f"{job_type}:{instance_id}:{client_request_id}"
        with self._lock:
            if client_request_id and request_key in self._client_requests:
                existing_id = self._client_requests[request_key]
                if existing_id in self._jobs:
                    return self._jobs[existing_id], False
                self._client_requests.pop(request_key, None)
            for job in self._jobs.values():
                if job.instance_id == instance_id and job.status in ACTIVE_JOB_STATUSES:
                    return None, False
            job_id = f"{id_prefix}_" + secrets.token_hex(12)
            job = DaemonJob(job_id, job_type, instance_id, request, on_change=self._persist_job)
            self._jobs[job_id] = job
            if client_request_id:
                self._client_requests[request_key] = job_id
            self._persist_job(job)
            return job, True

    def create_update_job(self, instance_id, request):
        return self.create_job("update", instance_id, request, "upd")

    def create_backup_job(self, instance_id, request):
        return self.create_job("backup", instance_id, request, "upd")

    def create_restore_job(self, instance_id, request):
        return self.create_job("restore", instance_id, request, "upd")

    def active_for_instance(self, instance_id):
        with self._lock:
            for job in self._jobs.values():
                if job.instance_id == instance_id and job.status in ACTIVE_JOB_STATUSES:
                    return job
            return None

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def _safe_job_name(self, job_id):
        return re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)

    def _job_state_path(self, job_id):
        if not self.storage_dir:
            return ""
        return os.path.join(self.storage_dir, f"{self._safe_job_name(job_id)}.json")

    def _job_log_path(self, job_id):
        if not self.storage_dir:
            return ""
        return os.path.join(self.storage_dir, f"{self._safe_job_name(job_id)}.log")

    def _persist_job(self, job):
        if not self.storage_dir:
            return
        with self._persist_lock:
            state_path = self._job_state_path(job.job_id)
            log_path = self._job_log_path(job.job_id)
            tmp_suffix = f"{os.getpid()}.{threading.get_ident()}.tmp"
            state_tmp = f"{state_path}.{tmp_suffix}"
            log_tmp = f"{log_path}.{tmp_suffix}"
            with open(state_tmp, "w", encoding="utf-8") as f:
                json.dump(job.state_record(), f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(state_tmp, state_path)
            with open(log_tmp, "w", encoding="utf-8") as f:
                for entry in job.log_records():
                    f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(log_tmp, log_path)

    def _load_jobs(self):
        if not self.storage_dir:
            return
        for name in os.listdir(self.storage_dir):
            if not name.endswith(".json"):
                continue
            state_path = os.path.join(self.storage_dir, name)
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            job_id = str(record.get("job_id") or name[:-5])
            logs = self._load_job_logs(job_id)
            job = DaemonJob.from_record(record, logs=logs, on_change=self._persist_job)
            if not job.job_id:
                continue
            if job.status in ACTIVE_JOB_STATUSES:
                job.fail(
                    "daemon_unavailable",
                    "任务未完成时 Windows 启动器 daemon 已重启，任务状态已失效",
                )
            self._jobs[job.job_id] = job
            client_request_id = str(job.request.get("client_request_id") or "").strip()
            if client_request_id:
                key = f"{job.type}:{job.instance_id}:{client_request_id}"
                self._client_requests[key] = job.job_id

    def _load_job_logs(self, job_id):
        log_path = self._job_log_path(job_id)
        logs = []
        if not log_path or not os.path.exists(log_path):
            return logs
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        logs.append(item)
        except OSError:
            return logs
        return logs[-1000:]

    def _prune_old_jobs(self):
        now = time.time()
        stale = []
        with self._lock:
            for job_id, job in self._jobs.items():
                snapshot = job.snapshot()
                if snapshot["status"] not in FINAL_JOB_STATUSES:
                    continue
                stamp = snapshot.get("finished_at") or snapshot.get("created_at")
                if not stamp:
                    continue
                try:
                    finished_ts = datetime.fromisoformat(stamp).timestamp()
                except ValueError:
                    continue
                if now - finished_ts > self.retention_seconds:
                    stale.append(job_id)
            for job_id in stale:
                self._jobs.pop(job_id, None)
                self._client_requests = {
                    key: value
                    for key, value in self._client_requests.items()
                    if value != job_id
                }
                for path in (self._job_state_path(job_id), self._job_log_path(job_id)):
                    try:
                        if path and os.path.exists(path):
                            os.remove(path)
                    except OSError:
                        pass


class LauncherDaemonFacade:
    def __init__(
        self,
        backend,
        *,
        http_host=HTTP_HOST,
        http_port=HTTP_PORT,
        socks_host=SOCKS_HOST,
        socks_port=SOCKS_PORT,
    ):
        self.backend = backend
        self.http_host = http_host
        self.http_port = http_port
        self.socks_host = socks_host
        self.socks_port = socks_port
        job_storage_dir = None
        if getattr(self.backend, "config", None) is not None:
            app_data_dir = getattr(self.backend.config, "app_data_dir", "")
            if app_data_dir:
                job_storage_dir = os.path.join(app_data_dir, "daemon_jobs")
        self.jobs = JobStore(storage_dir=job_storage_dir)
        self._bindings_by_instance = {}
        self._bindings_by_launcher_id = {}
        self._used_nonces = {}
        self._lock = threading.RLock()
        self._http_server = None
        self._http_thread = None
        self._socks_server = None
        self._socks_thread = None
        self._bridge = None
        self.started_at = _utc_now()

    def start(self):
        try:
            if self._http_server is None:
                handler_cls = self._make_http_handler()

                class ReusableHTTPServer(ThreadingHTTPServer):
                    allow_reuse_address = True

                self._http_server = ReusableHTTPServer(
                    (self.http_host, self.http_port),
                    handler_cls,
                )
                self._http_thread = threading.Thread(
                    target=self._http_server.serve_forever,
                    name="LauncherDaemonHTTP",
                    daemon=True,
                )
                self._http_thread.start()
            if self._socks_server is None:
                socks_cls = self._make_socks_handler()

                class ReusableTCPServer(socketserver.ThreadingTCPServer):
                    allow_reuse_address = True

                self._socks_server = ReusableTCPServer(
                    (self.socks_host, self.socks_port),
                    socks_cls,
                )
                self._socks_server.daemon_threads = True
                self._socks_thread = threading.Thread(
                    target=self._socks_server.serve_forever,
                    name="LauncherDaemonSOCKS",
                    daemon=True,
                )
                self._socks_thread.start()
            if self._bridge is None:
                self._bridge = DaemonSocksBridge(
                    self.backend,
                    listen_port=self.socks_port,
                    target_host=self.socks_host,
                    target_port=self.socks_port,
                )
                self._bridge.start()
        except Exception:
            self.stop()
            raise

    def stop(self):
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
        if self._socks_server is not None:
            self._socks_server.shutdown()
            self._socks_server.server_close()
            self._socks_server = None

    def ensure_instance_binding(self, launcher_inst_id, inst):
        data_dir = str(inst.get("data_dir") or "/root/nekro_agent_data")
        deploy_dir = str(inst.get("deploy_dir") or "/root/nekro_agent")
        instance_name = str(inst.get("instance_name") or "")
        tools_dir = f"{data_dir.rstrip('/')}/.na-tools"
        salt_path = f"{tools_dir}/instance.salt"
        token_path = f"{tools_dir}/daemon.token"

        with self._lock:
            self.backend._run_wsl_checked(
                DISTRO_NAME,
                f"mkdir -p {shlex.quote(tools_dir)}",
                action="[daemon] 创建 .na-tools 目录失败",
                timeout=30,
            )
            salt = self._read_wsl_file(salt_path).strip()
            if not salt:
                salt = secrets.token_hex(16)
                self._write_wsl_file(salt_path, salt + "\n")
            token = self._read_wsl_file(token_path).strip()
            if not token:
                token = secrets.token_hex(32)
                self._write_wsl_file(token_path, token + "\n")

            identity = f"{data_dir}\0{instance_name.rstrip('_') or launcher_inst_id}\0{salt}"
            instance_id = "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
            binding = DaemonBinding(
                launcher_inst_id=launcher_inst_id,
                instance_id=instance_id,
                token=token,
                data_dir=data_dir,
                deploy_dir=deploy_dir,
                compose_file=f"{deploy_dir}/docker-compose.yml",
                env_file=f"{deploy_dir}/.env",
                channel=str(inst.get("release_channel") or "stable"),
                nekro_port=int(inst.get("nekro_port") or 8021),
                instance_name=instance_name,
            )
            self._bindings_by_instance[instance_id] = binding
            self._bindings_by_launcher_id[launcher_inst_id] = binding
            self._write_daemon_json(binding, token_path)
            self._harden_binding_files(tools_dir, salt_path, token_path, binding)
            if self.backend.config:
                self.backend.config.update_instance(
                    launcher_inst_id,
                    daemon_instance_id=instance_id,
                )
            if self._bridge is not None:
                self._bridge.poke()
            return binding

    def env_values_for_binding(self, binding):
        return {
            "NA_TOOLS_DAEMON_ENABLED": "true",
            "NA_TOOLS_DAEMON_API_BASE": API_BASE,
            "NA_TOOLS_DAEMON_SOCKS": SOCKS_URL,
            "NA_TOOLS_DAEMON_INSTANCE_ID": binding.instance_id,
            "NA_TOOLS_DAEMON_TOKEN_FILE": f"{binding.data_dir}/.na-tools/daemon.token",
        }

    def _read_wsl_file(self, path):
        quoted = shlex.quote(path)
        return self.backend._wsl_exec(
            DISTRO_NAME,
            f"test -f {quoted} && cat {quoted}",
            timeout=15,
        )

    def _write_wsl_file(self, path, content):
        self.backend._write_to_wsl(DISTRO_NAME, content, path)

    def _write_daemon_json(self, binding, token_path):
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "provider": PROVIDER,
            "api_base": API_BASE,
            "socks_url": SOCKS_URL,
            "instance_id": binding.instance_id,
            "data_dir": binding.data_dir,
            "token_file": token_path,
            "http_bind": f"{self.http_host}:{self.http_port}",
            "socks_bind": f"wsl:0.0.0.0:{self.socks_port}",
            "daemon_pid": os.getpid(),
            "started_at": _utc_now(),
        }
        self._write_wsl_file(
            f"{binding.data_dir.rstrip('/')}/.na-tools/daemon.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def _harden_binding_files(self, tools_dir, salt_path, token_path, binding):
        daemon_json = f"{binding.data_dir.rstrip('/')}/.na-tools/daemon.json"
        self.backend._run_wsl_checked(
            DISTRO_NAME,
            "chmod 700 {tools_dir} && chmod 600 {salt} {token} {daemon_json}".format(
                tools_dir=shlex.quote(tools_dir),
                salt=shlex.quote(salt_path),
                token=shlex.quote(token_path),
                daemon_json=shlex.quote(daemon_json),
            ),
            action="[daemon] 设置 .na-tools 文件权限失败",
            timeout=30,
        )

    def _binding_for_instance(self, instance_id):
        with self._lock:
            binding = self._bindings_by_instance.get(instance_id)
            if binding:
                return binding
            if not self.backend.config:
                return None
            for launcher_inst_id, inst in self.backend.config.list_instances():
                if inst.get("daemon_instance_id") == instance_id:
                    try:
                        return self.ensure_instance_binding(launcher_inst_id, inst)
                    except Exception:
                        return None
            return None

    def _current_binding(self):
        if not self.backend.config:
            return None
        launcher_inst_id = self.backend.config.get_active_instance_id()
        if not launcher_inst_id:
            return None
        inst = self.backend.config.get_instance(launcher_inst_id)
        if not inst:
            return None
        with self._lock:
            binding = self._bindings_by_launcher_id.get(launcher_inst_id)
        if binding:
            return binding
        return self.ensure_instance_binding(launcher_inst_id, inst)

    def _validate_auth(self, method, path_with_query, headers, body):
        def _header(name):
            return headers.get(name.lower(), "")

        instance_id = _header("X-NA-Instance")
        timestamp = _header("X-NA-Timestamp")
        nonce = _header("X-NA-Nonce")
        signature = _header("X-NA-Signature")
        if not all([instance_id, timestamp, nonce, signature]):
            return None, _error("auth_failed", "缺少 daemon 签名头")
        binding = self._binding_for_instance(instance_id)
        if not binding:
            return None, _error("instance_not_bound", "实例未绑定到 Windows 启动器 daemon")
        try:
            timestamp_int = int(timestamp)
        except ValueError:
            return None, _error("auth_failed", "时间戳格式无效")
        now_ms = int(time.time() * 1000)
        if abs(now_ms - timestamp_int) > 60_000:
            return None, _error("auth_failed", "请求时间戳已过期")

        cutoff = now_ms - 300_000
        nonce_key = f"{instance_id}:{nonce}"
        with self._lock:
            self._used_nonces = {
                key: value for key, value in self._used_nonces.items() if value >= cutoff
            }
            if nonce_key in self._used_nonces:
                return None, _error("request_replayed", "重复的 daemon 请求 nonce")

        body_hash = _sha256_hex(body)
        signing_text = "\n".join(
            [method.upper(), path_with_query, timestamp, nonce, body_hash]
        )
        expected = "v1=" + hmac.new(
            binding.token.encode("utf-8"),
            signing_text.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None, _error("auth_failed", "daemon 签名校验失败")
        with self._lock:
            self._used_nonces[nonce_key] = now_ms
        return binding, None

    _CAPABILITY_REASONS = (
        "instance_not_bound",
        "compose_missing",
        "env_missing",
        "docker_unavailable",
        "docker_socket_missing",
        "docker_not_running",
    )

    def _capability_unavailable_reason(self, binding):
        if not self.backend.runtime_exists():
            return "wsl_unavailable"
        checks = [
            (f"test -d {shlex.quote(binding.data_dir)}", "instance_not_bound"),
            (f"test -f {shlex.quote(binding.compose_file)}", "compose_missing"),
            (f"test -f {shlex.quote(binding.env_file)}", "env_missing"),
            ("command -v docker >/dev/null 2>&1", "docker_unavailable"),
            ("docker compose version >/dev/null 2>&1", "docker_unavailable"),
            ("test -S /var/run/docker.sock", "docker_socket_missing"),
            ("docker version >/dev/null 2>&1", "docker_not_running"),
        ]
        # 合并为一次 WSL 调用：输出第一个失败原因码，全部通过输出 ok。
        script = " ".join(
            f"{cmd} || {{ echo {reason}; exit 0; }};" for cmd, reason in checks
        )
        script += " echo ok"
        raw = self.backend._wsl_exec(DISTRO_NAME, script, timeout=45)
        for line in raw.splitlines():
            token = line.strip().strip("\x00")
            if token == "ok":
                return None
            if token in self._CAPABILITY_REASONS:
                return token
        return "wsl_unavailable"

    def _capabilities(self, binding):
        reason = self._capability_unavailable_reason(binding)
        enabled = reason is None

        return {
            "enabled": enabled,
            "provider": PROVIDER,
            "protocol_version": PROTOCOL_VERSION,
            "platform": "windows",
            "instance_id": binding.instance_id,
            "supports": {
                "update": True,
                "preview": True,
                "rollback": True,
                "backup": True,
                "restore": True,
                "restore_pre_preview": True,
                "cancel": True,
                "log_stream": True,
                "daemon_update": False,
            },
            "limits": {"max_parallel_jobs_per_instance": 1, "job_log_retention_days": 7},
            "unavailable_reason": reason,
        }

    def _instance_payload(self, binding):
        channel = binding.channel
        if self.backend.config:
            inst = self.backend.config.get_instance(binding.launcher_inst_id)
            if inst:
                channel = str(inst.get("release_channel") or channel or "stable")
        return {
            "instance_id": binding.instance_id,
            "data_dir": binding.data_dir,
            "compose_file": binding.compose_file,
            "env_file": binding.env_file,
            "channel": channel,
            "app": {
                "expose_port": binding.nekro_port,
                "health_url": f"http://127.0.0.1:{binding.nekro_port}/api/health",
            },
            "container": {
                "name": f"{binding.instance_name}nekro_agent",
                "status": "running" if self.backend.is_running else "unknown",
                "image": self.backend.get_agent_image_ref(release_channel=channel),
                "image_tag": channel,
            },
            "docker": {
                "docker_installed": True,
                "compose_installed": True,
                "compose_cmd": ["docker", "compose"],
            },
        }

    def _handle_request(self, method, path_with_query, headers, body):
        parsed = urlparse(path_with_query)
        path = parsed.path
        binding, auth_error = self._validate_auth(method, path_with_query, headers, body)
        if auth_error:
            # 协议约定：签名类错误 401，实例绑定/归属类错误 403。
            error_code = auth_error.get("error", {}).get("code", "")
            status = 403 if error_code in {"instance_not_bound", "instance_mismatch"} else 401
            return status, auth_error

        if method == "GET" and path == "/v1/health":
            return 200, {
                "ok": True,
                "daemon_version": self._daemon_version(),
                "protocol_version": PROTOCOL_VERSION,
                "platform": "windows",
                "started_at": self.started_at,
            }
        if method == "GET" and path == "/v1/capabilities":
            return 200, self._capabilities(binding)
        if method == "GET" and path == "/v1/instances/current":
            return 200, self._instance_payload(binding)
        if method == "GET" and path == "/v1/backups":
            return self._list_backups(binding, parsed.query)
        if method == "POST" and path == "/v1/jobs/update":
            return self._create_update_job(binding, body)
        if method == "POST" and path == "/v1/jobs/backup":
            return self._create_backup_job(binding, body)
        if method == "POST" and path == "/v1/jobs/restore":
            return self._create_restore_job(binding, body)
        if method == "POST" and path.startswith("/v1/jobs/"):
            return self._job_action_response(binding, path)
        if method == "GET" and path.startswith("/v1/jobs/"):
            return self._job_response(binding, path, parsed.query)
        return 404, _error("daemon_protocol_error", "未知 daemon API 路径")

    def _daemon_version(self):
        try:
            from core.app_updater import APP_VERSION

            return APP_VERSION
        except Exception:
            return "0.0.0"

    def _request_context(self, binding, request):
        enriched = dict(request)
        enriched["_launcher_inst_id"] = binding.launcher_inst_id
        enriched["_deploy_dir"] = binding.deploy_dir
        enriched["_data_dir"] = binding.data_dir
        enriched["_instance_name"] = binding.instance_name
        enriched["_nekro_port"] = binding.nekro_port
        enriched["_current_channel"] = binding.channel
        return enriched

    def _parse_job_request(self, binding, body):
        try:
            request = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return None, (400, _error("daemon_protocol_error", "请求 JSON 格式无效"))
        if not isinstance(request, dict):
            return None, (400, _error("daemon_protocol_error", "请求 JSON 必须是对象"))
        if request.get("instance_id") != binding.instance_id:
            return None, (403, _error("instance_mismatch", "请求 instance_id 与签名实例不匹配"))
        return self._request_context(binding, request), None

    def _job_conflict_error(self, binding):
        active = self.jobs.active_for_instance(binding.instance_id)
        details = {"job_id": active.job_id} if active else None
        return 409, _error("job_conflict", "已有更新任务正在运行", details)

    def _launcher_busy_details(self):
        """启动器本地是否有互斥操作（UI 更新/切换/部署）在执行。"""
        name_getter = getattr(self.backend, "exclusive_operation_name", None)
        if callable(name_getter):
            name = name_getter()
            if name:
                return {"operation": name}
        if getattr(self.backend, "_deploying", False):
            return {"operation": "deploy"}
        return None

    def _launcher_busy_error(self, details):
        return 409, _error(
            "launcher_busy",
            "启动器正在执行本地操作，请等待完成后重试",
            details,
        )

    def _list_backups(self, binding, query):
        params = parse_qs(query)
        try:
            limit = min(100, max(1, int((params.get("limit") or ["50"])[0])))
        except ValueError:
            return 400, _error("daemon_protocol_error", "备份查询参数格式无效")
        name = str((params.get("name") or [""])[0]).strip()
        request = self._request_context(binding, {"instance_id": binding.instance_id})
        try:
            return 200, {
                "backups": self.backend.list_daemon_backups(
                    request,
                    name=name,
                    limit=limit,
                )
            }
        except Exception as exc:
            return 500, _error(
                "launcher_update_failed",
                f"读取备份列表失败: {type(exc).__name__}: {exc}",
            )

    def _create_update_job(self, binding, body):
        request, error_response = self._parse_job_request(binding, body)
        if error_response:
            return error_response
        assert request is not None
        channel = request.get("channel") or "stable"
        if channel not in {"stable", "preview", "rollback"}:
            return 400, _error("invalid_channel", "channel 必须是 stable、preview 或 rollback")
        busy = self._launcher_busy_details()
        if busy:
            return self._launcher_busy_error(busy)
        caps = self._capabilities(binding)
        if not caps["enabled"]:
            return 503, _error(
                caps["unavailable_reason"] or "daemon_unavailable",
                "当前实例不可执行在线更新",
            )
        job, created = self.jobs.create_update_job(binding.instance_id, request)
        if job is None:
            return self._job_conflict_error(binding)
        return self._start_job(job, created, self._run_update_job)

    def _create_backup_job(self, binding, body):
        request, error_response = self._parse_job_request(binding, body)
        if error_response:
            return error_response
        assert request is not None
        name = str(request.get("name") or "manual").strip()
        if not BACKUP_NAME_RE.fullmatch(name):
            return 400, _error("invalid_backup_name", "备份名称只能包含字母、数字、点、下划线和短横线")
        request["name"] = name
        busy = self._launcher_busy_details()
        if busy:
            return self._launcher_busy_error(busy)
        caps = self._capabilities(binding)
        if not caps["enabled"]:
            return 503, _error(
                caps["unavailable_reason"] or "daemon_unavailable",
                "当前实例不可执行备份",
            )
        job, created = self.jobs.create_backup_job(binding.instance_id, request)
        if job is None:
            return self._job_conflict_error(binding)
        return self._start_job(job, created, self._run_backup_job)

    def _create_restore_job(self, binding, body):
        request, error_response = self._parse_job_request(binding, body)
        if error_response:
            return error_response
        assert request is not None
        backup_id = str(request.get("backup_id") or "").strip()
        if not BACKUP_ID_RE.fullmatch(backup_id) or ".." in backup_id:
            return 400, _error("invalid_backup_id", "backup_id 必须是当前实例备份文件名")
        request["backup_id"] = backup_id
        busy = self._launcher_busy_details()
        if busy:
            return self._launcher_busy_error(busy)
        caps = self._capabilities(binding)
        if not caps["enabled"]:
            return 503, _error(
                caps["unavailable_reason"] or "daemon_unavailable",
                "当前实例不可执行还原",
            )
        job, created = self.jobs.create_restore_job(binding.instance_id, request)
        if job is None:
            return self._job_conflict_error(binding)
        return self._start_job(job, created, self._run_restore_job)

    def _start_job(self, job, created, runner):
        if created:
            thread = threading.Thread(
                target=runner,
                args=(job, job.request),
                name=f"LauncherDaemonJob-{job.job_id}",
                daemon=True,
            )
            thread.start()
        return 200, {
            "job_id": job.job_id,
            "status": job.status,
            "phase": job.phase,
            "message": job.message,
        }

    def _acquire_backend_exclusive(self, job):
        """任务真正执行前占用启动器互斥槽，防止与 UI 本地操作并发跑 compose。"""
        acquire = getattr(self.backend, "acquire_exclusive_operation", None)
        if not callable(acquire):
            return True
        if acquire(f"daemon:{job.type}"):
            return True
        job.fail("launcher_busy", "启动器正在执行本地操作，任务无法开始")
        return False

    def _release_backend_exclusive(self):
        release = getattr(self.backend, "release_exclusive_operation", None)
        if callable(release):
            release()

    def _run_update_job(self, job, request):
        if not self._acquire_backend_exclusive(job):
            return
        try:
            self.backend.run_daemon_update_job(request, job)
        except Exception as exc:
            job.fail(
                "launcher_update_failed",
                f"Windows 启动器更新任务异常: {type(exc).__name__}: {exc}",
            )
            self.backend.status_changed.emit("更新失败")
        finally:
            self._release_backend_exclusive()

    def _run_backup_job(self, job, request):
        if not self._acquire_backend_exclusive(job):
            return
        try:
            self.backend.run_daemon_backup_job(request, job)
        except Exception as exc:
            job.fail(
                "backup_failed",
                f"Windows 启动器备份任务异常: {type(exc).__name__}: {exc}",
            )
        finally:
            self._release_backend_exclusive()

    def _run_restore_job(self, job, request):
        if not self._acquire_backend_exclusive(job):
            return
        try:
            self.backend.run_daemon_restore_job(request, job)
        except Exception as exc:
            job.fail(
                "launcher_update_failed",
                f"Windows 启动器还原任务异常: {type(exc).__name__}: {exc}",
            )
            self.backend.status_changed.emit("更新失败")
        finally:
            self._release_backend_exclusive()

    def _job_action_response(self, binding, path):
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[3] == "cancel":
            job = self.jobs.get(parts[2])
            if not job:
                return 404, _error("job_not_found", "任务不存在")
            if job.instance_id != binding.instance_id:
                return 403, _error("instance_mismatch", "任务不属于当前实例")
            job.request_cancel()
            snapshot = job.snapshot()
            return 200, {
                "job_id": job.job_id,
                "status": snapshot["status"],
                "message": snapshot["message"],
            }
        return 404, _error("job_not_found", "任务资源不存在")

    def _job_response(self, binding, path, query):
        parts = path.strip("/").split("/")
        if len(parts) < 3:
            return 404, _error("job_not_found", "任务不存在")
        job = self.jobs.get(parts[2])
        if not job:
            return 404, _error("job_not_found", "任务不存在")
        if job.instance_id != binding.instance_id:
            return 403, _error("instance_mismatch", "任务不属于当前实例")
        if len(parts) == 3:
            return 200, job.snapshot()
        if len(parts) == 4 and parts[3] == "logs":
            params = parse_qs(query)
            try:
                limit = min(1000, max(1, int((params.get("limit") or ["200"])[0])))
                after_seq = max(0, int((params.get("after_seq") or ["0"])[0]))
            except ValueError:
                return 400, _error("daemon_protocol_error", "日志查询参数格式无效")
            return 200, job.log_snapshot(limit=limit, after_seq=after_seq)
        if len(parts) == 4 and parts[3] == "events":
            return 200, {"sse_job": job}
        return 404, _error("job_not_found", "任务资源不存在")

    def _make_http_handler(self):
        facade = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                self._handle()

            def do_POST(self):
                self._handle()

            def _headers_dict(self):
                return {key.lower(): self.headers.get(key, "") for key in self.headers.keys()}

            def _handle(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._send_json(
                        400,
                        _error("daemon_protocol_error", "Content-Length 格式无效"),
                    )
                    return
                if length > MAX_REQUEST_BODY_BYTES:
                    self._send_json(
                        413,
                        _error("daemon_protocol_error", "请求体超过 daemon 限制"),
                    )
                    return
                body = self.rfile.read(length) if length else b""
                status, payload = facade._handle_request(
                    self.command,
                    self.path,
                    self._headers_dict(),
                    body,
                )
                if isinstance(payload, dict) and "sse_job" in payload:
                    self._send_sse(payload["sse_job"])
                    return
                self._send_json(status, payload)

            def _send_json(self, status, payload):
                data = _json_bytes(payload)
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_sse(self, job):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                # SSE 响应没有 Content-Length，流结束后必须关闭连接，
                # 否则依赖 EOF 的客户端会一直挂到超时。
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                after_seq = 0
                params = parse_qs(urlparse(self.path).query)
                try:
                    after_seq = int((params.get("after_seq") or ["0"])[0])
                except ValueError:
                    after_seq = 0
                initial_snapshot = job.snapshot()
                self._write_sse("job", initial_snapshot)
                self._write_sse("progress", initial_snapshot["progress"])
                try:
                    while True:
                        snapshot = job.log_snapshot(limit=1000, after_seq=after_seq)
                        for entry in snapshot["logs"]:
                            after_seq = max(after_seq, entry["seq"])
                            self._write_sse("log", entry)
                        job_snapshot = job.snapshot()
                        self._write_sse("job", job_snapshot)
                        self._write_sse("progress", job_snapshot["progress"])
                        if job_snapshot["status"] in FINAL_JOB_STATUSES:
                            self._write_sse("result", job_snapshot)
                            break
                        if not job.wait_for_event(after_seq, timeout=30):
                            self._write_sse("ping", {"ts": _utc_now()})
                except OSError:
                    return

            def _write_sse(self, event, payload):
                data = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                self.wfile.write(data.encode("utf-8"))
                self.wfile.flush()

        return Handler

    def _make_socks_handler(self):
        facade = self

        class SocksHandler(socketserver.BaseRequestHandler):
            def handle(self):
                sock = self.request
                sock.settimeout(10)
                try:
                    header = sock.recv(2)
                    if len(header) != 2 or header[0] != 5:
                        return
                    methods = sock.recv(header[1])
                    if not methods:
                        return
                    sock.sendall(b"\x05\x00")
                    req = sock.recv(4)
                    if len(req) != 4 or req[0] != 5 or req[1] != 1:
                        self._reply(sock, 7)
                        return
                    atyp = req[3]
                    host = ""
                    if atyp == 1:
                        host = socket.inet_ntoa(sock.recv(4))
                    elif atyp == 3:
                        ln = sock.recv(1)[0]
                        host = sock.recv(ln).decode("idna")
                    else:
                        self._reply(sock, 8)
                        return
                    port = int.from_bytes(sock.recv(2), "big")
                    if not facade._socks_allowed(host, port):
                        self._reply(sock, 2)
                        return
                    upstream = socket.create_connection((facade.http_host, facade.http_port), timeout=10)
                    self._reply(sock, 0)
                    self._relay(sock, upstream)
                except Exception:
                    return

            def _reply(self, sock, code):
                sock.sendall(b"\x05" + bytes([code]) + b"\x00\x01\x00\x00\x00\x00\x00\x00")

            def _relay(self, left, right):
                sockets = [left, right]
                try:
                    while True:
                        readable, _, _ = select.select(sockets, [], [], 60)
                        if not readable:
                            return
                        for src in readable:
                            data = src.recv(65536)
                            if not data:
                                return
                            dst = right if src is left else left
                            dst.sendall(data)
                finally:
                    right.close()

        return SocksHandler

    def _socks_allowed(self, host, port):
        host_l = host.lower()
        return (
            (host_l == "na-tools.local" and port == 80)
            or (host_l in {"127.0.0.1", "localhost"} and port == self.http_port)
        )
