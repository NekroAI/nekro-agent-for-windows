#!/usr/bin/env bash
set -u

MIRRORS=(
  "docker.m.daocloud.io"
  "docker.1ms.run"
  "docker.xuanyuan.me"
  "docker.jiaxin.site"
)

IMAGES=(
  "postgres:14"
  "qdrant/qdrant:v1.17.1"
  "kromiose/nekro-agent:latest"
  "kromiose/nekro-agent-sandbox:latest"
  "mlikiowa/napcat-docker:latest"
)

ACCEPT_HEADER="Accept: application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json"
TIMEOUT="${TIMEOUT:-10}"
BODY_FILE="${TMPDIR:-/tmp}/na_mirror_probe_body.$$"

usage() {
  cat <<'USAGE'
Usage:
  bash scripts/probe_docker_mirrors.sh [--fix-localhost-proxy] [image ...]

Options:
  --fix-localhost-proxy
      If http_proxy/https_proxy points to 127.0.0.1 or localhost, rewrite it
      to the WSL default gateway IP for this shell only.

Env:
  TIMEOUT=10
      curl/docker manifest timeout seconds.

Examples:
  bash scripts/probe_docker_mirrors.sh
  TIMEOUT=20 bash scripts/probe_docker_mirrors.sh --fix-localhost-proxy
  bash scripts/probe_docker_mirrors.sh postgres:14 kromiose/nekro-agent:latest
USAGE
}

cleanup() {
  rm -f "$BODY_FILE"
}
trap cleanup EXIT

fix_localhost_proxy() {
  local gateway
  gateway="$(ip route | awk '/default/ {print $3; exit}')"
  if [[ -z "$gateway" ]]; then
    echo "[proxy] cannot find WSL gateway; keep current proxy env"
    return
  fi

  for name in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do
    local value="${!name:-}"
    if [[ "$value" =~ ^http://(127\.0\.0\.1|localhost):([0-9]+) ]]; then
      local port="${BASH_REMATCH[2]}"
      export "$name=http://$gateway:$port"
      echo "[proxy] rewrite $name -> http://$gateway:$port"
    fi
  done
}

warn_localhost_proxy() {
  for name in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do
    local value="${!name:-}"
    if [[ "$value" =~ ^http://(127\.0\.0\.1|localhost): ]]; then
      echo "[proxy] warning: $name=$value may not work inside WSL NAT."
      echo "[proxy] rerun with --fix-localhost-proxy if your Windows proxy listens on LAN/WSL gateway."
      return
    fi
  done
}

split_image() {
  local image="$1"
  local repo tag

  if [[ "$image" == *@* ]]; then
    repo="${image%@*}"
    tag="${image#*@}"
  elif [[ "${image##*/}" == *:* ]]; then
    repo="${image%:*}"
    tag="${image##*:}"
  else
    repo="$image"
    tag="latest"
  fi

  if [[ "$repo" != */* ]]; then
    repo="library/$repo"
  fi

  printf '%s\t%s\n' "$repo" "$tag"
}

short_body() {
  if [[ ! -s "$BODY_FILE" ]]; then
    echo "(empty body)"
    return
  fi
  tr '\n' ' ' < "$BODY_FILE" | sed -E 's/[[:space:]]+/ /g' | cut -c 1-260
}

curl_probe() {
  local reg="$1"
  local repo="$2"
  local tag="$3"
  local url="https://$reg/v2/$repo/manifests/$tag"
  local metrics status elapsed

  metrics="$(
    curl -sS -L \
      --connect-timeout "$TIMEOUT" \
      --max-time "$TIMEOUT" \
      -o "$BODY_FILE" \
      -w '%{http_code} %{time_total}' \
      -H "$ACCEPT_HEADER" \
      "$url" 2>&1
  )"
  status="${metrics%% *}"
  elapsed="${metrics##* }"

  if [[ "$status" =~ ^[0-9]{3}$ ]]; then
    printf '  [HTTP] %-24s status=%s time=%ss\n' "$reg" "$status" "$elapsed"
    printf '         %s\n' "$(short_body)"
  else
    printf '  [HTTP] %-24s curl-error=%s\n' "$reg" "$metrics"
  fi
}

docker_probe() {
  local ref="$1"
  local start elapsed rc output

  if ! command -v docker >/dev/null 2>&1; then
    printf '  [Docker] %-60s SKIP docker not found\n' "$ref"
    return
  fi

  start="$(date +%s%3N)"
  output="$(timeout "$TIMEOUT" docker manifest inspect "$ref" 2>&1 >/dev/null)"
  rc=$?
  elapsed="$(( $(date +%s%3N) - start ))"

  if [[ "$rc" -eq 0 ]]; then
    printf '  [Docker] %-60s OK   time=%sms\n' "$ref" "$elapsed"
  else
    output="$(printf '%s' "$output" | tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g' | cut -c 1-220)"
    printf '  [Docker] %-60s FAIL time=%sms rc=%s\n' "$ref" "$elapsed" "$rc"
    printf '           %s\n' "${output:-"(no output)"}"
  fi
}

ARGS=()
FIX_PROXY=0
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
    --fix-localhost-proxy)
      FIX_PROXY=1
      ;;
    *)
      ARGS+=("$arg")
      ;;
  esac
done

if [[ "$FIX_PROXY" -eq 1 ]]; then
  fix_localhost_proxy
else
  warn_localhost_proxy
fi

if [[ "${#ARGS[@]}" -gt 0 ]]; then
  IMAGES=("${ARGS[@]}")
fi

echo "== Docker mirror probe =="
echo "timeout: ${TIMEOUT}s"
echo

for image in "${IMAGES[@]}"; do
  IFS=$'\t' read -r repo tag < <(split_image "$image")
  echo "=== $image -> $repo:$tag ==="

  for reg in "${MIRRORS[@]}"; do
    curl_probe "$reg" "$repo" "$tag"
  done

  echo "  -- docker manifest inspect --"
  for reg in "${MIRRORS[@]}"; do
    docker_probe "$reg/$repo:$tag"
  done
  docker_probe "$image"
  echo
done
