#!/bin/sh
# Install a published client and register its local Codex MCP entry.
set -eu

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

case "$(uname -s):$(uname -m)" in
    Darwin:arm64) platform=macos-arm64 ;;
    Linux:x86_64) platform=linux-x86_64 ;;
    Linux:aarch64|Linux:arm64) platform=linux-arm64 ;;
    *) fail "Supported platforms: Apple Silicon macOS, Linux x86-64, Linux ARM64." ;;
esac

[ "$#" -le 1 ] || fail "Usage: sh install-client.sh [vX.Y.Z]"
releases=https://github.com/shk95/agent-journal/releases
tag=${1:-}
if [ -z "$tag" ]; then
    release_url=$(curl -fsSL --proto '=https' -o /dev/null -w '%{url_effective}' "$releases/latest")
    tag=${release_url#"$releases/tag/"}
fi
printf '%s\n' "$tag" | grep -Eq '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' ||
    fail "Expected a stable release tag in the form vX.Y.Z."

bin_dir=${AGENT_JOURNAL_BIN_DIR:-"$HOME/.local/bin"}
mkdir -p "$bin_dir"
bin_dir=$(cd "$bin_dir" && pwd -P)
temp=$(mktemp -d "$bin_dir/.agent-journal.XXXXXX")
trap 'rm -rf "$temp"' EXIT
trap 'exit 1' HUP INT TERM

asset=agent-journal-$platform
download=$releases/download/$tag
curl -fsSL --proto '=https' -o "$temp/checksums" "$download/client-sha256sums.txt"
curl -fsSL --proto '=https' -o "$temp/$asset" "$download/$asset"
expected=$(awk -v asset="$asset" '$2 == asset { print $1 }' "$temp/checksums")
if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum < "$temp/$asset")
else
    actual=$(shasum -a 256 < "$temp/$asset")
fi
[ "${actual%% *}" = "$expected" ] || fail "Client checksum verification failed."
chmod 0755 "$temp/$asset"
[ "$("$temp/$asset" --version)" = "agent-journal ${tag#v}" ] ||
    fail "Client version verification failed."
client=$bin_dir/agent-journal
[ ! -d "$client" ] || fail "$client is a directory."
mv -f "$temp/$asset" "$client"
printf 'Installed Agent Journal %s at %s\n' "$tag" "$client"

if command -v codex >/dev/null 2>&1; then
    codex mcp add journal -- "$client" mcp
    printf 'Restart Codex to use the journal MCP tools.\n'
else
    printf 'After installing Codex, run: codex mcp add journal -- "%s" mcp\n' "$client"
fi

printf 'For a first-time server connection, run "%s" connect.\n' "$client"
