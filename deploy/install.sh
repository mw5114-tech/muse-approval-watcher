#!/usr/bin/env bash
set -euo pipefail

[[ $(id -u) == 0 && $(uname -m) == aarch64 && $(hostname -s) == muse-approval-watcher ]] || {
    echo 'Run as root on the dedicated muse-approval-watcher ARM64 Ubuntu VM.' >&2
    exit 1
}
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
apt-get update
apt-get install -y python3-venv
id muse-watcher >/dev/null 2>&1 || useradd --system --home-dir /var/lib/muse-watcher --shell /usr/sbin/nologin muse-watcher
install -d -m 0755 /opt/muse-watcher /etc/muse-watcher
install -d -o muse-watcher -g muse-watcher -m 0700 /var/lib/muse-watcher
install -m 0644 "$root/watcher.py" "$root/requirements.txt" /opt/muse-watcher/
python3 -m venv /opt/muse-watcher/.venv
/opt/muse-watcher/.venv/bin/pip install -r /opt/muse-watcher/requirements.txt
PLAYWRIGHT_BROWSERS_PATH=/opt/muse-watcher/browsers /opt/muse-watcher/.venv/bin/playwright install --with-deps chromium
test -e /etc/muse-watcher/environment ||
    install -m 0600 "$root/.env.example" /etc/muse-watcher/environment
test -e /etc/muse-watcher/config.json ||
    install -m 0644 "$root/config.example.json" /etc/muse-watcher/config.json
install -m 0644 "$root/deploy/muse-watcher.service" /etc/systemd/system/
systemctl daemon-reload
echo 'Service installed. Complete configuration and interactive authentication before enabling.'
