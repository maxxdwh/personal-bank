#!/bin/zsh
# Installs the daily sync as a macOS launchd job (07:30 and at every login). Run in a normal terminal after the
# first successful sync. Remove with: launchctl bootout gui/$(id -u)/com.personal-bank.sync
set -euo pipefail
cd "$(dirname "$0")/.."
LABEL=com.personal-bank.sync
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/.personal-bank/sync.log"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PWD/run_sync.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>Umask</key><integer>63</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL. Log: $LOG"
