from typing import Callable

from core.wsl.constants import UPDATE_BACKUP_ARCHIVE_PATH


def build_update_plan(agent_image: str) -> list[dict]:
    """返回内置升级流程定义，不依赖远端下发 shell。"""
    return [
        {
            "step": 1,
            "type": "notify",
            "label": "更新开始通知",
            "message": "正在更新 Nekro Agent，请稍候...",
        },
        {
            "step": 2,
            "type": "backup",
            "label": "创建运行数据备份归档",
            "archive_path": UPDATE_BACKUP_ARCHIVE_PATH,
            "optional": True,
            "optional_prompt": (
                "是否在更新前备份当前运行数据与部署配置？\n"
                f"备份文件将写入 {UPDATE_BACKUP_ARCHIVE_PATH}。\n"
                "预计占用约 {backup_size} 空间。\n\n"
                "备份内容包括当前受管的数据卷、数据目录与部署配置。"
            ),
        },
        {
            "step": 3,
            "type": "pull",
            "label": "拉取最新 Nekro Agent 镜像",
            "image": agent_image,
        },
        {
            "step": 4,
            "type": "compose_up",
            "label": "重建 Nekro Agent 容器",
            "services": ["nekro_agent"],
        },
        {
            "step": 5,
            "type": "notify",
            "label": "更新完成通知",
            "message": "Nekro Agent 更新完成，服务已重启。",
        },
    ]


def log_update_plan(log_fn: Callable[[str, str], None], steps: list[dict]) -> None:
    for step in steps:
        step_no = step.get("step", "?")
        step_type = step.get("type")
        label = step.get("label", "")

        if step_type == "notify":
            log_fn(f"[step {step_no}] {label} => notify: {step.get('message', '')}", "info")
        elif step_type == "backup":
            log_fn(
                f"[step {step_no}] {label} => optional backup: {step.get('archive_path', UPDATE_BACKUP_ARCHIVE_PATH)}",
                "info",
            )
        elif step_type == "pull":
            log_fn(f"[step {step_no}] {label} => pull image: {step.get('image', '')}", "info")
        elif step_type == "compose_up":
            services = " ".join(step.get("services", []))
            log_fn(f"[step {step_no}] {label} => recreate services: {services}".rstrip(), "info")
        else:
            log_fn(f"[step {step_no}] 未知升级步骤: {step_type}", "warning")
