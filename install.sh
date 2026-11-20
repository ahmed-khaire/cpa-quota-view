#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$ICON_DIR" "$APP_DIR" "$UNIT_DIR"

cp "$REPO_DIR"/icons/qv-*-symbolic.svg "$ICON_DIR/"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" || true
fi

sed "s|__INSTALL_DIR__|$REPO_DIR|g" "$REPO_DIR/desktop/quota-view.desktop.example" > "$APP_DIR/quota-view.desktop"
sed "s|__INSTALL_DIR__|$REPO_DIR|g" "$REPO_DIR/systemd/quota-view.service.example" > "$UNIT_DIR/quota-view.service"

chmod 755 "$REPO_DIR/quota_view.py"
systemctl --user daemon-reload
systemctl --user enable --now quota-view.service

echo "Installed icons, desktop entry, and enabled quota-view.service (pointing to $REPO_DIR)."
