#!/usr/bin/env bash
# Install Docker Engine + compose plugin + NVIDIA Container Toolkit inside a
# WSL2 Ubuntu distro. Idempotent; safe to re-run. Called by setup.ps1.
#
# Why: Docker Desktop on Windows keeps creating AF_UNIX reparse-point socket
# files that Windows can't remove, crashing Docker Desktop on every restart.
# Running Docker Engine natively inside WSL2 sidesteps that entire surface.
set -euo pipefail

say()  { printf "\033[36m>> %s\033[0m\n" "$*"; }
ok()   { printf "\033[32m   OK: %s\033[0m\n" "$*"; }
warn() { printf "\033[33m   WARN: %s\033[0m\n" "$*"; }

if [ "$(id -u)" = "0" ]; then SUDO=""; else SUDO="sudo"; fi

# -- Enable systemd in /etc/wsl.conf so docker.service runs as a systemd unit
if [ ! -f /etc/wsl.conf ] || ! grep -q '^systemd=true' /etc/wsl.conf; then
    say "Enabling systemd in /etc/wsl.conf"
    $SUDO tee /etc/wsl.conf > /dev/null <<'EOF'
[boot]
systemd=true

[network]
generateResolvConf=true
EOF
    echo "   Note: 'wsl --shutdown' on Windows required before systemd activates"
fi

# -- Docker Engine (official apt repo) --------------------------------------
# Note: Docker Desktop injects a `/usr/bin/docker` shim into every WSL distro
# via its integration feature, so `command -v docker` succeeds even when no
# real Docker CE is installed. We check `dpkg -l docker-ce` instead.
if ! dpkg -l docker-ce 2>/dev/null | grep -q "^ii"; then
    # Remove Docker Desktop's WSL-integration shim so it doesn't shadow the
    # real docker we're about to install. (If Docker Desktop later reinjects
    # it, the apt-installed docker remains on PATH ahead of it.)
    if [ -L /usr/bin/docker ] || file /usr/bin/docker 2>/dev/null | grep -q "ASCII text"; then
        say "Removing Docker Desktop WSL-integration shim at /usr/bin/docker"
        $SUDO rm -f /usr/bin/docker
    fi

    say "Installing Docker Engine from download.docker.com"
    $SUDO apt-get update -qq
    $SUDO apt-get install -qq -y ca-certificates curl gnupg
    $SUDO install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | $SUDO tee /etc/apt/keyrings/docker.asc > /dev/null
    $SUDO chmod a+r /etc/apt/keyrings/docker.asc
    ARCH=$(dpkg --print-architecture)
    CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
    echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $CODENAME stable" \
        | $SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null
    $SUDO apt-get update -qq
    $SUDO apt-get install -qq -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    ok "Docker Engine installed"
else
    ok "Docker Engine already installed"
fi
docker --version 2>/dev/null | sed 's/^/   /'

# -- Add current user to docker group so sudo isn't needed for docker calls
CURRENT_USER="${SUDO_USER:-$USER}"
if [ -n "$CURRENT_USER" ] && [ "$CURRENT_USER" != "root" ]; then
    if ! id -nG "$CURRENT_USER" 2>/dev/null | grep -qw docker; then
        say "Adding $CURRENT_USER to the docker group"
        $SUDO usermod -aG docker "$CURRENT_USER"
        echo "   Note: a new WSL session will pick up the group"
    fi
fi

# -- NVIDIA Container Toolkit (only if WSL CUDA is working) -----------------
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    if ! command -v nvidia-ctk >/dev/null 2>&1; then
        say "Installing NVIDIA Container Toolkit"
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
            | $SUDO gpg --dearmor -o /etc/apt/keyrings/nvidia-container-toolkit.gpg
        curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
            | sed 's#deb https://#deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit.gpg] https://#g' \
            | $SUDO tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null
        $SUDO apt-get update -qq
        $SUDO apt-get install -qq -y nvidia-container-toolkit
    fi
    say "Configuring Docker to use nvidia runtime"
    $SUDO nvidia-ctk runtime configure --runtime=docker >/dev/null
    ok "nvidia-container-toolkit: $(nvidia-ctk --version 2>/dev/null | head -1)"
else
    warn "nvidia-smi not available inside WSL -- GPU acceleration disabled."
    warn "Install the WSL CUDA driver on Windows to enable GPU support:"
    warn "  https://developer.nvidia.com/cuda/wsl"
fi

# -- Start docker -----------------------------------------------------------
if pidof systemd >/dev/null 2>&1; then
    $SUDO systemctl enable --now docker.service
    ok "Docker running (systemd)"
else
    # systemd not active yet -- either never enabled, or /etc/wsl.conf just
    # got written and WSL needs a shutdown. Fall back to service/dockerd.
    if ! pgrep -f dockerd >/dev/null 2>&1; then
        $SUDO service docker start 2>/dev/null || {
            nohup $SUDO dockerd > /var/log/dockerd.log 2>&1 &
            sleep 3
        }
    fi
    warn "systemd not yet active -- run 'wsl --shutdown' on Windows and re-open the distro"
fi

# -- Verify -----------------------------------------------------------------
if docker info >/dev/null 2>&1 || $SUDO docker info >/dev/null 2>&1; then
    ok "docker info: daemon reachable"
else
    warn "docker info failed -- try 'wsl --shutdown' then reopen the distro"
fi

echo ""
ok "Docker-in-WSL install complete"
