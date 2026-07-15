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
                f"{{ test -e {shlex.quote(normalized)} || "
                f"test -L {shlex.quote(normalized)}; }} && echo yes",
                user=user,
            ).strip()
            == "yes"
        )

    @staticmethod
    def _migration_instance_prefix(instance: dict) -> str:
        instance_name = instance.get("instance_name") or instance.get("env", {}).get("INSTANCE_NAME", "")
        if instance_name and not instance_name.endswith("_"):
            instance_name += "_"
        return instance_name

    def _migration_destination_paths(self, instance: dict) -> tuple[str, str, list[str]]:
        instance_prefix = self._migration_instance_prefix(instance)
        volume_prefix = instance.get("instance_name") or instance.get("env", {}).get("INSTANCE_NAME", "")
        dest_deploy_dir = (
            f"/root/{instance_prefix}nekro_agent"
            if instance_prefix
            else "/root/nekro_agent"
        )
        dest_data_dir = (
            f"/root/{instance_prefix}nekro_agent_data"
            if instance_prefix
            else "/root/nekro_agent_data"
        )
        volume_dirs = [
            f"/var/lib/docker/volumes/{volume_prefix}nekro_postgres_data",
            f"/var/lib/docker/volumes/{volume_prefix}nekro_qdrant_data",
        ]
        return dest_deploy_dir, dest_data_dir, volume_dirs

    def _find_migration_destination_conflicts(self, paths: list[str]) -> list[str] | None:
        normalized_paths = [self._normalize_wsl_abs_path(path) for path in paths]
        if any(not path for path in normalized_paths):
            self.log_received.emit("[接管] 目标路径无效，无法执行冲突检测", "error")
            return None

        candidates = " ".join(shlex.quote(path) for path in normalized_paths)
        cmd = (
            f"for candidate in {candidates}; do "
            "if [ -e \"$candidate\" ] || [ -L \"$candidate\" ]; then "
            "printf '%s\\n' \"$candidate\"; fi; done"
        )
        try:
            proc = self._run_wsl_checked(
                DISTRO_NAME,
                cmd,
                action="[接管] 检测目标路径冲突失败",
                timeout=30,
                user="root",
            )
        except Exception as e:
            self.log_received.emit(f"[接管] 无法确认目标环境是否可安全迁移:\n{e}", "error")
            return None

        output = self._safe_decode(proc.stdout)
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _check_migration_destination_conflicts(self, paths: list[str]) -> bool:
        conflicts = self._find_migration_destination_conflicts(paths)
        if conflicts is None:
            return False
        if not conflicts:
            return True

        conflict_list = "\n".join(f"- {path}" for path in conflicts)
        self.log_received.emit(
            "[接管] 目标环境中已存在同名实例数据，已中止迁移以避免覆盖或合并:\n"
            f"{conflict_list}\n"
            "请先在 NekroAgent 发行版中备份并移走这些目录/卷，"
            "或修改源实例的 INSTANCE_NAME 后重新扫描并迁移。",
            "error",
        )
        return False

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
        self.log_received.emit("[实例扫描] 开始扫描非启动器/已有 WSL 部署", "info")
        distros = self._get_wsl_distros()
        if distros:
            self.log_received.emit(
                f"[实例扫描] 待扫描发行版 {len(distros)} 个: {', '.join(distros)}",
                "info",
            )
        else:
            self.log_received.emit("[实例扫描] 未获取到可扫描的 WSL 发行版", "warn")
        instances = []
        total = len(distros)
        for i, distro in enumerate(distros, 1):
            if distro.lower() in {"docker-desktop", "docker-desktop-data"}:
                self.log_received.emit(f"[实例扫描] {distro}: Docker Desktop 系统发行版，跳过", "debug")
                continue
            if on_step:
                on_step(f"正在检测发行版: {distro}（{i}/{total}）")
            try:
                results = self._scan_distro(distro)
                self.log_received.emit(
                    f"[实例扫描] {distro}: 扫描完成，发现 {len(results)} 个候选实例",
                    "info" if results else "debug",
                )
                if results:
                    instances.extend(results)
            except Exception as e:
                self.log_received.emit(f"[实例扫描] 扫描 {distro} 时异常: {type(e).__name__}: {e}", "warn")
        self.log_received.emit(
            f"[实例扫描] 扫描结束，共发现 {len(instances)} 个可接管实例",
            "info",
        )
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
            self.log_received.emit(f"[实例扫描] WSL 发行版列表解析结果: {distros}", "debug")
            return distros
        except Exception as e:
            self.log_received.emit(f"[实例扫描] 获取发行版列表失败: {e}", "debug")
            return []

    def _run_scan_command(self, distro: str, cmd: str, desc: str, *, timeout=15, user="root") -> str | None:
        try:
            proc = self._wsl_run(distro, cmd, timeout=timeout, user=user)
        except Exception as e:
            self.log_received.emit(
                f"[实例扫描] {distro}: {desc}异常: {type(e).__name__}: {e}",
                "debug",
            )
            return None

        if proc.returncode != 0:
            self.log_received.emit(
                self._format_command_failure(
                    f"[实例扫描] {distro}: {desc}失败",
                    cmd=cmd,
                    distro=distro,
                    user=user,
                    timeout=timeout,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                ),
                "debug",
            )
            return None
        return self._clean_command_output(self._safe_decode(proc.stdout))

    def _is_scan_candidate_path(self, path: str) -> bool:
        normalized = self._normalize_wsl_abs_path(path)
        return bool(
            normalized
            and (
                normalized.startswith("/root/")
                or normalized.startswith("/home/")
                or normalized.startswith("/opt/")
            )
        )

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

        socket_exists = self._run_scan_command(
            distro,
            "test -S /var/run/docker.sock && echo yes || echo no",
            "检测 Docker socket",
            user="root",
        )
        socket_exists = (socket_exists or "").strip()
        docker_running = socket_exists == "yes"
        self.log_received.emit(
            f"[实例扫描] {distro}: Docker socket 检测结果={socket_exists or '<empty>'}",
            "debug",
        )

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
                if all_containers:
                    self.log_received.emit(
                        f"[实例扫描] {distro}: 匹配到 {len(all_containers)} 个 nekro 相关容器: "
                        + ", ".join(f"{name}={status}" for name, status in all_containers.items()),
                        "debug",
                    )
                else:
                    self.log_received.emit(f"[实例扫描] {distro}: 未查询到 nekro 相关容器", "debug")
            except Exception as e:
                self.log_received.emit(
                    f"[实例扫描] {distro}: 查询 Docker 容器异常: {type(e).__name__}: {e}",
                    "debug",
                )
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
        self.log_received.emit(f"[实例扫描] {distro}/{deploy_dir}: 开始解析部署目录", "debug")
        deploy_mode = self._detect_deploy_mode_from_compose(distro, deploy_dir)
        self.log_received.emit(
            f"[实例扫描] {distro}/{deploy_dir}: 部署模式推断为 {deploy_mode}",
            "debug",
        )

        env_path = self._find_env_path(distro, deploy_dir)
        env = {}
        raw_env = ""
        if env_path:
            self.log_received.emit(f"[实例扫描] {distro}/{deploy_dir}: 读取 env: {env_path}", "debug")
            raw_env = self._run_scan_command(
                distro,
                f"cat {shlex.quote(env_path)}",
                f"读取 env 文件 {env_path}",
                user="root",
            ) or ""
            env = self._parse_env_values(raw_env)
            self.log_received.emit(
                f"[实例扫描] {distro}/{deploy_dir}: env key 数量={len(env)}",
                "debug",
            )
        else:
            self.log_received.emit(
                f"[实例扫描] {distro}/{deploy_dir}: 未找到 .env，将使用默认数据目录推断",
                "warn",
            )

        data_dir = env.get("NEKRO_DATA_DIR") or "/root/nekro_agent_data"
        instance_name = env.get("INSTANCE_NAME", "")
        self.log_received.emit(
            f"[实例扫描] {distro}/{deploy_dir}: data_dir={data_dir}, instance_name={instance_name or '<empty>'}",
            "debug",
        )

        status = "stopped"
        if docker_running:
            agent_container = f"{instance_name}nekro_agent"
            container_status = all_containers.get(agent_container, "")
            if container_status:
                status = "running" if "up" in container_status.lower() else "stopped"
                self.log_received.emit(
                    f"[实例扫描] {distro}/{deploy_dir}: 容器 {agent_container} 状态: {container_status}", "debug"
                )
            else:
                self.log_received.emit(
                    f"[实例扫描] {distro}/{deploy_dir}: 未匹配到容器 {agent_container}",
                    "debug",
                )

        agent_image = self._detect_agent_image(distro, deploy_dir)
        self.log_received.emit(
            f"[实例扫描] {distro}/{deploy_dir}: agent_image={agent_image}, status={status}",
            "debug",
        )

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
        compose_find_cmd = (
            "find /root /home /opt -maxdepth 5 "
            "\\( -name 'docker-compose.yml' -o -name 'docker-compose.yaml' "
            "-o -name 'compose.yml' -o -name 'compose.yaml' \\) "
            "-print 2>/dev/null"
        )
        try:
            all_compose = self._run_scan_command(
                distro,
                compose_find_cmd,
                "查找 compose 文件候选",
                timeout=20,
                user="root",
            )
            if all_compose is None:
                return []
            compose_candidates = [
                line.strip()
                for line in all_compose.splitlines()
                if self._is_scan_candidate_path(line.strip())
            ]
            ignored_candidates = [
                line.strip()
                for line in all_compose.splitlines()
                if line.strip() and not self._is_scan_candidate_path(line.strip())
            ]
            for path in ignored_candidates[:5]:
                self.log_received.emit(
                    f"[实例扫描] {distro}: 忽略非法 compose 候选输出: {path}",
                    "debug",
                )
            if compose_candidates:
                self.log_received.emit(
                    f"[实例扫描] {distro}: 发现 {len(compose_candidates)} 个 compose 文件候选",
                    "debug",
                )
                for path in compose_candidates[:20]:
                    self.log_received.emit(f"[实例扫描] {distro}: compose 候选 {path}", "debug")
                if len(compose_candidates) > 20:
                    self.log_received.emit(
                        f"[实例扫描] {distro}: compose 候选过多，已省略 {len(compose_candidates) - 20} 个",
                        "debug",
                    )
            else:
                self.log_received.emit(
                    f"[实例扫描] {distro}: /root /home /opt 下未发现 compose 文件候选",
                    "debug",
                )

            result = self._run_scan_command(
                distro,
                compose_find_cmd
                + " | while IFS= read -r file; do "
                + "grep -qiE 'nekro|nekro-agent' \"$file\" 2>/dev/null && printf '%s\\n' \"$file\"; "
                + "done",
                "筛选 nekro compose 文件",
                timeout=20,
                user="root",
            )
            if result is None:
                return []
            dirs = []
            for line in result.strip().splitlines():
                path = line.strip()
                if self._is_scan_candidate_path(path):
                    d = os.path.dirname(path)
                    if d and d not in dirs:
                        dirs.append(d)
                        self.log_received.emit(
                            f"[实例扫描] {distro}: 命中 nekro compose: {path}",
                            "debug",
                        )
                elif path:
                    self.log_received.emit(
                        f"[实例扫描] {distro}: 忽略非法 nekro compose 输出: {path}",
                        "debug",
                    )
            if compose_candidates and not dirs:
                self.log_received.emit(
                    f"[实例扫描] {distro}: compose 候选均未包含 nekro 关键字，跳过",
                    "debug",
                )
            return dirs
        except Exception as e:
            self.log_received.emit(
                f"[实例扫描] {distro}: 查找部署目录异常: {type(e).__name__}: {e}",
                "debug",
            )
        return []

    def _detect_deploy_mode_from_compose(self, distro: str, deploy_dir: str) -> str:
        """从 docker-compose.yml 内容推断 deploy_mode，不依赖运行中的容器。"""
        compose_content = self._read_first_compose_file(distro, deploy_dir)
        if "napcat" in compose_content.lower():
            return "napcat"
        return "lite"

    def _detect_agent_image(self, distro: str, deploy_dir: str) -> str:
        """从 docker-compose.yml 中读取实际使用的 nekro-agent 镜像引用。"""
        compose_content = self._read_first_compose_file(distro, deploy_dir)
        for line in compose_content.splitlines():
            stripped = line.strip()
            if stripped.startswith("image:") and "nekro-agent" in stripped.lower():
                image = stripped.split("image:", 1)[1].strip()
                return image
        return "kromiose/nekro-agent:latest"

    def _read_first_compose_file(self, distro: str, deploy_dir: str) -> str:
        for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            path = posixpath.join(deploy_dir, name)
            content = self._run_scan_command(
                distro,
                f"test -f {shlex.quote(path)} && cat {shlex.quote(path)} || true",
                f"读取 compose 文件 {path}",
                user="root",
            )
            if content:
                self.log_received.emit(
                    f"[实例扫描] {distro}/{deploy_dir}: 读取 compose 文件 {path}",
                    "debug",
                )
                return content
        self.log_received.emit(
            f"[实例扫描] {distro}/{deploy_dir}: 未能读取 compose 文件内容",
            "debug",
        )
        return ""

    def _find_env_path(self, distro: str, deploy_dir: str) -> str:
        """定位 .env 文件路径。"""
        standard = f"{deploy_dir}/.env"
        check = self._run_scan_command(
            distro,
            f"test -f {shlex.quote(standard)} && echo yes || echo no",
            f"检测 env 文件 {standard}",
            user="root",
        )
        if (check or "").strip() == "yes":
            return standard

        found = self._run_scan_command(
            distro,
            f"find {shlex.quote(deploy_dir)} -maxdepth 2 -name '.env' 2>/dev/null | head -1",
            f"查找部署目录 env {deploy_dir}",
            user="root",
        )
        if found and self._is_scan_candidate_path(found.strip()):
            return found.strip()

        parent = posixpath.dirname(deploy_dir.rstrip("/")) or "/"
        found = self._run_scan_command(
            distro,
            f"find {shlex.quote(parent)} -maxdepth 3 -name '.env' -path '*nekro*' 2>/dev/null | head -1",
            f"查找邻近 env {parent}",
            user="root",
        )
        if found and self._is_scan_candidate_path(found.strip()):
            self.log_received.emit(
                f"[实例扫描] {distro}/{deploy_dir}: 使用邻近 env 文件 {found.strip()}",
                "debug",
            )
            return found.strip()
        if found and found.strip():
            self.log_received.emit(
                f"[实例扫描] {distro}/{deploy_dir}: 忽略非法 env 输出: {found.strip()}",
                "debug",
            )
        return ""

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
        if not self._sync_config_from_env(
            env,
            instance["deploy_mode"],
            instance.get("agent_image", ""),
            instance,
        ):
            self.progress_updated.emit("接管失败：启动器配置保存失败")
            return False

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

        dest_deploy_dir, dest_data_dir, dest_volume_dirs = self._migration_destination_paths(instance)
        if not self._check_migration_destination_conflicts(
            [dest_deploy_dir, dest_data_dir, *dest_volume_dirs]
        ):
            return False

        # 2. 实时检测并停止源实例。扫描结果可能已过期，不能用缓存 status
        # 判断数据是否可以安全打包。
        _step(1, "停止源实例容器...")
        running_source_services = self._get_running_source_services(src_distro, deploy_dir)
        if running_source_services is None:
            self.log_received.emit(
                "[接管] ✗ 无法确认源实例的实时运行状态，已中止迁移；"
                "请根据上方命令输出处理后重试",
                "error",
            )
            return False

        source_stopped = False
        migration_succeeded = False
        migration_committed = False
        staging_dir = ""
        target_docker_was_active = False
        target_docker_prepared = False
        target_docker_stopped_by_recheck = False
        completed_moves: list[tuple[str, str]] = []
        if running_source_services:
            stop_ok = self._stop_source_instance(src_distro, deploy_dir)
            if not stop_ok:
                self.log_received.emit(
                    "[接管] ✗ 源实例停止失败，已中止迁移；"
                    "请根据上方命令输出处理后重试",
                    "error",
                )
                return False
            source_stopped = True
        else:
            self.log_received.emit("[接管] 源实例未运行，跳过停止步骤", "debug")

        try:
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

            # 5. 在独立 staging 目录中解压，避免归档直接写入目标根目录。
            _step(4, "还原数据到目标环境...")
            staging_dir = self._create_migration_staging_dir()
            if not staging_dir:
                return False

            docker_state = self._prepare_target_docker_for_migration()
            if docker_state is None:
                return False
            target_docker_was_active = docker_state
            target_docker_prepared = True

            if not self._restore_data(archive_path, staging_dir):
                self.log_received.emit("[接管] ✗ 数据还原失败", "error")
                return False

            # 6. 将 staging 中的数据原子移动到最终路径；目标存在时绝不覆盖。
            _step(5, "整理目录结构...")
            staged_data_dir = self._staged_migration_path(staging_dir, data_dir)
            staged_deploy_dir = self._staged_migration_path(staging_dir, deploy_dir)
            if not self._relocate_dir(staged_data_dir, dest_data_dir, timeout=300):
                self.log_received.emit("[接管] ✗ 数据目录整理失败", "error")
                return False
            completed_moves.append((staged_data_dir, dest_data_dir))
            if not self._relocate_dir(staged_deploy_dir, dest_deploy_dir):
                self.log_received.emit("[接管] ✗ 部署目录整理失败", "error")
                return False
            completed_moves.append((staged_deploy_dir, dest_deploy_dir))

            staged_volumes = [
                (self._staged_migration_path(staging_dir, volume_dir), volume_dir)
                for volume_dir in dest_volume_dirs
                if self._wsl_path_exists(
                    DISTRO_NAME,
                    self._staged_migration_path(staging_dir, volume_dir),
                    user="root",
                )
            ]
            for staged_volume_dir, volume_dir in staged_volumes:
                stopped_by_recheck = self._ensure_target_docker_stopped_for_volume_change(
                    f"提交 Docker volume 前（{volume_dir}）"
                )
                if stopped_by_recheck is None:
                    return False
                target_docker_stopped_by_recheck |= stopped_by_recheck
                if not self._relocate_dir(staged_volume_dir, volume_dir, timeout=300):
                    self.log_received.emit(f"[接管] ✗ Docker volume 整理失败: {volume_dir}", "error")
                    return False
                completed_moves.append((staged_volume_dir, volume_dir))

            # 7. 清理临时文件
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
            if not self._sync_config_from_env(
                env,
                instance["deploy_mode"],
                instance.get("agent_image", ""),
                normalized_instance,
            ):
                return False

            migration_committed = True

            if target_docker_was_active and not self._restore_target_docker_after_migration():
                return False

            migration_succeeded = True
            self.log_received.emit("[接管] ✓ 迁移完成，启动器接管", "info")
            self.progress_updated.emit("迁移完成！")
            return True
        finally:
            rollback_ok = True
            if not migration_committed and completed_moves:
                rollback_ok, stopped_by_recheck = self._rollback_migration_moves(completed_moves)
                target_docker_stopped_by_recheck |= stopped_by_recheck
            if staging_dir and (migration_committed or rollback_ok):
                self._cleanup_migration_staging_dir(staging_dir)
            if target_docker_prepared and target_docker_was_active and not migration_committed:
                if rollback_ok:
                    self._restore_target_docker_after_migration()
                else:
                    self.log_received.emit(
                        "[接管] 部分目标数据未能回滚，Docker 已保持停止；"
                        "请根据上方具体路径和命令输出完成手动恢复后再启动 Docker",
                        "error",
                    )
            if migration_committed and not migration_succeeded:
                self.log_received.emit(
                    "[接管] 数据与启动器配置已完成迁移，但目标 Docker 未能恢复。"
                    "源实例将保持停止，请根据上方错误手动启动 NekroAgent 发行版中的 Docker。",
                    "error",
                )
            if target_docker_stopped_by_recheck and not target_docker_was_active:
                self.log_received.emit(
                    "[接管] 目标 Docker 在迁移期间被外部启动，已为 volume 安全操作停止；"
                    "由于迁移开始时 Docker 未运行，将保持停止状态。",
                    "warn",
                )
            if source_stopped and not migration_committed:
                self.log_received.emit("[接管] 迁移未完成，正在恢复源实例原运行状态...", "warn")
                if not self._start_source_instance(
                    src_distro,
                    deploy_dir,
                    running_source_services,
                ):
                    self.log_received.emit(
                        "[接管] ✗ 迁移失败且源实例未能自动恢复；"
                        "请根据上方命令输出手动启动源实例",
                        "error",
                    )

    def _relocate_dir(self, src, dest, timeout=120):
        """将 staging 中的目录移动到空闲目标；目标存在时绝不覆盖或合并。"""
        src = self._normalize_wsl_abs_path(src)
        dest = self._normalize_wsl_abs_path(dest)
        if src == dest:
            return True
        if not self._is_safe_migration_path(src):
            self.log_received.emit(f"[接管] 拒绝移动高风险源路径: {src or '<empty>'}", "error")
            return False
        managed_dest = dest.startswith("/root/") or dest.startswith("/var/lib/docker/volumes/")
        if not self._is_safe_migration_path(dest) or not managed_dest:
            self.log_received.emit(f"[接管] 拒绝移动到非托管目标路径: {dest or '<empty>'}", "error")
            return False
        if not self._wsl_path_exists(DISTRO_NAME, src, user="root"):
            self.log_received.emit(f"[接管] 待整理目录不存在: {src}", "error")
            return False
        if self._wsl_path_exists(DISTRO_NAME, dest, user="root"):
            self.log_received.emit(
                f"[接管] 目标路径在迁移期间已出现，拒绝覆盖或合并: {dest}；"
                "请备份并移走该路径后重试",
                "error",
            )
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
        move_cmd = (
            f"mv -T -n {shlex.quote(src)} {shlex.quote(dest)} && "
            f"test ! -e {shlex.quote(src)} && test -e {shlex.quote(dest)}"
        )
        proc = self._wsl_run(
            DISTRO_NAME,
            move_cmd,
            timeout=timeout,
            user="root",
        )
        if proc.returncode == 0:
            return True
        self.log_received.emit(
            self._format_command_failure(
                "[接管] 目录移动失败（目标可能在迁移期间出现，未执行覆盖或合并）",
                cmd=move_cmd,
                distro=DISTRO_NAME,
                user="root",
                timeout=timeout,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            ),
            "error",
        )
        return False

    def _rollback_migration_moves(
        self,
        completed_moves: list[tuple[str, str]],
    ) -> tuple[bool, bool]:
        """按逆序将已提交的目标目录移回 staging，全部成功后才允许恢复 Docker。"""
        self.log_received.emit("[接管] 迁移未完成，正在回滚已移动的目标数据...", "warn")
        docker_stopped_by_recheck = False
        for staged_path, dest_path in reversed(completed_moves):
            if dest_path.startswith("/var/lib/docker/volumes/"):
                stopped = self._ensure_target_docker_stopped_for_volume_change(
                    f"回滚 Docker volume 前（{dest_path}）"
                )
                if stopped is None:
                    return False, docker_stopped_by_recheck
                docker_stopped_by_recheck |= stopped
            if not self._wsl_path_exists(DISTRO_NAME, dest_path, user="root"):
                if self._wsl_path_exists(DISTRO_NAME, staged_path, user="root"):
                    continue
                self.log_received.emit(
                    "[接管] 回滚失败：目标与 staging 路径均不存在\n"
                    f"目标路径: {dest_path}\n"
                    f"staging 路径: {staged_path}\n"
                    "请检查上方迁移日志并手动确认数据位置。",
                    "error",
                )
                return False, docker_stopped_by_recheck
            if self._wsl_path_exists(DISTRO_NAME, staged_path, user="root"):
                self.log_received.emit(
                    "[接管] 回滚失败：staging 路径已被占用，拒绝覆盖或合并\n"
                    f"目标路径: {dest_path}\n"
                    f"staging 路径: {staged_path}",
                    "error",
                )
                return False, docker_stopped_by_recheck
            if not self._relocate_dir(dest_path, staged_path, timeout=300):
                self.log_received.emit(
                    "[接管] 回滚目录失败；Docker 将保持停止\n"
                    f"目标路径: {dest_path}\n"
                    f"staging 路径: {staged_path}\n"
                    "请根据紧邻的目录移动命令输出手动恢复。",
                    "error",
                )
                return False, docker_stopped_by_recheck
            self.log_received.emit(
                f"[接管] 已回滚目录: {dest_path} → {staged_path}",
                "warn",
            )
        return True, docker_stopped_by_recheck

    def _get_running_source_services(self, distro: str, deploy_dir: str) -> list[str] | None:
        """实时返回源 Compose 项目中正在运行的 service。

        None 表示无法可靠判断，调用方必须中止迁移，避免在线打包数据。
        """
        cmd = (
            f"cd {shlex.quote(deploy_dir)} && "
            "if [ ! -S /var/run/docker.sock ]; then exit 0; fi; "
            "if docker compose version >/dev/null 2>&1; then "
            "  docker compose ps --services --filter status=running; "
            "elif docker-compose version >/dev/null 2>&1; then "
            "  docker-compose ps --services --filter status=running; "
            "else "
            "  echo 'Docker Compose is unavailable' >&2; exit 127; "
            "fi"
        )
        try:
            proc = subprocess.run(
                ["wsl", "-d", distro, "-u", "root", "--", "bash", "-c", cmd],
                capture_output=True,
                timeout=30,
                creationflags=self._creation_flags(),
            )
            if proc.returncode != 0:
                self.log_received.emit(
                    self._format_command_failure(
                        "[接管] 检测源实例运行状态失败",
                        cmd=cmd,
                        distro=distro,
                        user="root",
                        timeout=30,
                        returncode=proc.returncode,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                    ),
                    "error",
                )
                return None
            output = self._clean_command_output(self._safe_decode(proc.stdout))
            return list(dict.fromkeys(line.strip() for line in output.splitlines() if line.strip()))
        except Exception as e:
            self.log_received.emit(
                self._format_command_failure(
                    "[接管] 检测源实例运行状态异常",
                    cmd=cmd,
                    distro=distro,
                    user="root",
                    timeout=30,
                    exception=e,
                ),
                "error",
            )
            return None

    def _stop_source_instance(self, distro: str, deploy_dir: str) -> bool:
        """停止源发行版中的 nekro-agent 容器，兼容新版插件式和旧版独立 docker-compose。"""
        cmd = (
            f"cd {shlex.quote(deploy_dir)} && "
            f'if docker compose version >/dev/null 2>&1; then '
            f'  docker compose stop; '
            f'else '
            f'  docker-compose stop; '
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
                    "error",
                )
                return False
            return True
        except Exception as e:
            self.log_received.emit(
                self._format_command_failure(
                    "[接管] 停止源实例异常",
                    cmd=cmd,
                    distro=distro,
                    user="root",
                    timeout=60,
                    exception=e,
                ),
                "error",
            )
            return False

    def _start_source_instance(
        self,
        distro: str,
        deploy_dir: str,
        services: list[str],
    ) -> bool:
        """恢复迁移前实际运行的源 Compose services，不启动原本停止的 service。"""
        if not services:
            return True
        service_args = " ".join(shlex.quote(service) for service in services)
        cmd = (
            f"cd {shlex.quote(deploy_dir)} && "
            f'if docker compose version >/dev/null 2>&1; then '
            f'  docker compose start {service_args}; '
            f'else '
            f'  docker-compose start {service_args}; '
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
                        "[接管] 恢复源实例运行状态失败",
                        cmd=cmd,
                        distro=distro,
                        user="root",
                        timeout=60,
                        returncode=proc.returncode,
                        stdout=proc.stdout,
                        stderr=proc.stderr,
                    ),
                    "error",
                )
                return False
            return True
        except Exception as e:
            self.log_received.emit(
                self._format_command_failure(
                    "[接管] 恢复源实例运行状态异常",
                    cmd=cmd,
                    distro=distro,
                    user="root",
                    timeout=60,
                    exception=e,
                ),
                "error",
            )
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

        _dest_deploy_dir, _dest_data_dir, optional_volumes = self._migration_destination_paths(instance)
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

    def _create_migration_staging_dir(self) -> str:
        try:
            state_proc = self._run_wsl_checked(
                DISTRO_NAME,
                "mktemp -d /root/.nekro-agent-migrate.XXXXXX",
                action="[接管] 创建迁移 staging 目录失败",
                timeout=30,
                user="root",
            )
            staging_dir = self._safe_decode(state_proc.stdout).strip()
            if not staging_dir.startswith("/root/.nekro-agent-migrate."):
                raise RuntimeError(f"mktemp 返回了非预期路径: {staging_dir or '<empty>'}")
            return staging_dir
        except Exception as e:
            self.log_received.emit(f"[接管] 无法创建安全的临时还原目录:\n{e}", "error")
            return ""

    @staticmethod
    def _staged_migration_path(staging_dir: str, original_path: str) -> str:
        return posixpath.join(staging_dir.rstrip("/"), original_path.lstrip("/"))

    def _cleanup_migration_staging_dir(self, staging_dir: str):
        normalized = self._normalize_wsl_abs_path(staging_dir)
        if not normalized.startswith("/root/.nekro-agent-migrate."):
            self.log_received.emit(f"[接管] 拒绝清理非 staging 路径: {normalized or '<empty>'}", "error")
            return
        try:
            self._run_wsl_checked(
                DISTRO_NAME,
                f"rm -rf -- {shlex.quote(normalized)}",
                action="[接管] 清理迁移 staging 目录失败",
                timeout=60,
                user="root",
            )
        except Exception as e:
            self.log_received.emit(f"[接管] 清理 staging 目录异常:\n{e}", "warn")

    def _prepare_target_docker_for_migration(self) -> bool | None:
        try:
            state_proc = self._run_wsl_checked(
                DISTRO_NAME,
                "systemctl is-active docker",
                action="[接管] 检测目标 Docker 运行状态失败",
                timeout=20,
                user="root",
                ok_returncodes=(0, 3),
            )
            docker_was_active = state_proc.returncode == 0
            if docker_was_active:
                self._run_wsl_checked(
                    DISTRO_NAME,
                    "systemctl stop docker",
                    action="[接管] 还原前停止目标 Docker 失败",
                    timeout=30,
                    user="root",
                )
            return docker_was_active
        except Exception as e:
            self.log_received.emit(f"[接管] 准备目标 Docker 环境异常:\n{e}", "error")
            return None

    def _ensure_target_docker_stopped_for_volume_change(self, phase: str) -> bool | None:
        """在 volume 目录变更前复核 daemon 状态；返回是否由本次复核执行了停止。"""
        try:
            state_proc = self._run_wsl_checked(
                DISTRO_NAME,
                "systemctl is-active docker",
                action=f"[接管] {phase}复核目标 Docker 状态失败",
                timeout=20,
                user="root",
                ok_returncodes=(0, 3),
            )
            if state_proc.returncode == 3:
                return False

            self.log_received.emit(
                f"[接管] {phase}检测到目标 Docker 被外部启动，正在安全停止...",
                "warn",
            )
            self._run_wsl_checked(
                DISTRO_NAME,
                "systemctl stop docker",
                action=f"[接管] {phase}停止目标 Docker 失败",
                timeout=30,
                user="root",
            )
            self._run_wsl_checked(
                DISTRO_NAME,
                "systemctl is-active docker",
                action=f"[接管] {phase}验证目标 Docker 已停止失败",
                timeout=20,
                user="root",
                ok_returncodes=(3,),
            )
            return True
        except Exception as e:
            self.log_received.emit(
                f"[接管] {phase}无法确认 Docker 已安全停止，已中止 volume 变更:\n{e}",
                "error",
            )
            return None

    def _restore_target_docker_after_migration(self) -> bool:
        try:
            self._run_wsl_checked(
                DISTRO_NAME,
                "systemctl start docker",
                action="[接管] 恢复目标 Docker 原运行状态失败",
                timeout=30,
                user="root",
            )
            time.sleep(2)
            return True
        except Exception as e:
            self.log_received.emit(f"[接管] 恢复目标 Docker 原运行状态异常:\n{e}", "error")
            return False

    def _restore_data(self, archive_path: str, staging_dir: str) -> bool:
        """校验归档成员后，仅解压到本次迁移的独立 staging 目录。"""
        validate_cmd = (
            f"tar -tzf {shlex.quote(archive_path)} | "
            "awk 'BEGIN { bad=0 } /^\\// || /(^|\\/)\\.\\.(\\/|$)/ { bad=1 } "
            "END { exit bad }'"
        )
        restore_cmd = (
            f"tar -xzf {shlex.quote(archive_path)} "
            f"-C {shlex.quote(staging_dir)} --keep-old-files"
        )
        try:
            self._run_wsl_checked(
                DISTRO_NAME,
                validate_cmd,
                action="[接管] 迁移归档安全校验失败",
                timeout=300,
                user="root",
            )
            self._run_wsl_checked(
                DISTRO_NAME,
                restore_cmd,
                action="[接管] 数据还原失败",
                timeout=300,
                user="root",
            )
            return True
        except Exception as e:
            self.log_received.emit(f"[接管] 数据还原异常:\n{e}", "error")
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
            self.log_received.emit("[接管] 启动器配置管理器不可用，无法完成接管", "error")
            return False

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
        saved = self.config.update_instance_with_globals(
            inst_id,
            instance_updates=inst_data,
            global_updates=global_updates,
        )

        if not saved:
            error = getattr(self.config, "last_save_error", "") or "未知错误"
            self.log_received.emit(
                "[接管] 保存启动器配置失败，接管未完成。"
                "请检查配置目录是否可写后重试。\n"
                f"错误: {error}",
                "error",
            )
            return False

        self.log_received.emit(
            f"[接管] config 已同步 — 实例: {inst_id}, 端口: {nekro_port}, 模式: {deploy_mode}, 频道: {release_channel}", "info"
        )
        return True
