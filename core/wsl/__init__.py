from core.wsl.constants import (
    DISTRO_NAME,
    MANAGED_IMAGES_BASE,
    NA_BACKUP_TARGETS,
    PREVIEW_BACKUP_ARCHIVE_PATH,
    PREVIEW_COMPOSE_IMAGE,
    PREVIEW_IMAGE,
    REQUIRED_IMAGES_BASE,
    ROOTFS_URLS,
    STABLE_IMAGE,
)

# 注意：不要在此处导入 WSLManager。manager 依赖 core.launcher_daemon，
# 而 launcher_daemon/daemon_bridge 又依赖 core.wsl.constants；包 __init__
# 急切导入 manager 会让“先导入 launcher_daemon”的路径形成循环导入。
# 需要 WSLManager 的调用方应直接 `from core.wsl.manager import WSLManager`。

__all__ = [
    "DISTRO_NAME",
    "MANAGED_IMAGES_BASE",
    "NA_BACKUP_TARGETS",
    "PREVIEW_BACKUP_ARCHIVE_PATH",
    "PREVIEW_COMPOSE_IMAGE",
    "PREVIEW_IMAGE",
    "REQUIRED_IMAGES_BASE",
    "ROOTFS_URLS",
    "STABLE_IMAGE",
]
