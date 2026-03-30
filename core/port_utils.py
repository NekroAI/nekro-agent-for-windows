import socket


def _can_bind_localhost(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False


def validate_port_bindings(port_specs, ignore_ports=None):
    """
    校验端口配置。

    port_specs: [(label, port), ...]
    ignore_ports: {port, ...} 中的端口跳过占用检测，但仍参与重复检测。
    返回 (ok, message)
    """
    ignore_ports = {int(port) for port in (ignore_ports or set())}
    normalized = [(label, int(port)) for label, port in port_specs]

    seen = {}
    for label, port in normalized:
        if port in seen:
            other_label = seen[port]
            return False, f"{other_label} 与 {label} 不能使用同一个端口 ({port})。"
        seen[port] = label

    conflicts = []
    for label, port in normalized:
        if port in ignore_ports:
            continue
        if not _can_bind_localhost(port):
            conflicts.append(f"{label}: {port}")

    if conflicts:
        return False, "以下端口已被占用，请修改后重试：\n" + "\n".join(conflicts)

    return True, ""
