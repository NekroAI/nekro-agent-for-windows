import os
import posixpath
import shlex
import subprocess
import time

from core.port_utils import normalize_port
from core.wsl.constants import DISTRO_NAME, PREVIEW_IMAGE


class WSLDiscoveryMixin:
    """扫描本机所有 WSL 发行版中已有的 nekro-agent 实例，并提供接管能力。"""

    _DANGEROUS_MIGRATION_PATHS = {
        "",
        "/",
        "/root",
        "/home",
        "/opt",
        "/var",
        "/var/lib",
        "/var/lib/docker",
        "/var/lib/docker/volumes",
    }

    @staticmethod
    def _normalize_wsl_abs_path(path):
        normalized = str(path or "").strip().replace("\\", "/")
        if not normalized.startswith("/"):
            return ""
        return posixpath.normpath(normalized)

    def _is_safe_migration_path(self, path):
        normalized = self._normalize_wsl_abs_path(path)
        if not normalized or normalized in self._DANGEROUS_MIGRATION_PATHS:
            return False
        return "nekro" in normalized.lower()

    def _wsl_path_exists(self, distro, path, *, user="root"):
        normalized = self._normalize_wsl_abs_path(path)
        if not normalized:
            return False
        return (
            self._wsl_exec(
                distro,
                f"test -e {shlex.quote(normalized)} && echo yes",
                user=user,
            ).strip()
            == "yes"
        )

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #

    def scan_existing_instances(self, on_step=None) -> list:
        """扫描所有 WSL 发行版，返回发现的实例信息列表。

        on_step: 可选回调 (str) -> None，每个关键步骤时调用，用于 UI 进度展示。

        支持同一发行版内存在多个 nekro-agent 部署目录的情况。

        每个元素为 dict：
          distro      - 发行版名称
          is_managed  - 是否本工具专属发行版 NekroAgent
          deploy_mode - "lite" | "napcat"
          deploy_dir  - WSL 内部署目录
          data_dir    - WSL 内数据目录
          env         - .env 解析后的 dict
          instance_name - INSTANCE_NAME 前缀
          status      - "running" | "stopped"
        """
        if on_step:
            on_step("正在获取 WSL 发行版列表...")
        distros = self._get_wsl_distros()
        instances = []
        total = len(distros)
        for i, distro in enumerate(distros, 1):
            if on_step:
                on_step(f"正在检测发行版: {distro}（{i}/{total}）")
            try:
                results = self._scan_distro(distro)
                if results:
                    instances.extend(results)
            except Exception as e:
                self.log_received.emit(f"[实例扫描] 扫描 {distro} 时异常: {e}", "debug")
        return instances

    def takeover_instance(self, instance: dict, on_step=None) -> bool:
        """将发现的实例接管到本启动器管理。

        on_step: 可选回调 (step_index: int, total: int, desc: str) -> None

        场景 A（is_managed=True）：直接读取 .env 写入 config，无需迁移。
        场景 B（is_managed=False）：迁移数据到 NekroAgent 发行版后接管。
        """
        if instance.get("is_managed"):
            return self._takeover_managed(instance, on_step=on_step)
        return self._takeover_foreign(instance, on_step=on_step)

    # ------------------------------------------------------------------ #
    # 扫描内部实现
    # ------------------------------------------------------------------ #

    def _get_wsl_distros(self) -> list:
        """返回本机所有 WSL 发行版名称列表。"""
        try:
            proc = subprocess.run(
                ["wsl", "-l", "-q"],
                capture_output=True,
                timeout=10,
                creationflags=self._creation_flags(),
            )
            if proc.returncode != 0:
                self.log_received.emit(
                    self._format_command_failure(
                        "[实例扫描] 获取 WSL 发行版列表失败",
                        args=["wsl", "-l", "-q"],
                        timeout=10,
                        returncode=proc.returncode,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                    ),
                    "debug",
                )
                return []
            output = self._safe_decode(proc.stdout)
            distros = [
                line.strip().strip("\x00")
                for line in output.splitlines()
                if line.strip().strip("\x00")
            ]
            return distros
        except Exception as e:
            self.log_received.emit(f"[实例扫描] 获取发行版列表失败: {e}", "debug")
            return []

    def _scan_distro(self, distro: str) -> list:
        """扫描单个发行版，返回发现的所有 nekro-agent 实例列表。

        扫描策略：先用轻量文件检查（不唤醒 Docker daemon），只有确认存在
        nekro_agent 部署目录后，才对已运行中的 Docker daemon 做容器查询。
        这样可以避免 docker info 唤醒所有 WSL 实例消耗大量系统资源。
        """
        self.log_received.emit(f"[实例扫描] 扫描发行版: {distro}", "debug")

        deploy_dirs = self._find_all_deploy_dirs(distro)
        if not deploy_dirs:
            self.log_received.emit(f"[实例扫描] {distro}: 未发现 nekro_agent 部署目录，跳过", "debug")
            return []

        self.log_received.emit(f"[实例扫描] {distro}: 发现 {len(deploy_dirs)} 个部署目录", "debug")

        socket_exists = self._wsl_exec(
            distro,
            "test -S /var/run/docker.sock && echo yes || echo no",
            user="root",
        ).strip()
        docker_running = socket_exists == "yes"

        all_containers = {}
        if docker_running:
            try:
                ps_proc = subprocess.run(
                    [
                        "wsl", "-d", distro, "-u", "root", "--", "bash", "-c",
                        "docker ps -a --filter 'name=nekro' --format '{{.Names}}\\t{{.Status}}' 2>/dev/null",
                    ],
                    capture_output=True,
                    timeout=10,
                    creationflags=self._creation_flags(),
                )
                containers_raw = self._clean_command_output(self._safe_decode(ps_proc.stdout))
                if ps_proc.returncode != 0:
                    self.log_received.emit(
                        self._format_command_failure(
                            f"[实例扫描] {distro}: 查询 Docker 容器失败",
                            cmd="docker ps -a --filter 'name=nekro' --format '{{.Names}}\\t{{.Status}}' 2>/dev/null",
                            distro=distro,
                            user="root",
                            timeout=10,
                            returncode=ps_proc.returncode,
                            stdout=ps_proc.stdout,
                            stderr=ps_proc.stderr,
                        ),
                        "debug",
                    )
                if containers_raw:
                    for line in containers_raw.splitlines():
                        parts = line.split("\t", 1)
                        if len(parts) == 2:
                            all_containers[parts[0].strip()] = parts[1].strip()
            except Exception:
                pass
        else:
            self.log_received.emit(f"[实例扫描] {distro}: Docker daemon 未运行，以文件检测结果为准", "debug")

        instances = []
        for deploy_dir in deploy_dirs:
            inst = self._scan_single_deploy(distro, deploy_dir, docker_running, all_containers)
            if inst:
                instances.append(inst)
        return instances

    def _scan_single_deploy(
        self,
        distro: str,
        deploy_dir: str,
        docker_running: bool,
        all_containers: dict,
    ) -> dict | None:
        """扫描单个部署目录并返回实例信息。"""
        deploy_mode = self._detect_deploy_mode_from_compose(distro, deploy_dir)

        env_path = self._find_env_path(distro, deploy_dir)
        env = {}
        raw_env = ""
        if env_path:
            raw_env = self._wsl_exec(distro, f'cat "{env_path}"', user="root")
            env = self._parse_env_values(raw_env)

        data_dir = env.get("NEKRO_DATA_DIR") or "/root/nekro_agent_data"
        instance_name = env.get("INSTANCE_NAME", "")

        status = "stopped"
        if docker_running:
            agent_container = f"{instance_name}nekro_agent"
            container_status = all_containers.get(agent_container, "")
            if container_status:
                status = "running" if "up" in container_status.lower() else "stopped"
                self.log_received.emit(
                    f"[实例扫描] {distro}/{deploy_dir}: 容器 {agent_container} 状态: {container_status}", "debug"
                )

        agent_image = self._detect_agent_image(distro, deploy_dir)

        return {
            "distro": distro,
            "is_managed": distro == DISTRO_NAME,
            "deploy_mode": deploy_mode,
            "deploy_dir": deploy_dir,
            "data_dir": data_dir,
            "instance_name": instance_name,
            "env": env,
            "raw_env": raw_env,
            "status": status,
            "agent_image": agent_image,
        }

    def _find_all_deploy_dirs(self, distro: str) -> list:
        """在发行版内查找所有 nekro_agent 部署目录，不启动任何服务。"""
        try:
            result = self._wsl_exec(
                distro,
                "find /root /home /opt -maxdepth 4 -name 'docker-compose.yml' -print0 2>/dev/null"
                " | xargs -0 grep -l 'nekro' 2>/dev/null",
                user="root",
            )
            dirs = []
            for line in result.strip().splitlines():
                path = line.strip()
                if path:
                    d = os.path.dirname(path)
                    if d and d not in dirs:
                        dirs.append(d)
            return dirs
        except Exception:
            pass
        return []

    def _detect_deploy_mode_from_compose(self, distro: str, deploy_dir: str) -> str:
        """从 docker-compose.yml 内容推断 deploy_mode，不依赖运行中的容器。"""
        compose_content = self._wsl_exec(
            distro,
            f"cat {shlex.quote(posixpath.join(deploy_dir, 'docker-compose.yml'))} 2>/dev/null",
            user="root",
        )
        if "napcat" in compose_content.lower():
            return "napcat"
        return "lite"

    def _detect_agent_image(self, distro: str, deploy_dir: str) -> str:
        """从 docker-compose.yml 中读取实际使用的 nekro-agent 镜像引用。"""
        compose_content = self._wsl_exec(
            distro,
            f"cat {shlex.quote(posixpath.join(deploy_dir, 'docker-compose.yml'))} 2>/dev/null",
            user="root",
        )
        for line in compose_content.splitlines():
            stripped = line.strip()
            if stripped.startswith("image:") and "nekro-agent" in stripped:
                image = stripped.split("image:", 1)[1].strip()
                return image
        return "kromiose/nekro-agent:latest"

    def _find_env_path(self, distro: str, deploy_dir: str) -> str:
        """定位 .env 文件路径。"""
        standard = f"{deploy_dir}/.env"
        check = self._wsl_exec(
            distro,
            f"test -f {shlex.quote(standard)} && echo yes",
            user="root",
        )
        if check.strip() == "yes":
            return standard

        found = self._wsl_exec(
            distro,
            f'find /root -maxdepth 4 -name ".env" -path "*/nekro_agent/*" 2>/dev/null | head -1',
            user="root",
        )
        return found.strip() or ""

    # ------------------------------------------------------------------ #
    # 接管：场景 A（NekroAgent 发行版内的实例）
    # ------------------------------------------------------------------ #

    def _takeover_managed(self, instance: dict, on_step=None) -> bool:
        """接管已在 NekroAgent 发行版内的实例（无需迁移，仅同步 config）。"""
        if on_step:
            on_step(1, 1, "同步配置...")
        self.progress_updated.emit("正在接管已有实例...")
        self.log_received.emit("[接管] 场景A：接管 NekroAgent 发行版内的实例", "info")

        env = instance["env"]
        self._sync_config_from_env(env, instance["deploy_mode"], instance.get("agent_image", ""), instance)

        self.log_received.emit("[接管] ✓ 配置已同步，等待启动器接管", "info")
        self.progress_updated.emit("接管完成！")
        return True

    # ------------------------------------------------------------------ #
    # 接管：场景 B（其他发行版内的实例 → 迁移到 NekroAgent 发行版）
    # ------------------------------------------------------------------ #

    _FOREIGN_TOTAL_STEPS = 7

    def _takeover_foreign(self, instance: dict, on_step=None) -> bool:
        """将其他 WSL 发行版中的 nekro-agent 数据迁移到 NekroAgent 发行版。"""
        src_distro = instance["distro"]
        deploy_dir = instance["deploy_dir"]
        data_dir = instance["data_dir"]
        total = self._FOREIGN_TOTAL_STEPS

        def _step(idx, desc):
            self.log_received.emit(f"[接管] {idx}/{total} {desc}", "info")
            self.progress_updated.emit(desc)
            if on_step:
                on_step(idx, total, desc)

        self.log_received.emit(f"[接管] 场景B：从 {src_distro} 迁移到 {DISTRO_NAME}", "info")

        # 1. 确保 NekroAgent 发行版存在
        if not self._distro_exists():
            self.log_received.emit("[接管] NekroAgent 发行版不存在，需先创建", "info")
            self.progress_updated.emit("__need_create_runtime__")
            return False

        # 2. 停止源实例
        _step(1, "停止源实例容器...")
        if instance.get("status") == "running":
            stop_ok = self._stop_source_instance(src_distro, deploy_dir)
            if not stop_ok:
                self.log_received.emit("[接管] ⚠ 源实例停止失败，将继续迁移（数据可能不完整）", "warn")
        else:
            self.log_received.emit("[接管] 源实例未运行，跳过停止步骤", "debug")

        # 3. 迁移镜像（docker save → docker load，跳过拉取）
        _step(2, "迁移 Docker 镜像...")
        images_ok = self._migrate_images(src_distro)
        if not images_ok:
            self.log_received.emit(
                "[接管] ⚠ 镜像迁移未完成，后续首次启动将需要联网拉取镜像",
                "warn",
            )

        # 4. 打包数据到共享目录（包含 deploy_dir + data_dir + volumes）
        _step(3, "打包源实例数据...")
        archive_path = "/mnt/wsl/na_migrate.tar.gz"
        if not self._pack_source_data(src_distro, deploy_dir, data_dir, archive_path, instance):
            self.log_received.emit("[接管] /mnt/wsl 不可用，尝试 Windows 临时目录中转...", "warn")
            archive_path = self._pack_via_windows_temp(src_distro, deploy_dir, data_dir, instance)
            if not archive_path:
                self.log_received.emit("[接管] ✗ 数据打包失败", "error")
                return False

        # 5. 计算目标路径（保留源 INSTANCE_NAME 以确保 volume 名称一致）
        instance_name = instance.get("instance_name") or instance.get("env", {}).get("INSTANCE_NAME", "")
        if instance_name and not instance_name.endswith("_"):
            instance_name += "_"
        dest_deploy_dir = f"/root/{instance_name}nekro_agent" if instance_name else "/root/nekro_agent"
        dest_data_dir = f"/root/{instance_name}nekro_agent_data" if instance_name else "/root/nekro_agent_data"

        # 6. 在 NekroAgent 发行版中解压（Docker daemon 尚未启动，避免 metadata 冲突）
        _step(4, "还原数据到目标环境...")
        if not self._restore_data(archive_path):
            self.log_received.emit("[接管] ✗ 数据还原失败", "error")
            return False

        # 7. 将解压出的数据移动到 /root 下的标准路径
        _step(5, "整理目录结构...")
        if not self._relocate_dir(data_dir, dest_data_dir, timeout=300):
            self.log_received.emit("[接管] ✗ 数据目录整理失败", "error")
            return False
        if not self._relocate_dir(deploy_dir, dest_deploy_dir):
            self.log_received.emit("[接管] ✗ 部署目录整理失败", "error")
            return False

        # 8. 清理临时文件
        _step(6, "清理临时文件...")
        self._cleanup_archive(src_distro, archive_path)

        # 9. 写入 .env 到目标发行版，保留原始凭据但修正数据目录路径
        _step(7, "写入配置并同步...")
        raw_env = instance.get("raw_env", "")
        if raw_env:
            fixed_env = self._rewrite_env_data_dir(raw_env, dest_data_dir)
            self._run_wsl_checked(
                DISTRO_NAME,
                f"mkdir -p {shlex.quote(dest_deploy_dir)}",
                action="[接管] 创建目标部署目录失败",
                user="root",
                timeout=30,
            )
            self._write_to_wsl(DISTRO_NAME, fixed_env, f"{dest_deploy_dir}/.env")
            self.log_received.emit("[接管] ✓ 原始 .env 已写入目标发行版（数据目录已修正）", "info")
        else:
            self.log_received.emit("[接管] ⚠ 未获取到原始 .env，凭据将在首次启动时重新生成", "warn")

        env = instance["env"]
        normalized_instance = {**instance, "deploy_dir": dest_deploy_dir, "data_dir": dest_data_dir}
        self._sync_config_from_env(env, instance["deploy_mode"], instance.get("agent_image", ""), normalized_instance)

        self.log_received.emit("[接管] ✓ 迁移完成，启动器接管", "info")
        self.progress_updated.emit("迁移完成！")
        return True

    def _relocate_dir(self, src, dest, timeout=120):
        """将 src 目录移动到 dest（同路径则跳过），优先 mv 快速重命名。"""
        src = self._normalize_wsl_abs_path(src)
        dest = self._normalize_wsl_abs_path(dest)
        if src == dest:
            return True
        if not self._is_safe_migration_path(src):
            self.log_received.emit(f"[接管] 拒绝移动高风险源路径: {src or '<empty>'}", "error")
            return False
        if not self._is_safe_migration_path(dest) or not dest.startswith("/root/"):
            self.log_received.emit(f"[接管] 拒绝移动到非托管目标路径: {dest or '<empty>'}", "error")
            return False
        if not self._wsl_path_exists(DISTRO_NAME, src, user="root"):
            self.log_received.emit(f"[接管] 待整理目录不存在: {src}", "error")
            return False

        self.log_received.emit(f"[接管] 移动目录: {src} → {dest}", "info")
        dest_parent = posixpath.dirname(dest.rstrip("/")) or "/"
        self._run_wsl_checked(
            DISTRO_NAME,
            f"mkdir -p {shlex.quote(dest_parent)}",
            action="[接管] 创建目标父目录失败",
            timeout=30,
            user="root",
        )
        proc = self._wsl_run(
            DISTRO_NAME,
            f"mv -T {shlex.quote(src)} {shlex.quote(dest)} 2>/dev/null",
            timeout=timeout,
            user="root",
        )
        if proc.returncode == 0:
            return True

        self._run_wsl_checked(
            DISTRO_NAME,
            f"mkdir -p {shlex.quote(dest)}",
            action="[接管] 创建目标目录失败",
            timeout=30,
            user="root",
        )
        fallback_cmd = (
            f"cp -a {shlex.quote(src.rstrip('/') + '/.')} "
            f"{shlex.quote(dest.rstrip('/') + '/')} && "
            f"rm -rf {shlex.quote(src)}"
        )
        mv_result = self._wsl_run(
            DISTRO_NAME,
            fallback_cmd,
            timeout=timeout,
            user="root",
        )
        if mv_result.returncode != 0:
            self.log_received.emit(
                self._format_command_failure(
                    "[接管] 目录移动失败",
                    cmd=fallback_cmd,
                    distro=DISTRO_NAME,
                    user="root",
                    timeout=timeout,
                    returncode=mv_result.returncode,
                    stdout=mv_result.stdout,
                    stderr=mv_result.stderr,
                ),
                "error",
            )
            return False
        return True

    def _stop_source_instance(self, distro: str, deploy_dir: str) -> bool:
        """停止源发行版中的 nekro-agent 容器，兼容新版插件式和旧版独立 docker-compose。"""
        cmd = (
            f"cd {shlex.quote(deploy_dir)} && "
            f'if docker compose version >/dev/null 2>&1; then '
            f'  docker compose -f docker-compose.yml stop 2>&1; '
            f'else '
            f'  docker-compose -f docker-compose.yml stop 2>&1; '
            f'fi'
        )
        try:
            proc = subprocess.run(
                ["wsl", "-d", distro, "-u", "root", "--", "bash", "-c", cmd],
                capture_output=True,
                timeout=60,
                creationflags=self._creation_flags(),
            )
            if proc.returncode != 0:
                self.log_received.emit(
                    self._format_command_failure(
                        "[接管] 停止源实例失败",
                        cmd=cmd,
                        distro=distro,
                        user="root",
                        timeout=60,
                        returncode=proc.returncode,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                    ),
                    "debug",
                )
                return False
            return True
        except Exception as e:
            self.log_received.emit(f"[接管] 停止源实例异常: {e}", "debug")
            return False

    def _migrate_images(self, src_distro: str) -> bool:
        """将源发行版中的 nekro/napcat/postgres/qdrant 镜像通过 /mnt/wsl 导入到 NekroAgent 发行版。

        返回 True 表示镜像迁移成功（或无需迁移），False 表示失败（后续需联网拉取）。
        不会中断接管流程：start_services 会按需拉取缺失镜像。
        """
        try:
            images_raw = self._wsl_exec(
                src_distro,
                "docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null",
                user="root",
            )
            images = [
                img.strip() for img in images_raw.splitlines()
                if img.strip() and not img.strip().endswith(":<none>")
                and any(kw in img.lower() for kw in ("nekro-agent", "napcat", "postgres", "qdrant"))
            ]
            if not images:
                self.log_received.emit("[接管] 源发行版中未发现相关镜像，跳过镜像迁移", "debug")
                return True

            self.log_received.emit(f"[接管] 待迁移镜像: {images}", "info")

            check = self._wsl_exec(src_distro, "test -d /mnt/wsl && echo yes", user="root")
            if check.strip() != "yes":
                self.log_received.emit("[接管] /mnt/wsl 不可用，跳过镜像迁移", "warn")
                return False

            image_archive = "/mnt/wsl/na_images.tar.gz"
            image_list = " ".join(shlex.quote(img) for img in images)
            save_cmd = f"docker save {image_list} | gzip > {shlex.quote(image_archive)}"
            save_proc = self._wsl_run(src_distro, save_cmd, timeout=600, user="root")
            if save_proc.returncode != 0:
                self.log_received.emit(
                    self._format_command_failure(
                        "[接管] 镜像导出失败，跳过镜像迁移",
                        cmd=save_cmd,
                        distro=src_distro,
                        user="root",
                        timeout=600,
                        returncode=save_proc.returncode,
                        stdout=save_proc.stdout,
                        stderr=save_proc.stderr,
                    ),
                    "warn",
                )
                return False

            verify = self._wsl_exec(src_distro, f'test -f "{image_archive}" && echo yes', user="root")
            if verify.strip() != "yes":
                self.log_received.emit("[接管] 镜像包未生成，跳过镜像迁移", "warn")
                return False

            load_cmd = f"gzip -dc {shlex.quote(image_archive)} | docker load"
            load_proc = self._wsl_run(DISTRO_NAME, load_cmd, timeout=600, user="root")
            ok = load_proc.returncode == 0
            if ok:
                self.log_received.emit("[接管] ✓ 镜像迁移完成", "info")
            else:
                self.log_received.emit(
                    self._format_command_failure(
                        "[接管] 镜像导入失败",
                        cmd=load_cmd,
                        distro=DISTRO_NAME,
                        user="root",
                        timeout=600,
                        returncode=load_proc.returncode,
                        stdout=load_proc.stdout,
                        stderr=load_proc.stderr,
                    ),
                    "warn",
                )

            self._wsl_exec(
                src_distro,
                f"rm -f {shlex.quote(image_archive)} 2>/dev/null",
                user="root",
            )
            self._wsl_exec(
                DISTRO_NAME,
                f"rm -f {shlex.quote(image_archive)} 2>/dev/null",
                user="root",
            )
            return ok
        except Exception as e:
            self.log_received.emit(f"[接管] 镜像迁移异常: {e}", "warn")
            return False

    def _migration_archive_targets(
        self,
        distro: str,
        deploy_dir: str,
        data_dir: str,
        instance: dict,
    ) -> list[str]:
        targets = []
        required = [
            ("部署目录", deploy_dir),
            ("数据目录", data_dir),
        ]
        for label, path in required:
            normalized = self._normalize_wsl_abs_path(path)
            if not self._is_safe_migration_path(normalized):
                self.log_received.emit(
                    f"[接管] {label}路径不符合迁移安全规则: {path or '<empty>'}",
                    "error",
                )
                return []
            if not self._wsl_path_exists(distro, normalized, user="root"):
                self.log_received.emit(
                    f"[接管] {label}不存在，无法迁移: {normalized}",
                    "error",
                )
                return []
            targets.append(normalized)

        instance_name = instance.get("env", {}).get("INSTANCE_NAME", "")
        optional_volumes = [
            f"/var/lib/docker/volumes/{instance_name}nekro_postgres_data",
            f"/var/lib/docker/volumes/{instance_name}nekro_qdrant_data",
        ]
        for volume in optional_volumes:
            if self._wsl_path_exists(distro, volume, user="root"):
                targets.append(volume)
            else:
                self.log_received.emit(
                    f"[接管] 可选 Docker volume 不存在，跳过: {volume}",
                    "debug",
                )
        return targets

    def _pack_source_data(
        self,
        src_distro: str,
        deploy_dir: str,
        data_dir: str,
        archive_path: str,
        instance: dict,
    ) -> bool:
        """在源发行版中以 root 打包数据到 /mnt/wsl 共享路径。

        打包内容：deploy_dir（含 docker-compose.yml, .env）、data_dir、相关 docker volumes。
        """
        try:
            check = self._wsl_exec(src_distro, "test -d /mnt/wsl && echo yes", user="root")
            if check.strip() != "yes":
                return False

            targets = self._migration_archive_targets(
                src_distro,
                deploy_dir,
                data_dir,
                instance,
            )
            if not targets:
                return False

            pack_cmd = (
                f"tar -czf {shlex.quote(archive_path)} "
                + " ".join(shlex.quote(target) for target in targets)
            )
            proc = self._wsl_run(src_distro, pack_cmd, timeout=300, user="root")
            if proc.returncode != 0:
                self.log_received.emit(
                    self._format_command_failure(
                        "[接管] 源实例数据打包失败",
                        cmd=pack_cmd,
                        distro=src_distro,
                        user="root",
                        timeout=300,
                        returncode=proc.returncode,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                    ),
                    "debug",
                )
                return False

            verify = self._wsl_exec(
                src_distro,
                f"test -f {shlex.quote(archive_path)} && echo yes",
                user="root",
            )
            ok = verify.strip() == "yes"
            if not ok:
                self.log_received.emit(
                    f"[接管] 打包命令完成但未找到归档: {archive_path}",
                    "debug",
                )
            return ok
        except Exception as e:
            self.log_received.emit(f"[接管] 打包异常: {e}", "debug")
            return False

    def _pack_via_windows_temp(self, src_distro: str, deploy_dir: str, data_dir: str, instance: dict) -> str:
        """通过 Windows %TEMP% 目录作为中转，全程以 root 执行，失败返回空串。"""
        import tempfile
        try:
            win_temp = tempfile.gettempdir()
            wsl_temp = self._wsl_exec(
                src_distro,
                f'wslpath "{win_temp.replace(chr(92), "/")}"',
                user="root",
            ).strip()
            if not wsl_temp:
                return ""

            archive_win = os.path.join(win_temp, "na_migrate.tar.gz")
            archive_wsl_src = f"{wsl_temp}/na_migrate.tar.gz"

            targets = self._migration_archive_targets(
                src_distro,
                deploy_dir,
                data_dir,
                instance,
            )
            if not targets:
                return ""

            pack_cmd = (
                f"tar -czf {shlex.quote(archive_wsl_src)} "
                + " ".join(shlex.quote(target) for target in targets)
            )
            proc = self._wsl_run(src_distro, pack_cmd, timeout=300, user="root")
            if proc.returncode != 0:
                self.log_received.emit(
                    self._format_command_failure(
                        "[接管] Windows 临时目录打包失败",
                        cmd=pack_cmd,
                        distro=src_distro,
                        user="root",
                        timeout=300,
                        returncode=proc.returncode,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                    ),
                    "debug",
                )
                return ""

            verify = self._wsl_exec(
                src_distro,
                f"test -f {shlex.quote(archive_wsl_src)} && echo yes",
                user="root",
            )
            if verify.strip() != "yes":
                self.log_received.emit(
                    f"[接管] Windows 临时目录打包完成但未找到归档: {archive_wsl_src}",
                    "debug",
                )
                return ""

            archive_wsl_dst = self._wsl_exec(
                DISTRO_NAME,
                f'wslpath "{archive_win.replace(chr(92), "/")}"',
                user="root",
            ).strip()
            return archive_wsl_dst or ""
        except Exception as e:
            self.log_received.emit(f"[接管] Windows 临时目录中转失败: {e}", "debug")
            return ""

    def _restore_data(self, archive_path: str) -> bool:
        """在 NekroAgent 发行版中以 root 解压迁移包。

        确保 Docker daemon 已停止再解压，避免 daemon 内部 metadata 与磁盘目录不一致。
        解压完毕后启动 Docker daemon，让它重新扫描 volumes 目录注册已有 volume。
        """
        try:
            self._wsl_exec(DISTRO_NAME, "systemctl stop docker 2>/dev/null || true", timeout=20, user="root")

            restore_cmd = f"tar -xzf {shlex.quote(archive_path)} -C /"
            proc = self._wsl_run(DISTRO_NAME, restore_cmd, timeout=300, user="root")
            if proc.returncode != 0:
                self.log_received.emit(
                    self._format_command_failure(
                        "[接管] 数据还原失败",
                        cmd=restore_cmd,
                        distro=DISTRO_NAME,
                        user="root",
                        timeout=300,
                        returncode=proc.returncode,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                    ),
                    "debug",
                )
                return False

            self._run_wsl_checked(
                DISTRO_NAME,
                "systemctl start docker",
                action="[接管] 还原后启动 Docker 失败",
                timeout=30,
                user="root",
            )
            time.sleep(2)
            return True
        except Exception as e:
            self.log_received.emit(f"[接管] 数据还原异常: {e}", "debug")
            return False

    def _cleanup_archive(self, src_distro: str, archive_path: str):
        """清理临时打包文件（以 root 执行，文件可能由 root 创建）。"""
        try:
            self._wsl_exec(
                src_distro,
                f"rm -f {shlex.quote(archive_path)} 2>/dev/null",
                user="root",
            )
            self._wsl_exec(
                DISTRO_NAME,
                f"rm -f {shlex.quote(archive_path)} 2>/dev/null",
                user="root",
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 公共工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _rewrite_env_data_dir(raw_env: str, new_data_dir: str) -> str:
        """将 .env 内容中的 NEKRO_DATA_DIR 替换为目标路径。"""
        lines = []
        for line in raw_env.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and stripped.startswith("NEKRO_DATA_DIR="):
                lines.append(f"NEKRO_DATA_DIR={new_data_dir}")
            else:
                lines.append(line)
        return "\n".join(lines) + "\n"

    def _sync_config_from_env(self, env: dict, deploy_mode: str, agent_image: str = "", instance: dict | None = None):
        """将从 .env 读取的凭据写入 config 和 deploy_info，同时注册到多实例配置。"""
        if not self.config:
            return

        nekro_port = normalize_port(env.get("NEKRO_EXPOSE_PORT"), 8021)
        napcat_port = normalize_port(env.get("NAPCAT_EXPOSE_PORT"), 6099)
        instance_name = env.get("INSTANCE_NAME", "")

        is_preview = agent_image == PREVIEW_IMAGE
        release_channel = "preview" if is_preview else "stable"

        deploy_dir = (instance or {}).get("deploy_dir", "/root/nekro_agent")
        data_dir = (instance or {}).get("data_dir", "/root/nekro_agent_data")

        deploy_info = {
            "port": str(nekro_port),
            "admin_password": env.get("NEKRO_ADMIN_PASSWORD", ""),
            "onebot_token": env.get("ONEBOT_ACCESS_TOKEN", ""),
            "deploy_mode": deploy_mode,
        }
        if deploy_mode == "napcat":
            deploy_info["napcat_port"] = str(napcat_port)

        inst_id = instance_name or "default"
        existing = self.config.get_instance(inst_id)
        if existing and existing.get("deploy_dir") != deploy_dir:
            inst_id = self.config.next_instance_id()
            existing = None
        inst_data = {
            "instance_name": instance_name,
            "deploy_dir": deploy_dir,
            "data_dir": data_dir,
            "deploy_mode": deploy_mode,
            "nekro_port": nekro_port,
            "napcat_port": napcat_port,
            "release_channel": release_channel,
            "deploy_info": deploy_info,
            "preview_backup_available": bool(
                existing.get("preview_backup_available", False) if existing else False
            ),
        }
        global_updates = {
            "first_run": False,
            "deploy_mode": deploy_mode,
            "nekro_port": nekro_port,
            "napcat_port": napcat_port,
            "wsl_distro": DISTRO_NAME,
            "release_channel": release_channel,
            "deploy_info": deploy_info,
            "active_instance": inst_id,
            "preview_backup_available": bool(
                inst_data.get("preview_backup_available", False)
            ),
        }
        if not self.config.get_default_instance_id():
            global_updates["default_instance"] = inst_id
        self.config.update_instance_with_globals(
            inst_id,
            instance_updates=inst_data,
            global_updates=global_updates,
        )

        self.log_received.emit(
            f"[接管] config 已同步 — 实例: {inst_id}, 端口: {nekro_port}, 模式: {deploy_mode}, 频道: {release_channel}", "info"
        )
