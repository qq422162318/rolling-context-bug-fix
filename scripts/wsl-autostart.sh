#!/usr/bin/env bash
# Called by the Windows scheduled task; runs the fix inside the WSL distro.
# Place next to fix-proxy.sh and point the task's Action at:
#   wsl.exe -e bash -lc "cd /home/user/rolling-context-bug-fix && ./fix-proxy.sh"
exec bash "$(dirname "$0")/fix-proxy.sh"
