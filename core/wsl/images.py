import subprocess
import threading
from urllib.request import Request, urlopen

from core.wsl.constants import DISTRO_NAME, MANAGED_IMAGES_BASE, PREVIEW_IMAGE, REQUIRED_IMAGES_BASE, STABLE_IMAGE


class WSLImageMixin:
    @staticmethod
    def get_agent_image_ref(config=None):
        channel = "stable"
        if config is not None:
            try:
                channel = config.get("release_channel") or "stable"
            except Exception:
                channel = "stable"
        return PREVIEW_IMAGE if channel == "preview" else STABLE_IMAGE

    @classmethod
    def get_required_images(cls, deploy_mode, config=None):
        agent_ref = cls.get_agent_image_ref(config)
        base_images = REQUIRED_IMAGES_BASE.get(deploy_mode, REQUIRED_IMAGES_BASE["lite"])
        return [agent_ref if image == STABLE_IMAGE else image for image in base_images]

    @classmethod
    def get_managed_images(cls, config=None):
        agent_ref = cls.get_agent_image_ref(config)
        managed = []
        for image_ref, name, desc, modes in MANAGED_IMAGES_BASE:
            current_ref = agent_ref if image_ref == STABLE_IMAGE else image_ref
            managed.append((current_ref, name, desc, modes))
        return managed

    def _emit_pull_progress(self, phase, message):
        self.progress_updated.emit(f"__pull_progress__|{phase}|{message}")

    def _get_local_images(self, distro):
        """获取 WSL 内已存在的 docker 镜像列表，返回 set of 'repo:tag'"""
        try:
            output = self._wsl_exec(distro, "docker images --format '{{.Repository}}:{{.Tag}}'", timeout=15)
            images = set()
            for line in output.strip().splitlines():
                line = line.strip().strip("'")
                if line and line != "<none>:<none>":
                    images.add(line)
            return images
        except Exception:
            return set()

    def _get_missing_images(self, distro, deploy_mode):
        """对比镜像清单，返回本地缺失的镜像列表"""
        required = self.get_required_images(deploy_mode, self.config)
        local = self._get_local_images(distro)

        missing = []
        for image in required:
            check_name = image if ":" in image else f"{image}:latest"
            if check_name not in local:
                missing.append(image)
        return missing

    def _docker_hub_ref(self, image):
        if "@" in image:
            return image

        name = image
        tag = ""
        last_segment = image.rsplit("/", 1)[-1]
        if ":" in last_segment:
            name, tag = image.rsplit(":", 1)

        first_segment = name.split("/", 1)[0]
        if "." in first_segment or ":" in first_segment or first_segment == "localhost":
            return image

        if "/" not in name:
            name = f"library/{name}"

        qualified = f"docker.io/{name}"
        return f"{qualified}:{tag}" if tag else qualified

    def _pull_image_once(self, distro, image_ref):
        proc = subprocess.Popen(
            ["wsl", "-d", distro, "--", "docker", "pull", image_ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=self._creation_flags(),
        )
        last_lines = []
        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if line:
                text = self._safe_decode(line).strip()
                if text and not self._is_wsl_noise(text):
                    self._emit_pull_progress("update", text)
                    last_lines.append(text)
                    if len(last_lines) > 10:
                        last_lines.pop(0)

        proc.wait()
        return proc.returncode == 0, last_lines

    def _pull_images(self, distro, images):
        """逐个拉取镜像列表，带进度反馈。返回 True 全部成功，False 有失败"""
        total = len(images)
        for idx, image in enumerate(images, 1):
            self._emit_pull_progress("stage", f"拉取镜像 ({idx}/{total}): {image}")
            self.log_received.emit(f"拉取镜像 ({idx}/{total}): {image}", "info")

            ok, last_lines = self._pull_image_once(distro, image)
            if not ok:
                hub_image = self._docker_hub_ref(image)
                if hub_image != image:
                    retry_msg = f"镜像站拉取失败，尝试使用官方 Docker Hub 重试: {hub_image}"
                    self.log_received.emit(retry_msg, "warning")
                    self._emit_pull_progress("stage", f"官方 Hub 重试 ({idx}/{total}): {image}")
                    ok, retry_lines = self._pull_image_once(distro, hub_image)
                    if ok:
                        self.log_received.emit(f"✓ {image} 已通过官方 Docker Hub 拉取完成", "info")
                        continue
                    last_lines = retry_lines or last_lines

                detail = "\n".join(last_lines[-5:]) if last_lines else ""
                err_msg = f"镜像拉取失败: {image}"
                if hub_image != image:
                    err_msg += f"\n已使用官方 Docker Hub 重试: {hub_image}"
                if detail:
                    err_msg += f"\n{detail}"
                self._emit_pull_progress("error", err_msg)
                self.log_received.emit(err_msg, "error")
                return False
            self.log_received.emit(f"✓ {image} 拉取完成", "info")

        self._emit_pull_progress("done", "所有镜像拉取完成")
        self.log_received.emit("✓ 所有镜像拉取完成", "info")
        return True

    def check_images_status(self, only_image=None):
        """检测镜像本地/远程状态，结果通过 image_status_result 信号发出"""
        import json

        distro = DISTRO_NAME

        def _do_check():
            results = []
            for image_ref, name, desc, modes in self.get_managed_images(self.config):
                if only_image and image_ref != only_image:
                    continue
                image = image_ref.split(":")[0]
                tag = image_ref.split(":")[1] if ":" in image_ref else "latest"
                entry = {
                    "image": image_ref,
                    "name": name,
                    "modes": modes,
                    "local": None,
                    "remote": None,
                    "has_update": False,
                    "error": None,
                }
                try:
                    local_out = self._wsl_exec(
                        distro,
                        f"docker image inspect {image}:{tag} --format '{{{{index .RepoDigests 0}}}}' 2>/dev/null",
                        timeout=15,
                    ).strip().strip("'")
                    if local_out and "@" in local_out:
                        full_local = local_out.split("@")[-1]
                        entry["local"] = full_local[:19]
                    else:
                        full_local = ""
                        entry["local"] = None

                    token_req = Request(
                        f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{image}:pull",
                        headers={"User-Agent": "NekroAgent/1.0"},
                    )
                    with urlopen(token_req, timeout=10) as resp:
                        token = json.loads(resp.read())["token"]
                    manifest_req = Request(
                        f"https://registry-1.docker.io/v2/{image}/manifests/{tag}",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/vnd.docker.distribution.manifest.v2+json",
                            "User-Agent": "NekroAgent/1.0",
                        },
                    )
                    with urlopen(manifest_req, timeout=10) as resp:
                        full_remote = resp.headers.get("Docker-Content-Digest", "")
                        entry["remote"] = full_remote[:19] if full_remote else ""

                    if full_remote:
                        entry["has_update"] = full_remote != full_local
                except Exception as e:
                    entry["error"] = str(e)
                results.append(entry)
            self.image_status_result.emit(results)

        threading.Thread(target=_do_check, daemon=True).start()

    def pull_single_image(self, image_ref):
        """拉取单个镜像，结果通过 image_pull_result 信号发出"""
        distro = DISTRO_NAME

        def _do_pull():
            image = image_ref.split(":")[0]
            tag = image_ref.split(":")[1] if ":" in image_ref else "latest"
            self._emit_pull_progress("start", f"拉取镜像: {image_ref}")
            ok = self._pull_images(distro, [f"{image}:{tag}"])
            if ok:
                self.image_pull_result.emit(image_ref, True, "拉取成功")
            else:
                self.image_pull_result.emit(image_ref, False, "拉取失败")

        threading.Thread(target=_do_pull, daemon=True).start()
