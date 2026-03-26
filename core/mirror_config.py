UBUNTU_CLOUD_IMAGE_URLS = [
    "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cloud-images/jammy/current/jammy-server-cloudimg-amd64-azure.vhd.tar.gz",
    "https://mirrors.ustc.edu.cn/ubuntu-cloud-images/jammy/current/jammy-server-cloudimg-amd64-azure.vhd.tar.gz",
    "https://mirror.sjtu.edu.cn/ubuntu-cloud-images/jammy/current/jammy-server-cloudimg-amd64-azure.vhd.tar.gz",
    "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64-azure.vhd.tar.gz",
]

APT_MIRROR_LINES = [
    "deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ jammy main restricted universe multiverse",
    "deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ jammy-updates main restricted universe multiverse",
    "deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ jammy-backports main restricted universe multiverse",
    "deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu/ jammy-security main restricted universe multiverse",
]

DOCKER_REGISTRY_MIRRORS = [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com",
    "https://hub-mirror.c.163.com",
]

DOCKER_APT_MIRRORS = [
    ("清华大学", "https://mirrors.tuna.tsinghua.edu.cn/docker-ce"),
    ("阿里云", "https://mirrors.aliyun.com/docker-ce"),
    ("官方源", "https://download.docker.com"),
]
