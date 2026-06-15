import json
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.wsl.constants import DISTRO_NAME, MANAGED_IMAGES_BASE, PREVIEW_IMAGE, REQUIRED_IMAGES_BASE, STABLE_IMAGE


@dataclass(frozen=True)
class _PullCandidate:
    pull_ref: str
    final_ref: str
    source: str
    is_proxy: bool
    latency_ms: int | None = None


class WSLImageMixin:
    _DOCKER_PROXY_REGISTRIES = (
        "docker.m.daocloud.io",
        "docker.1ms.run",
        "docker.xuanyuan.me",
        "docker.jiaxin.site",
    )
    _PROBE_TIMEOUT = 8

    @staticmethod
    def get_agent_image_ref(config=None, release_channel=None):
        channel = "stable"
        if release_channel is not None:
            channel = release_channel or "stable"
        elif config is not None:
            try:
                channel = config.get("release_channel") or "stable"
            except Exception:
                channel = "stable"
        return PREVIEW_IMAGE if channel == "preview" else STABLE_IMAGE

    @classmethod
    def get_required_images(cls, deploy_mode, config=None, release_channel=None):
        agent_ref = cls.get_agent_image_ref(
            config=config,
            release_channel=release_channel,
        )
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

    def _get_missing_images(self, distro, deploy_mode, release_channel=None):
        """对比镜像清单，返回本地缺失的镜像列表"""
        required = self.get_required_images(
            deploy_mode,
            self.config,
            release_channel=release_channel,
        )
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

    _PULL_TIMEOUT = 1800

    @staticmethod
    def _normalize_image_ref(image_ref):
        if "@" in image_ref:
            return image_ref
        last_segment = image_ref.rsplit("/", 1)[-1]
        if ":" in last_segment:
            return image_ref
        return f"{image_ref}:latest"

    @staticmethod
    def _docker_hub_repo_tag(image_ref):
        """返回可拼接到 Docker Hub 代理 registry 后的 repo:tag。"""
        if "@" in image_ref:
            return ""

        normalized = WSLImageMixin._normalize_image_ref(image_ref)
        name, tag = normalized.rsplit(":", 1)
        first_segment = name.split("/", 1)[0]

        if first_segment in {"docker.io", "registry-1.docker.io", "index.docker.io"}:
            name = name.split("/", 1)[1] if "/" in name else ""
        elif "." in first_segment or ":" in first_segment or first_segment == "localhost":
            return ""

        if not name:
            return ""
        if "/" not in name:
            name = f"library/{name}"
        return f"{name}:{tag}"

    @classmethod
    def _proxy_image_ref(cls, registry, image_ref):
        repo_tag = cls._docker_hub_repo_tag(image_ref)
        if not repo_tag:
            return ""
        return f"{registry.rstrip('/')}/{repo_tag}"

    @classmethod
    def _build_pull_candidates(cls, image_ref):
        final_ref = cls._normalize_image_ref(image_ref)
        candidates = []
        for registry in cls._DOCKER_PROXY_REGISTRIES:
            proxy_ref = cls._proxy_image_ref(registry, image_ref)
            if proxy_ref:
                candidates.append(_PullCandidate(proxy_ref, final_ref, registry, True))
        candidates.append(_PullCandidate(final_ref, final_ref, "Docker Hub", False))
        return candidates

    @staticmethod
    def _registry_manifest_target(image_ref):
        if "@" in image_ref:
            name, digest = image_ref.split("@", 1)
            ref = digest
        else:
            normalized = WSLImageMixin._normalize_image_ref(image_ref)
            name, ref = normalized.rsplit(":", 1)

        first_segment = name.split("/", 1)[0]
        if first_segment in {"docker.io", "registry-1.docker.io", "index.docker.io"}:
            registry = "registry-1.docker.io"
            repo = name.split("/", 1)[1] if "/" in name else ""
        elif "." in first_segment or ":" in first_segment or first_segment == "localhost":
            registry = first_segment
            repo = name.split("/", 1)[1] if "/" in name else ""
        else:
            registry = "registry-1.docker.io"
            repo = name
            if "/" not in repo:
                repo = f"library/{repo}"

        if not repo:
            return "", "", ""
        return registry, repo, ref

    def _probe_registry_manifest(self, candidate):
        registry, repo, ref = self._registry_manifest_target(candidate.pull_ref)
        if not registry or not repo or not ref:
            return False, "无法解析镜像引用"

        headers = {
            "Accept": (
                "application/vnd.docker.distribution.manifest.list.v2+json, "
                "application/vnd.oci.image.index.v1+json, "
                "application/vnd.docker.distribution.manifest.v2+json, "
                "application/vnd.oci.image.manifest.v1+json"
            ),
            "User-Agent": "NekroAgentLauncher/1.0",
        }
        if registry == "registry-1.docker.io":
            token_req = Request(
                "https://auth.docker.io/token?"
                f"service=registry.docker.io&scope=repository:{repo}:pull",
                headers={"User-Agent": "NekroAgentLauncher/1.0"},
            )
            with urlopen(token_req, timeout=self._PROBE_TIMEOUT) as resp:
                token = json.loads(resp.read())["token"]
            headers["Authorization"] = f"Bearer {token}"

        manifest_req = Request(
            f"https://{registry}/v2/{repo}/manifests/{ref}",
            headers=headers,
        )
        with urlopen(manifest_req, timeout=self._PROBE_TIMEOUT) as resp:
            resp.read(256)
        return True, ""

    def _probe_pull_candidate_with_docker(self, distro, candidate):
        start = time.monotonic()
        args = [
            "wsl",
            "-d",
            distro,
            "--",
            "docker",
            "manifest",
            "inspect",
            candidate.pull_ref,
        ]
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                timeout=self._PROBE_TIMEOUT,
                creationflags=self._creation_flags(),
            )
        except subprocess.TimeoutExpired as e:
            return False, None, self._format_command_failure(
                "镜像源测速超时",
                args=args,
                distro=distro,
                timeout=self._PROBE_TIMEOUT,
                stdout=e.stdout,
                stderr=e.stderr,
                exception=e,
                max_output=800,
            )
        except Exception as e:
            return False, None, self._format_command_failure(
                "镜像源测速异常",
                args=args,
                distro=distro,
                exception=e,
                max_output=800,
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        if proc.returncode == 0:
            return True, latency_ms, ""
        return False, latency_ms, self._format_command_failure(
            "镜像源测速失败",
            args=args,
            distro=distro,
            timeout=self._PROBE_TIMEOUT,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            max_output=800,
        )

    def _probe_pull_candidate(self, distro, candidate):
        start = time.monotonic()
        try:
            ok, detail = self._probe_registry_manifest(candidate)
        except HTTPError as e:
            ok = False
            detail = f"HTTP {e.code} {e.reason}"
        except URLError as e:
            ok = False
            detail = f"网络错误: {e.reason}"
        except TimeoutError:
            ok = False
            detail = f"连接超时 ({self._PROBE_TIMEOUT}s)"
        except Exception as e:
            ok = False
            detail = f"{type(e).__name__}: {e}"
        latency_ms = int((time.monotonic() - start) * 1000)
        if ok:
            return True, latency_ms, ""

        docker_ok, docker_latency_ms, docker_detail = self._probe_pull_candidate_with_docker(
            distro,
            candidate,
        )
        if docker_ok:
            return True, docker_latency_ms, ""
        if docker_detail:
            detail = f"Registry API: {detail}\nDocker CLI: {docker_detail}"
        return False, latency_ms, detail

    def _probe_pull_candidates(self, distro, candidates):
        ranked = []
        failed = []
        candidate_index = {candidate: index for index, candidate in enumerate(candidates)}
        with ThreadPoolExecutor(max_workers=len(candidates)) as executor:
            futures = {
                executor.submit(self._probe_pull_candidate, distro, candidate): candidate
                for candidate in candidates
            }
            results = []
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    ok, latency_ms, detail = future.result()
                except Exception as e:
                    ok, latency_ms, detail = False, None, str(e)
                results.append((candidate, ok, latency_ms, detail))

        results.sort(key=lambda item: candidate_index[item[0]])
        for candidate, ok, latency_ms, detail in results:
            if ok and latency_ms is not None:
                ranked.append(
                    _PullCandidate(
                        candidate.pull_ref,
                        candidate.final_ref,
                        candidate.source,
                        candidate.is_proxy,
                        latency_ms,
                    )
                )
                self.log_received.emit(
                    f"镜像源测速 {candidate.source}: {latency_ms}ms",
                    "debug",
                )
            else:
                failed.append(candidate)
                if detail:
                    self.log_received.emit(detail, "debug")

        ranked.sort(key=lambda item: item.latency_ms if item.latency_ms is not None else 10**9)
        return ranked, failed, results

    def _pull_candidate_cache_key(self, image_ref):
        return self._normalize_image_ref(image_ref)

    def _get_pull_candidate_cache(self):
        cache = getattr(self, "_pull_candidate_cache", None)
        if cache is None:
            cache = {}
            self._pull_candidate_cache = cache
        return cache

    def _cache_pull_candidates(self, image_ref, candidates):
        self._get_pull_candidate_cache()[self._pull_candidate_cache_key(image_ref)] = candidates

    def _rank_pull_candidates(self, distro, image_ref):
        cache = self._get_pull_candidate_cache()
        cached = cache.get(self._pull_candidate_cache_key(image_ref))
        if cached:
            return cached

        candidates = self._build_pull_candidates(image_ref)
        if len(candidates) == 1:
            self._cache_pull_candidates(image_ref, candidates)
            return candidates

        ranked, failed, _results = self._probe_pull_candidates(distro, candidates)
        if ranked:
            best = ranked[0]
            self.log_received.emit(
                f"镜像源测速完成，优先使用 {best.source} ({best.latency_ms}ms)",
                "info",
            )
            selected = ranked + failed
            self._cache_pull_candidates(image_ref, selected)
            return selected

        self.log_received.emit(
            "镜像源测速未找到可用源，将按默认顺序尝试拉取。"
            "请在镜像源测速页面查看失败原因。",
            "warn",
        )
        self._cache_pull_candidates(image_ref, candidates)
        return candidates

    def speedtest_pull_sources(self, distro, images):
        image_results = []
        source_stats = {}

        for image_ref in images:
            candidates = self._build_pull_candidates(image_ref)
            if len(candidates) == 1:
                self._cache_pull_candidates(image_ref, candidates)
                image_results.append(
                    {
                        "image": image_ref,
                        "best_source": candidates[0].source,
                        "candidates": [
                            {
                                "source": candidates[0].source,
                                "pull_ref": candidates[0].pull_ref,
                                "ok": True,
                                "latency_ms": None,
                                "detail": "非 Docker Hub 镜像，直接使用原始源。",
                            }
                        ],
                    }
                )
                continue

            ranked, failed, results = self._probe_pull_candidates(distro, candidates)
            selected = ranked + failed if ranked else candidates
            self._cache_pull_candidates(image_ref, selected)
            best_source = ranked[0].source if ranked else ""

            candidate_results = []
            for candidate, ok, latency_ms, detail in results:
                candidate_results.append(
                    {
                        "source": candidate.source,
                        "pull_ref": candidate.pull_ref,
                        "ok": ok,
                        "latency_ms": latency_ms,
                        "detail": detail,
                    }
                )
                stat = source_stats.setdefault(
                    candidate.source,
                    {
                        "source": candidate.source,
                        "ok_count": 0,
                        "total": 0,
                        "latencies": [],
                        "last_detail": "",
                    },
                )
                stat["total"] += 1
                if ok and latency_ms is not None:
                    stat["ok_count"] += 1
                    stat["latencies"].append(latency_ms)
                elif detail:
                    stat["last_detail"] = detail

            image_results.append(
                {
                    "image": image_ref,
                    "best_source": best_source,
                    "candidates": candidate_results,
                }
            )

        sources = []
        for stat in source_stats.values():
            latencies = stat.pop("latencies")
            if latencies:
                stat["avg_latency_ms"] = int(sum(latencies) / len(latencies))
                stat["best_latency_ms"] = min(latencies)
            else:
                stat["avg_latency_ms"] = None
                stat["best_latency_ms"] = None
            sources.append(stat)
        sources.sort(
            key=lambda item: (
                0 if item["ok_count"] else 1,
                item["avg_latency_ms"] if item["avg_latency_ms"] is not None else 10**9,
                item["source"],
            )
        )
        return {"images": image_results, "sources": sources}

    def _retag_pulled_image(self, distro, source_ref, target_ref):
        if source_ref == target_ref:
            return True, ""
        try:
            self._run_wsl_checked(
                distro,
                f"docker tag {shlex.quote(source_ref)} {shlex.quote(target_ref)}",
                action="镜像重打标签失败",
                timeout=60,
            )
            self._run_wsl_checked(
                distro,
                f"docker image rm {shlex.quote(source_ref)} >/dev/null 2>&1 || true",
                action="清理加速源镜像标签失败",
                timeout=60,
            )
            return True, ""
        except Exception as e:
            return False, str(e)

    def _pull_image_once(self, distro, image_ref):
        args = ["wsl", "-d", distro, "--", "docker", "pull", image_ref]
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=self._creation_flags(),
            )
        except Exception as e:
            return False, [
                self._format_command_failure(
                    "启动 docker pull 失败",
                    args=args,
                    distro=distro,
                    exception=e,
                )
            ]

        last_lines = []
        deadline = time.monotonic() + self._PULL_TIMEOUT
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                last_lines.append(
                    self._format_command_failure(
                        "镜像拉取超时",
                        args=args,
                        distro=distro,
                        timeout=self._PULL_TIMEOUT,
                        stdout="\n".join(last_lines),
                    )
                )
                return False, last_lines
            line = proc.stdout.readline() if proc.stdout else b""
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
        if proc.returncode != 0:
            last_lines.append(
                self._format_command_failure(
                    "镜像拉取失败",
                    args=args,
                    distro=distro,
                    timeout=self._PULL_TIMEOUT,
                    returncode=proc.returncode,
                    stdout="\n".join(last_lines),
                )
            )
            return False, last_lines
        return True, last_lines

    def _pull_images(self, distro, images):
        """逐个拉取镜像列表，带进度反馈。返回 True 全部成功，False 有失败"""
        self._last_pull_error = ""
        total = len(images)
        for idx, image in enumerate(images, 1):
            cached = self._get_pull_candidate_cache().get(
                self._pull_candidate_cache_key(image)
            )
            if not cached:
                self._emit_pull_progress(
                    "speedtest",
                    f"{idx}/{total}|测速镜像源 ({idx}/{total}): {image}",
                )
            candidates = self._rank_pull_candidates(distro, image)
            ok = False
            last_lines = []
            tried_sources = []

            for candidate in candidates:
                tried_sources.append(candidate.source)
                source_label = (
                    f"{candidate.source} ({candidate.latency_ms}ms)"
                    if candidate.latency_ms is not None
                    else candidate.source
                )
                self._emit_pull_progress(
                    "stage",
                    f"{idx}/{total}|拉取镜像 ({idx}/{total}) [{source_label}]: {image}",
                )
                self.log_received.emit(
                    f"拉取镜像 ({idx}/{total}) [{source_label}]: {image}",
                    "info",
                )

                ok, last_lines = self._pull_image_once(distro, candidate.pull_ref)
                if not ok:
                    self.log_received.emit(
                        f"镜像源 {candidate.source} 拉取失败，尝试下一个源...",
                        "warning",
                    )
                    continue

                tag_ok, tag_message = self._retag_pulled_image(
                    distro,
                    candidate.pull_ref,
                    candidate.final_ref,
                )
                if not tag_ok:
                    ok = False
                    last_lines = [tag_message]
                    self.log_received.emit(tag_message, "error")
                    continue

                if candidate.pull_ref != candidate.final_ref:
                    self.log_received.emit(
                        f"✓ {image} 已通过 {candidate.source} 拉取，并标记为 {candidate.final_ref}",
                        "info",
                    )
                else:
                    self.log_received.emit(f"✓ {image} 拉取完成", "info")
                break

            if not ok:
                detail = "\n".join(last_lines[-5:]) if last_lines else ""
                err_msg = f"镜像拉取失败: {image}"
                if tried_sources:
                    err_msg += "\n已尝试镜像源: " + "、".join(tried_sources)
                if detail:
                    err_msg += f"\n{detail}"
                self._last_pull_error = err_msg
                self._emit_pull_progress("error", err_msg)
                self.log_received.emit(err_msg, "error")
                return False

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
                except HTTPError as e:
                    entry["error"] = (
                        "镜像远程状态检查失败\n"
                        f"镜像: {image_ref}\n"
                        f"Docker Hub 仓库: {image}\n"
                        f"标签: {tag}\n"
                        f"HTTP: {e.code} {e.reason}"
                    )
                except URLError as e:
                    entry["error"] = (
                        "镜像远程状态检查失败\n"
                        f"镜像: {image_ref}\n"
                        f"Docker Hub 仓库: {image}\n"
                        f"标签: {tag}\n"
                        f"网络错误: {e.reason}"
                    )
                except Exception as e:
                    entry["error"] = (
                        "镜像状态检查失败\n"
                        f"镜像: {image_ref}\n"
                        f"Docker Hub 仓库: {image}\n"
                        f"标签: {tag}\n"
                        f"异常: {type(e).__name__}: {e}"
                    )
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
                self.image_pull_result.emit(
                    image_ref,
                    False,
                    getattr(self, "_last_pull_error", "") or "拉取失败",
                )

        threading.Thread(target=_do_pull, daemon=True).start()
