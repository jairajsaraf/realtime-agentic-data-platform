#!/usr/bin/env bash
#
# Stage E6 — single-host bootstrap for the rtdp deploy target (Ubuntu LTS).
#
# Idempotent and safe to rerun. Installs Docker Engine + the Compose plugin, creates an
# unprivileged `deploy` user for the gated CI/CD SSH deploy, configures a deny-by-default firewall
# (allowing only SSH/HTTP/HTTPS), prepares the deploy directory, and optionally installs the Doppler
# CLI for runtime secret injection.
#
# It does NOT: call any cloud/provider API, create a droplet, write any secret, or force SSH
# hardening that could lock you out (it only PRINTS hardening suggestions). Run it on the host AFTER
# the droplet exists — see RUNBOOK "Stage E6 go-live" (E6.2).
#
# Usage (on the host, as a sudo-capable user):
#   sudo bash deploy/bootstrap_host.sh
# Override defaults via env, e.g.:
#   sudo DEPLOY_USER=deploy DEPLOY_DIR=/opt/rtdp INSTALL_DOPPLER=1 bash deploy/bootstrap_host.sh

set -euo pipefail

# --- configurable (no secrets) ---
DEPLOY_USER="${DEPLOY_USER:-deploy}"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/rtdp}"
INSTALL_DOPPLER="${INSTALL_DOPPLER:-1}"   # 1 = install Doppler CLI for host-side `doppler run`

log() { printf '>> %s\n' "$*"; }

# --- must run as root (via sudo); no assumption that root login itself is enabled ---
if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run with sudo, e.g. 'sudo bash deploy/bootstrap_host.sh'." >&2
  exit 1
fi

# Targets Debian/Ubuntu (apt). Bail clearly elsewhere.
if ! command -v apt-get >/dev/null 2>&1; then
  echo "ERROR: this script targets Ubuntu LTS (apt-get not found)." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

# --- 1. Docker Engine + Compose plugin (official Docker apt repository) ---
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  log "Docker + Compose plugin already present — skipping install."
else
  log "Installing Docker Engine + Compose plugin from Docker's official apt repo..."
  apt-get update -y
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  # Overwrite the key each run so a partial previous run can't wedge it (idempotent).
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

# --- 2. Unprivileged deploy user in the docker group (least privilege for compose) ---
# Membership in `docker` is the minimum needed to run `docker compose` for the deploy. NOTE: the
# docker group is effectively root-equivalent on the host — that is the documented trade-off of a
# docker-group deploy user vs. granting narrow sudo. No password is set (key-based SSH only).
if id -u "$DEPLOY_USER" >/dev/null 2>&1; then
  log "User '$DEPLOY_USER' already exists."
else
  log "Creating deploy user '$DEPLOY_USER' (no password; SSH key only)..."
  useradd --create-home --shell /bin/bash "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"

# Prepare ~/.ssh and an empty authorized_keys so a PUBLIC key can be added out-of-band.
# (No key material is written here — that manual step keeps all secrets out of this repo.)
deploy_home="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$deploy_home/.ssh"
if [ ! -f "$deploy_home/.ssh/authorized_keys" ]; then
  install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /dev/null "$deploy_home/.ssh/authorized_keys"
fi

# --- 3. Deploy directory (owned by the deploy user) ---
log "Preparing deploy directory: $DEPLOY_DIR"
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$DEPLOY_DIR"

# --- 4. Firewall: default deny inbound, allow only SSH/HTTP/HTTPS ---
# Allow SSH FIRST, then enable, so enabling UFW can never cut off the current SSH session.
if ! command -v ufw >/dev/null 2>&1; then
  apt-get install -y ufw
fi
log "Configuring UFW (default deny inbound; allow OpenSSH, 80, 443)..."
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH        # 22/tcp — keep BEFORE enabling so we never lock ourselves out
ufw allow 80/tcp         # Caddy HTTP (ACME challenge + redirect to HTTPS)
ufw allow 443/tcp        # Caddy HTTPS
# 8000 (API), 9001 (MinIO console), 4317 (OTLP) are intentionally NOT opened — they stay on
# localhost / the internal compose network. Bind the API to 127.0.0.1 via RTDP_API_BIND on the host.
ufw --force enable
ufw status verbose || true

# --- 5. (Optional) Doppler CLI for runtime secret injection (`doppler run -- docker compose ...`) ---
if [ "$INSTALL_DOPPLER" = "1" ]; then
  if command -v doppler >/dev/null 2>&1; then
    log "Doppler CLI already installed."
  else
    log "Installing Doppler CLI (official installer)..."
    # No token is configured here — authenticate the host out-of-band (e.g. a Doppler service token
    # in the deploy user's environment). Never commit a token.
    curl -Ls --tlsv1.2 --proto "=https" --retry 3 https://cli.doppler.com/install.sh | sh
  fi
else
  log "Skipping Doppler CLI install (INSTALL_DOPPLER != 1)."
fi

# --- Done: print manual, secret-free next steps + hardening SUGGESTIONS (not enforced) ---
cat <<EOF

Bootstrap complete (idempotent — safe to rerun).

Next (manual, out-of-band — no secrets in this repo):
  1. Add your SSH PUBLIC key to: ${deploy_home}/.ssh/authorized_keys   (as user '${DEPLOY_USER}')
  2. Place the repo's deploy assets under: ${DEPLOY_DIR}   (e.g. 'git clone' there as '${DEPLOY_USER}')
  3. Configure Doppler on the host (service token in the deploy user's env) if using 'doppler run'.
  4. In GitHub, set the 'production' environment secrets DEPLOY_SSH_HOST/USER/KEY/PATH.

SSH hardening SUGGESTIONS (review and apply yourself — NOT changed automatically, to avoid lockout):
  - Disable password auth:  set 'PasswordAuthentication no' in /etc/ssh/sshd_config (key auth only)
  - Disable root SSH login:  set 'PermitRootLogin no'
  - Then reload ssh, AFTER confirming key login works in a SEPARATE session: sudo systemctl reload ssh
EOF
