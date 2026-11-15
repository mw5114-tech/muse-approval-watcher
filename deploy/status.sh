#!/usr/bin/env bash
set -eu
systemctl show muse-watcher.service -p ActiveState -p SubState -p Result -p NRestarts -p MemoryCurrent -p CPUUsageNSec
sudo journalctl --unit=muse-watcher.service --lines=50 --no-pager
