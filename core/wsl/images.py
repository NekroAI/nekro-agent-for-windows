import json
import re
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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
    def _local_digest_cmd(normalized_ref):
        """构造读取本地镜像 RepoDigest 的命令；Go 模板花括号必须原样下发。"""
        return (
            "docker image inspect "
            f"{shlex.quote(normalized_ref)} "
            "--format '{{index .RepoDigests 0}}' 2>/dev/null"
        )

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

    _MANIFEST_ACCEPT = (
        "application/vnd.docker.distribution.manifest.list.v2+json, "
        "application/vnd.oci.image.index.v1+json, "
        "application/vnd.docker.distribution.manifest.v2+json, "
        "application/vnd.oci.image.manifest.v1+json"
    )

    @staticmethod
    def _parse_auth_challenge(header):
        """解析 Www-Authenticate: Bearer realm=...,service=...,scope=... 头。"""
        if not header:
            return None
        match = re.match(r"\s*Bearer\s+(.*)", header, re.IGNORECASE)
        if not match:
            return None
        params = {
            key.lower(): value
            for key, value in re.findall(r'(\w+)="([^"]*)"', match.group(1))
        }
        return params if "realm" in params else None

    def _fetch_bearer_token(self, header):
        """按 registry 返回的 challenge 去对应 realm 换取匿名 pull token。"""
        params = self._parse_auth_challenge(header)
        if not params:
            return None
        realm = params.get("realm")
        query = {k: params[k] for k in ("service", "scope") if params.get(k)}
        url = f"{realm}?{urlencode(query)}" if query else realm
        token_req = Request(url, headers={"User-Agent": "NekroAgentLauncher/1.0"})
        with urlopen(token_req, timeout=self._PROBE_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return data.get("token") or data.get("access_token")

    def _open_manifest(self, url, headers):
        """HEAD 探测 manifest；遇到不支持 HEAD 的源回退 GET。返回响应上下文管理器。"""
        try:
            req = Request(url, headers=headers, method="HEAD")
            return urlopen(req, timeout=self._PROBE_TIMEOUT)
        except HTTPError as e:
            if e.code in (405, 501):
                req = Request(url, headers=headers, method="GET")
                return urlopen(req, timeout=self._PROBE_TIMEOUT)
            raise

    def _open_manifest_with_auth(self, url, headers):
        try:
            return self._open_manifest(url, headers)
        except HTTPError as e:
            if e.code != 401:
                raise
            token = self._fetch_bearer_token(e.headers.get("Www-Authenticate", ""))
            if not token:
                raise
            auth_headers = {**headers, "Authorization": f"Bearer {token}"}
            return self._open_manifest(url, auth_headers)

    def _probe_registry_manifest(self, candidate):
        """探测 manifest 是否可达。

        返回 (ok, detail, reachable)。reachable 表示源本身活着（含 429 限流、
        鉴权受限等），仅是当前不可直接用 manifest 接口验证，拉取时仍可一试。
        """
        registry, repo, ref = self._registry_manifest_target(candidate.pull_ref)
        if not registry or not repo or not ref:
            return False, "无法解析镜像引用", False

        url = f"https://{registry}/v2/{repo}/manifests/{ref}"
        headers = {
            "Accept": self._MANIFEST_ACCEPT,
            "User-Agent": "NekroAgentLauncher/1.0",
        }

        try:
            with self._open_manifest(url, headers):
                return True, "", True
        except HTTPError as e:
            if e.code == 401:
                # 标准 Registry v2：匿名访问需先按 challenge 换 token 再重试。
                # 官方源与各代理源走的是同一套协议，此处统一处理。
                challenge = e.headers.get("Www-Authenticate", "")
                token = None
                try:
                    token = self._fetch_bearer_token(challenge)
                except Exception:
                    token = None
                if token:
                    auth_headers = {**headers, "Authorization": f"Bearer {token}"}
                    try:
                        with self._open_manifest(url, auth_headers):
                            return True, "", True
                    except HTTPError as e2:
                        if e2.code == 429:
                            return False, "HTTP 429 触发限流（源可达）", True
                        if e2.code in (401, 403):
                            return False, f"HTTP {e2.code} 鉴权受限（源可达）", True
                        return False, f"HTTP {e2.code} {e2.reason}", False
                return False, "HTTP 401 无法获取鉴权令牌（源可达）", True
            if e.code == 429:
                return False, "HTTP 429 触发限流（源可达）", True
            if e.code in (403,):
                return False, "HTTP 403 鉴权受限（源可达）", True
            return False, f"HTTP {e.code} {e.reason}", False

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
        reachable = False
        try:
            ok, detail, reachable = self._probe_registry_manifest(candidate)
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
            return True, latency_ms, "", True

        docker_ok, docker_latency_ms, docker_detail = self._probe_pull_candidate_with_docker(
            distro,
            candidate,
        )
        if docker_ok:
            return True, docker_latency_ms, "", True
        if docker_detail:
            detail = f"Registry API: {detail}\nDocker CLI: {docker_detail}"
        return False, latency_ms, detail, reachable

    def _probe_pull_candidates(self, distro, candidates):
        ranked = []
        reachable = []
        dead = []
        candidate_index = {candidate: index for index, candidate in enumerate(candidates)}
        with ThreadPoolExecutor(max_workers=len(candidates)) as executor:
            futures = {
                executor.submit(self._probe_pull_candidate, distro, candidate): candidate
                for candidate in candidates
            }
            raw = []
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    ok, latency_ms, detail, is_reachable = future.result()
                except Exception as e:
                    ok, latency_ms, detail, is_reachable = False, None, str(e), False
                raw.append((candidate, ok, latency_ms, detail, is_reachable))

        raw.sort(key=lambda item: candidate_index[item[0]])
        results = []
        for candidate, ok, latency_ms, detail, is_reachable in raw:
            results.append((candidate, ok, latency_ms, detail))
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
            elif is_reachable:
                # 源可达但暂时受限（429/鉴权），拉取时仍可一试，排在死源之前。
                reachable.append(candidate)
                if detail:
                    self.log_received.emit(detail, "debug")
            else:
                dead.append(candidate)
                if detail:
                    self.log_received.emit(detail, "debug")

        ranked.sort(key=lambda item: item.latency_ms if item.latency_ms is not None else 10**9)
        failed = reachable + dead
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
        distro = DISTRO_NAME

        def _do_check():
            results = []
            for image_ref, name, desc, modes in self.get_managed_images(self.config):
                if only_image and image_ref != only_image:
                    continue
                normalized_ref = self._normalize_image_ref(image_ref)
                registry, repo, ref = self._registry_manifest_target(image_ref)
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
                    if not registry or not repo or not ref:
                        raise ValueError(f"无法解析镜像引用: {image_ref}")

                    local_out = self._wsl_exec(
                        distro,
                        self._local_digest_cmd(normalized_ref),
                        timeout=15,
                    ).strip().strip("'")
                    if local_out and "@" in local_out:
                        full_local = local_out.split("@")[-1]
                        entry["local"] = full_local[:19]
                    else:
                        full_local = ""
                        entry["local"] = None

                    manifest_url = f"https://{registry}/v2/{repo}/manifests/{ref}"
                    manifest_headers = {
                        "Accept": self._MANIFEST_ACCEPT,
                        "User-Agent": "NekroAgent/1.0",
                    }
                    with self._open_manifest_with_auth(
                        manifest_url,
                        manifest_headers,
                    ) as resp:
                        resp.read(256)
                        full_remote = resp.headers.get("Docker-Content-Digest", "")
                        entry["remote"] = full_remote[:19] if full_remote else ""

                    if full_remote:
                        entry["has_update"] = full_remote != full_local
                except HTTPError as e:
                    entry["error"] = (
                        "镜像远程状态检查失败\n"
                        f"镜像: {image_ref}\n"
                        f"Registry: {registry}\n"
                        f"仓库: {repo}\n"
                        f"引用: {ref}\n"
                        f"HTTP: {e.code} {e.reason}"
                    )
                except URLError as e:
                    entry["error"] = (
                        "镜像远程状态检查失败\n"
                        f"镜像: {image_ref}\n"
                        f"Registry: {registry}\n"
                        f"仓库: {repo}\n"
                        f"引用: {ref}\n"
                        f"网络错误: {e.reason}"
                    )
                except Exception as e:
                    entry["error"] = (
                        "镜像状态检查失败\n"
                        f"镜像: {image_ref}\n"
                        f"Registry: {registry}\n"
                        f"仓库: {repo}\n"
                        f"引用: {ref}\n"
                        f"异常: {type(e).__name__}: {e}"
                    )
                results.append(entry)
            self.image_status_result.emit(results)

        threading.Thread(target=_do_check, daemon=True).start()

    def pull_single_image(self, image_ref):
        """拉取单个镜像，结果通过 image_pull_result 信号发出"""
        distro = DISTRO_NAME

        def _do_pull():
            self._emit_pull_progress("start", f"拉取镜像: {image_ref}")
            ok = self._pull_images(distro, [image_ref])
            if ok:
                self.image_pull_result.emit(image_ref, True, "拉取成功")
            else:
                self.image_pull_result.emit(
                    image_ref,
                    False,
                    getattr(self, "_last_pull_error", "") or "拉取失败",
                )

        threading.Thread(target=_do_pull, daemon=True).start()
