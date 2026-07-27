WSL_VIRTUALIZATION_ERROR_MARKERS = (
    "hcs_e_hyperv_not_installed",
    "0x80370102",
    "0x80370114",
    "enablevirtualization",
    "virtual machine platform",
    "virtualmachineplatform",
    "virtualization is disabled",
    "required feature is not installed",
    "虚拟机平台",
    "未启用虚拟化",
    "尚未启用虚拟化",
)


def is_wsl_virtualization_error(output):
    """判断 WSL 输出是否表示 WSL2 虚拟化前置条件未满足。"""
    lowered = (output or "").lower()
    return any(marker in lowered for marker in WSL_VIRTUALIZATION_ERROR_MARKERS)


def wsl_virtualization_guidance():
    return (
        "\n\nWSL2 的虚拟化前置条件尚未满足。请按以下步骤处理：\n"
        "1. 以管理员身份运行 wsl --install --no-distribution，启用“适用于 Linux 的 Windows 子系统”和“虚拟机平台”；\n"
        "2. 完成后重启电脑；\n"
        "3. 若重启后仍失败，请进入 BIOS/UEFI 开启 CPU 硬件虚拟化（Intel VT-x / AMD-V 或 SVM）。"
    )


__all__ = [
    "is_wsl_virtualization_error",
    "wsl_virtualization_guidance",
]
