# Source from the repository root after starting the dedicated Colima profile.
# Keep Docker configuration local; do not change the user's default context.
export DOCKER_HOST="unix://${HOME}/.colima/rsi-eval/docker.sock"
export DOCKER_CONFIG="${PWD}/.runtime/docker"
export PATH="/opt/homebrew/bin:${PATH}"
mkdir -p "$DOCKER_CONFIG"
if [ ! -f "$DOCKER_CONFIG/config.json" ]; then
  cat > "$DOCKER_CONFIG/config.json" <<'JSON'
{"cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]}
JSON
fi
