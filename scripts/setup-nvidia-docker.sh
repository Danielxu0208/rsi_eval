#!/usr/bin/env bash
# Review and run on the actual Ubuntu host console. Default: read-only preflight.
# Sources:
# https://docs.docker.com/engine/install/ubuntu/
# https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html
set -euo pipefail

install_requested=false
docker_guide=https://docs.docker.com/engine/install/ubuntu/
nvidia_guide=https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html
toolkit_version=1.20.1-1

usage() {
    cat <<'USAGE'
Usage: bash scripts/setup-nvidia-docker.sh [--install]

Without --install: inspect local prerequisites and print the installation plan.
With --install: root on the actual Ubuntu systemd host installs missing Docker
components, installs NVIDIA Container Toolkit, configures the local system daemon,
restarts Docker and runs the NVIDIA sample workload. An existing complete Docker
installation is reused. Conflicting old runtime packages are never removed.

Supported: Ubuntu 22.04/24.04/26.04 amd64 with a working NVIDIA host driver.
The script does not install a driver or grant any user access to Docker.
Docker restart can interrupt running containers; coordinate shared-host downtime.
USAGE
}
fail() {
    printf 'ERROR: %s\n' "$*" >&2
    printf 'Official guides:\n  %s\n  %s\n' "$docker_guide" "$nvidia_guide" >&2
    exit 1
}
for argument in "$@"; do
    case "$argument" in
        --install) install_requested=true ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; fail "Unsupported argument: $argument" ;;
    esac
done

[[ -r /etc/os-release ]] || fail 'Unsupported OS: /etc/os-release is unavailable.'
# This is the host's root-owned distribution metadata, not user configuration.
. /etc/os-release
[[ "${ID:-}" == ubuntu ]] || fail 'Unsupported OS: only Ubuntu is automated.'
case "${VERSION_ID:-}" in
    22.04) expected_codename=jammy ;;
    24.04) expected_codename=noble ;;
    26.04) expected_codename=resolute ;;
    *) fail 'Unsupported Ubuntu release: use the official installation guides.' ;;
esac
command -v dpkg >/dev/null || fail 'dpkg is required for the Ubuntu architecture check.'
[[ "$(dpkg --print-architecture)" == amd64 ]] || fail 'Unsupported architecture: amd64 is required.'
[[ "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}" == "$expected_codename" ]] || fail 'Unexpected Ubuntu release codename.'

# Always inspect/use the local system daemon, regardless of a remote/rootless context.
# Do not print Docker config, proxy settings, credential files or the environment.
docker_local() {
    env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
        docker --host unix:///var/run/docker.sock "$@"
}
compose_compatible() {
    [[ "$1" =~ ^v?([0-9]+)\.([0-9]+) ]] || return 1
    local major=$((10#${BASH_REMATCH[1]})) minor=$((10#${BASH_REMATCH[2]}))
    ((major > 2 || (major == 2 && minor >= 30)))
}

init_name=unknown
[[ ! -r /proc/1/comm ]] || read -r init_name < /proc/1/comm
container_detected=false
if [[ -e /.dockerenv || -e /run/.containerenv ]]; then
    container_detected=true
elif command -v systemd-detect-virt >/dev/null && systemd-detect-virt --container --quiet; then
    container_detected=true
fi
driver_ok=false
if command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,driver_version --format=csv,noheader >/dev/null 2>&1; then
    driver_ok=true
fi
docker_available=false
buildx_available=false
compose_version=unavailable
if command -v docker >/dev/null; then
    docker_available=true
    if docker_local buildx version >/dev/null 2>&1; then buildx_available=true; fi
    compose_version=$(docker_local compose version --short 2>/dev/null) || compose_version=unavailable
fi
local_daemon_accessible=false
if "$docker_available" && docker_local info --format '{{.ServerVersion}}' >/dev/null 2>&1; then
    local_daemon_accessible=true
fi
docker_complete=false
if "$docker_available" && "$buildx_available" && compose_compatible "$compose_version"; then
    if command -v dockerd >/dev/null || "$local_daemon_accessible"; then
        docker_complete=true
    fi
fi
conflicts=()
for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
    status=$(dpkg-query --show --showformat='${Status}' "$package" 2>/dev/null) || status=unavailable
    if [[ "$status" == 'install ok installed' ]]; then conflicts+=("$package"); fi
done

printf 'Ubuntu %s amd64; UID=%s; PID 1=%s; container detected=%s\n' "$VERSION_ID" "$EUID" "$init_name" "$container_detected"
printf 'NVIDIA driver works=%s; Docker CLI=%s; local daemon accessible=%s\n' "$driver_ok" "$docker_available" "$local_daemon_accessible"
printf 'Buildx=%s; Compose=%s; reusable Docker=%s\n' "$buildx_available" "$compose_version" "$docker_complete"
if ((${#conflicts[@]})); then printf 'Existing distro runtime packages: %s\n' "${conflicts[*]}"; fi
printf '\nInstallation plan (--install only):\n'
if "$docker_complete"; then
    printf '  Reuse the existing Docker Engine, Buildx and Compose.\n'
else
    printf '  Configure the official Docker apt repository; install missing Engine/Buildx/Compose packages.\n'
    printf '  Existing packages are not explicitly upgraded; conflicting runtimes require manual review.\n'
fi
printf '  Configure NVIDIA stable apt repository; install Container Toolkit %s.\n' "$toolkit_version"
printf '  Back up existing daemon.json, then nvidia-ctk runtime configure --runtime=docker.\n'
printf '  Restart the local Docker daemon; running containers can stop or restart.\n'
printf '  Run: docker run --rm --runtime=nvidia --gpus all ubuntu:24.04 nvidia-smi\n'
printf '  Leave user groups, Docker socket permissions and host security policy unchanged.\n'
printf 'Official guides:\n  %s\n  %s\n' "$docker_guide" "$nvidia_guide"

if ! "$install_requested"; then
    printf '\nPreflight only: no network download, package, daemon or permission changes.\n'
    printf 'Review this file, then run --install as root from the actual host console.\n'
    exit 0
fi

[[ "$EUID" == 0 ]] || fail '--install requires root on the host.'
[[ "$init_name" == systemd ]] || fail 'A systemd host console is required; this shell is not a supported host session.'
! "$container_detected" || fail 'Run from the real host console, not from a container.'
"$driver_ok" || fail 'The NVIDIA host driver must work before installation; this script does not install drivers.'
for command_name in apt-get dpkg-query install mktemp systemctl unshare; do
    command -v "$command_name" >/dev/null || fail "Required host command is missing: $command_name"
done
unshare --mount true >/dev/null 2>&1 || fail 'Root cannot create a mount namespace here. Use the unrestricted host console; security policy is not weakened.'
if ! "$docker_complete" && ((${#conflicts[@]})); then
    fail "Conflicting runtime packages need administrator review: ${conflicts[*]}. Nothing has been installed or removed."
fi
if "$docker_complete"; then
    unit_state=$(systemctl show --property=LoadState --value docker.service 2>/dev/null) || unit_state=unknown
    [[ "$unit_state" == loaded ]] || fail 'Existing Docker does not use the supported system docker.service; configure it with the official guide.'
fi

# Keep backups private; do not display daemon settings, credentials or apt output.
backup_dir=/var/backups/rsi-eval-gpu-host/$(date -u +%Y%m%dT%H%M%SZ)-$$
install -d -m 0700 "$backup_dir"
work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT
backup_file() {
    [[ ! -L "$1" ]] || fail "Refusing a symlink at managed configuration path: $1"
    if [[ -f "$1" ]]; then cp -p -- "$1" "$backup_dir/$(basename "$1")"; fi
}
quiet_step() {
    local label=$1
    shift
    printf '%s\n' "$label"
    "$@" >/dev/null 2>&1 || fail "$label failed. Inspect the relevant host tool locally; no command output or credentials were copied into logs."
}
repository_present() {
    local path
    for path in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
        if [[ -f "$path" ]] && grep -Fq -- "$1" "$path"; then return 0; fi
    done
    return 1
}

quiet_step 'Refresh apt metadata' apt-get update
quiet_step 'Install repository tools' apt-get install -y --no-remove --no-install-recommends ca-certificates curl gnupg
if ! "$docker_complete"; then
    if ! repository_present 'https://download.docker.com/linux/ubuntu'; then
        install -m 0755 -d /etc/apt/keyrings
        backup_file /etc/apt/keyrings/docker.asc
        backup_file /etc/apt/sources.list.d/docker.sources
        quiet_step 'Download official Docker signing key' curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$work_dir/docker.asc"
        install -m 0644 "$work_dir/docker.asc" /etc/apt/keyrings/docker.asc
        cat > "$work_dir/docker.sources" <<DOCKER_SOURCE
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $expected_codename
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
DOCKER_SOURCE
        install -m 0644 "$work_dir/docker.sources" /etc/apt/sources.list.d/docker.sources
    fi
    quiet_step 'Refresh official Docker metadata' apt-get update
    quiet_step 'Install missing Docker packages (no removals)' apt-get install -y --no-remove --no-upgrade \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
compose_version=$(docker_local compose version --short 2>/dev/null) || fail 'Docker Compose plugin is missing.'
compose_compatible "$compose_version" || fail 'Compose 2.30.0 or newer is required; update the existing plugin explicitly on the host.'
docker_local buildx version >/dev/null 2>&1 || fail 'Docker Buildx plugin is missing.'

if ! repository_present 'https://nvidia.github.io/libnvidia-container/stable/deb'; then
    install -m 0755 -d /usr/share/keyrings
    backup_file /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    backup_file /etc/apt/sources.list.d/nvidia-container-toolkit.list
    quiet_step 'Download official NVIDIA signing key' curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey -o "$work_dir/nvidia-key.asc"
    quiet_step 'Create NVIDIA apt keyring' gpg --batch --yes --dearmor -o "$work_dir/nvidia-key.gpg" "$work_dir/nvidia-key.asc"
    install -m 0644 "$work_dir/nvidia-key.gpg" /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    quiet_step 'Download official NVIDIA stable repository definition' curl -fsSL \
        https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list -o "$work_dir/nvidia.list"
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        "$work_dir/nvidia.list" > "$work_dir/nvidia-signed.list"
    install -m 0644 "$work_dir/nvidia-signed.list" /etc/apt/sources.list.d/nvidia-container-toolkit.list
fi
quiet_step 'Refresh NVIDIA metadata' apt-get update
quiet_step 'Install NVIDIA Container Toolkit (no removals)' apt-get install -y --no-remove \
    "nvidia-container-toolkit=$toolkit_version" "nvidia-container-toolkit-base=$toolkit_version" \
    "libnvidia-container-tools=$toolkit_version" "libnvidia-container1=$toolkit_version"
backup_file /etc/docker/daemon.json
quiet_step 'Configure the local NVIDIA Docker runtime' nvidia-ctk runtime configure --runtime=docker
printf 'Restarting Docker now. Running containers can stop or restart on this host.\n'
quiet_step 'Restart local docker.service' systemctl restart docker
quiet_step 'Check local system Docker daemon' docker_local info --format '{{.ServerVersion}}'
printf 'Run the NVIDIA sample workload (downloads ubuntu:24.04 if needed).\n'
docker_local run --rm --runtime=nvidia --gpus all ubuntu:24.04 nvidia-smi
printf 'GPU container check completed. Private configuration backups: %s\n' "$backup_dir"
printf 'Use the normal experiment account with its existing approved Docker access.\n'
