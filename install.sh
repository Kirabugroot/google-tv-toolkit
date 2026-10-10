#!/bin/zsh
set -e
REPO="${0:A:h}"
AGENTS="$HOME/Library/LaunchAgents"

brew list --cask android-platform-tools >/dev/null 2>&1 || brew install --cask android-platform-tools
brew list scrcpy >/dev/null 2>&1 || brew install scrcpy
command -v catt >/dev/null || pipx install catt

python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q -r "$REPO/discord/requirements.txt"

mkdir -p "$HOME/.config/tvpresence" "$HOME/.local/bin" "$AGENTS"
ln -sf "$REPO/bin/tvcast" "$REPO/bin/tvscreen" "$HOME/.local/bin/"

for name in tvbot tvrecap; do
  plist="$AGENTS/tv.toolkit.${name#tv}.plist"
  sed "s|__REPO__|$REPO|g; s|__HOME__|$HOME|g" "$REPO/launchd/$name.plist" > "$plist"
  launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
done

echo "OK. Config attendue dans ~/.config/tvpresence/ (voir README)."
