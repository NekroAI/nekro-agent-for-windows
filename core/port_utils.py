import socket


def _parse_port(port):
    try:
        value = int(str(port).strip())
    except (TypeError, ValueError):
        return None
    if not (1 <= value <= 65535):
        return None
    return value


def normalize_port(port, default):
    parsed = _parse_port(port)
    if parsed is not None:
        return parsed
    fallback = _parse_port(default)
    return fallback if fallback is not None else 1


def _can_bind_localhost(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
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
    ignore_ports = {
        parsed
        for parsed in (_parse_port(port) for port in (ignore_ports or set()))
        if parsed is not None
    }

    normalized = []
    invalid_labels = []
    for label, port in port_specs:
        parsed = _parse_port(port)
        if parsed is None:
            invalid_labels.append(label)
            continue
        normalized.append((label, parsed))

    if invalid_labels:
        return False, "请输入有效的端口号（1-65535）：" + "、".join(invalid_labels)

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


def _instance_display_name(inst_id, inst):
    if not isinstance(inst, dict):
        return str(inst_id or "default")
    return (
        str(inst.get("remark") or "").strip()
        or str(inst.get("instance_name") or "").rstrip("_")
        or str(inst_id or "default")
    )


def _instance_port_entries(inst_id, inst):
    if not isinstance(inst, dict):
        return []

    display = _instance_display_name(inst_id, inst)
    entries = [("Nekro Agent 端口", inst.get("nekro_port"))]
    if inst.get("deploy_mode") == "napcat":
        entries.append(("NapCat 端口", inst.get("napcat_port")))

    result = []
    for label, port in entries:
        parsed = _parse_port(port)
        result.append((inst_id, display, label, parsed, port))
    return result


def validate_instance_port_conflicts(
    instances,
    port_specs=None,
    current_instance_id=None,
):
    """校验实例之间的端口配置不重复。

    port_specs 为 None 时校验所有已登记实例；否则校验待保存端口是否与
    其它实例冲突。current_instance_id 用于编辑当前实例时跳过自身。
    """
    instance_entries = []
    invalid = []
    for inst_id, inst in instances or []:
        for entry_inst_id, display, label, parsed, raw_port in _instance_port_entries(
            inst_id,
            inst,
        ):
            if parsed is None:
                invalid.append(f"实例「{display}」{label}: {raw_port}")
                continue
            instance_entries.append((entry_inst_id, display, label, parsed))

    if invalid:
        return False, "实例端口配置无效，请先修正：\n" + "\n".join(invalid)

    if port_specs is not None:
        conflicts = []
        for label, port in port_specs:
            parsed = _parse_port(port)
            if parsed is None:
                return False, "请输入有效的端口号（1-65535）：" + str(label)
            for inst_id, display, existing_label, existing_port in instance_entries:
                if current_instance_id and inst_id == current_instance_id:
                    continue
                if parsed == existing_port:
                    conflicts.append(
                        f"{label}: {parsed} 已被实例「{display}」的{existing_label}使用"
                    )
        if conflicts:
            return False, "端口已被其他实例配置使用，请修改后重试：\n" + "\n".join(conflicts)
        return True, ""

    seen = {}
    conflicts = []
    for inst_id, display, label, port in instance_entries:
        if port in seen:
            other_display, other_label = seen[port]
            conflicts.append(
                f"{port}: 实例「{other_display}」的{other_label} 与实例「{display}」的{label}重复"
            )
            continue
        seen[port] = (display, label)

    if conflicts:
        return False, "多个实例配置了相同端口，请先修改后再启动：\n" + "\n".join(conflicts)

    return True, ""
