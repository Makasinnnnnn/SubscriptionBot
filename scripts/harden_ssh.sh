#!/usr/bin/env sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }

MODE=${1:-}
DEPLOY_USER=${2:-propgate}
KEY_FILE=${3:-}

case "$DEPLOY_USER" in
  *[!a-zA-Z0-9_-]*|'') echo "Invalid deploy username." >&2; exit 1 ;;
esac

prepare() {
  [ -f "$KEY_FILE" ] || { echo "Public key file not found." >&2; exit 1; }
  grep -Eq '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp)' "$KEY_FILE" || {
    echo "The file does not contain a supported SSH public key." >&2
    exit 1
  }
  if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$DEPLOY_USER"
  fi
  usermod -aG sudo,docker "$DEPLOY_USER"
  install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
  install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$KEY_FILE" "/home/$DEPLOY_USER/.ssh/authorized_keys"
  echo "Prepared $DEPLOY_USER. Open a NEW terminal and verify key login before finalize."
}

finalize() {
  [ "${CONFIRM_SSH_KEY_LOGIN:-}" = "yes" ] || {
    echo "Set CONFIRM_SSH_KEY_LOGIN=yes only after a successful login in a second terminal." >&2
    exit 1
  }
  [ -s "/home/$DEPLOY_USER/.ssh/authorized_keys" ] || {
    echo "No authorized key found for $DEPLOY_USER." >&2
    exit 1
  }
  cat >/etc/ssh/sshd_config.d/99-propgate-hardening.conf <<'EOF'
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
X11Forwarding no
MaxAuthTries 3
EOF
  sshd -t
  systemctl reload ssh
  echo "SSH hardened: password login disabled; root accepts keys only. Keep the current session open and test again."
}

case "$MODE" in
  prepare) prepare ;;
  finalize) finalize ;;
  *) echo "Usage: $0 prepare USER PUBLIC_KEY_FILE | CONFIRM_SSH_KEY_LOGIN=yes $0 finalize USER" >&2; exit 1 ;;
esac
