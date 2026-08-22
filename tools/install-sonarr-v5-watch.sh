#!/bin/bash
# Installs (or reinstalls) the weekly launchd agent that runs
# tools/sonarr-v5-watch.py every Monday at 9:00 local time.
# Idempotent; run again after moving the repo.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/tools/sonarr-v5-watch.py"
LABEL="com.infiniteloopalchemist.sonarr-v5-watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"
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
    <string>/tmp/sonarr-v5-watch.err</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: $LABEL (Mondays 9:00 local), plist at $PLIST"
