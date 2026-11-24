#!/bin/bash
# Installs (or reinstalls) the weekly launchd agent that runs
# tools/sonarr-v5-watch.py every Monday at 9:00 local time.
# Idempotent; run again after editing sonarr-v5-watch.py or moving the repo.
#
# launchd jobs cannot read ~/Documents (macOS privacy blocks them, python
# exits 2 with "Operation not permitted"), so the agent runs a deployed
# copy under ~/.local/share, never the repo file. Re-run this script after
# every edit to sonarr-v5-watch.py or the deployed copy goes stale.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$HOME/.local/share/sonarr-v5-watch"
SCRIPT="$DEPLOY_DIR/sonarr-v5-watch.py"
LABEL="com.infiniteloopalchemist.sonarr-v5-watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ERR_LOG="$HOME/Library/Logs/sonarr-v5-watch.err"

mkdir -p "$DEPLOY_DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
install -m 644 "$REPO_ROOT/tools/sonarr-v5-watch.py" "$SCRIPT"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/arch</string>
        <string>-arm64</string>
        <string>/usr/bin/python3</string>
        <string>$SCRIPT</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key><integer>1</integer>
        <key>Hour</key><integer>9</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardErrorPath</key>
    <string>$ERR_LOG</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: $LABEL (Mondays 9:00 local), runs $SCRIPT, plist at $PLIST"
