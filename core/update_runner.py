import json
from typing import Callable
from urllib.request import urlopen, Request
from urllib.error import URLError

UPDATE_JSON_URL = "https://ep.nekro.ai/e/liugu2023/api/update/update_nekro_agent.json"

# 允许直接透传到 shell 的 Linux 基础命令白名单
_SHELL_PASSTHROUGH = {
    "mv", "cp", "rm", "mkdir", "chmod", "chown", "ln", "touch",
    "cat", "echo", "sed", "awk", "grep", "find", "tar", "curl", "wget",
    "systemctl", "service", "bash", "sh",
}


def _fetch_json(log_fn: Callable[[str, str], None]) -> dict | None:
    """从远端拉取更新 JSON，失败返回 None。"""
    log_fn(f"[update_runner] 拉取更新配置: {UPDATE_JSON_URL}", "info")
    try:
        req = Request(UPDATE_JSON_URL, headers={"User-Agent": "NekroAgent-Updater/1.0"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data
    except URLError as e:
        log_fn(f"[update_runner] 网络错误: {e}", "error")
    except json.JSONDecodeError as e:
        log_fn(f"[update_runner] JSON 解析失败: {e}", "error")
    return None


def _build_cmd(step: dict) -> str | None:
    """根据 step 类型拼接命令字符串，未知类型返回 None。"""
    step_type = step.get("type")

    if step_type == "pull":
        image = step.get("image", "")
        return f"docker pull {image}"

    if step_type == "compose_down":
        services = step.get("services", [])
        svc_str = " ".join(services)
        return f"docker compose stop {svc_str}".strip()

    if step_type == "compose_up":
        services = step.get("services", [])
        svc_str = " ".join(services)
        return f"docker compose up -d {svc_str}".strip()

    if step_type == "shell":
        # 通用 shell 命令：{ "type": "shell", "command": "mv /a /b" }
        command = step.get("command", "").strip()
        base = command.split()[0] if command else ""
        if base in _SHELL_PASSTHROUGH:
            return command
        return None  # 不在白名单内，交由调用方处理

    if step_type == "notify":
        return None  # notify 无 shell 命令

    return None


def parse_update_commands(log_fn: Callable[[str, str], None]) -> list[dict]:
    """
    实时拉取并解析更新命令，在 info 级别输出每步拼接的命令。

    log_fn:  签名为 (message: str, level: str) -> None 的日志回调
    返回值:  按 step 升序排列的步骤列表，每项附带 _cmd 字段
             _cmd 为 None 表示无 shell 命令（notify）或未知/不允许的类型
    """
    data = _fetch_json(log_fn)
    if data is None:
        return []

    steps = sorted(data.get("steps", []), key=lambda s: s.get("step", 0))
    parsed = []

    for step in steps:
        step_type = step.get("type")
        label = step.get("label", "")
        step_no = step.get("step", "?")

        cmd = _build_cmd(step)

        if step_type == "notify":
            message = step.get("message", "")
            log_fn(f"[step {step_no}] {label} => notify: {message}", "info")

        elif step_type == "shell" and cmd is None:
            command = step.get("command", "")
            base = command.split()[0] if command else ""
            log_fn(f"[step {step_no}] {label} => shell 命令不在白名单，已跳过: {command}", "warning")

        elif cmd is not None:
            log_fn(f"[step {step_no}] {label} => {cmd}", "info")

        else:
            log_fn(f"[step {step_no}] 未知类型: {step_type}，已跳过", "warning")

        parsed.append({**step, "_cmd": cmd})

    return parsed
