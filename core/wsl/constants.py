DISTRO_NAME = "NekroAgent"

REQUIRED_IMAGES_BASE = {
    "napcat": [
        "postgres:14",
        "qdrant/qdrant:v1.17.1",
        "kromiose/nekro-agent:latest",
        "mlikiowa/napcat-docker",
        "kromiose/nekro-agent-sandbox",
        "kromiose/nekro-cc-sandbox",
    ],
    "lite": [
        "postgres:14",
        "qdrant/qdrant:v1.17.1",
        "kromiose/nekro-agent:latest",
        "kromiose/nekro-agent-sandbox",
        "kromiose/nekro-cc-sandbox",
    ],
}

MANAGED_IMAGES_BASE = [
    ("kromiose/nekro-agent:latest", "Nekro Agent 本体", "NA 核心服务镜像", ["napcat", "lite"]),
    ("kromiose/nekro-agent-sandbox", "NA 沙盒", "代码执行沙盒环境", ["napcat", "lite"]),
    ("kromiose/nekro-cc-sandbox", "NA CC 沙盒", "CC 扩展沙盒环境", ["napcat", "lite"]),
]

ROOTFS_URLS = [
    "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cloud-images/wsl/jammy/current/ubuntu-jammy-wsl-amd64-ubuntu22.04lts.rootfs.tar.gz",
    "https://mirror.sjtu.edu.cn/ubuntu-cloud-images/wsl/jammy/current/ubuntu-jammy-wsl-amd64-ubuntu22.04lts.rootfs.tar.gz",
    "https://cloud-images.ubuntu.com/wsl/jammy/current/ubuntu-jammy-wsl-amd64-ubuntu22.04lts.rootfs.tar.gz",
]

STABLE_IMAGE = "kromiose/nekro-agent:latest"
PREVIEW_IMAGE = "kromiose/nekro-agent:preview"
PREVIEW_COMPOSE_IMAGE = STABLE_IMAGE
PREVIEW_BACKUP_ARCHIVE_PATH = "/root/na_preview_backup.tar.gz"
UPDATE_BACKUP_ARCHIVE_PATH = "/root/na_update_backup.tar.gz"
NA_BACKUP_TARGETS = [
    "/var/lib/docker/volumes/nekro_postgres_data",
    "/var/lib/docker/volumes/nekro_qdrant_data",
    "/root/nekro_agent_data",
    "/root/nekro_agent",
]

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
    "UPDATE_BACKUP_ARCHIVE_PATH",
]
