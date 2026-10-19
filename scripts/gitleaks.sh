#!/bin/sh
# Scan every commit on every branch with gitleaks: a pinned release, whose
# SHA-256 is checked before it runs, in a throwaway directory.
#   scripts/gitleaks.sh            (Linux x64 or macOS arm64)
set -eu
version=8.30.1
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) asset=linux_x64; sum=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb ;;
  Darwin-arm64) asset=darwin_arm64; sum=b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5 ;;
  *) echo "gitleaks.sh: no pinned gitleaks for $(uname -s)-$(uname -m)" >&2; exit 2 ;;
esac
d=$(mktemp -d "${TMPDIR:-/tmp}/cxd-gitleaks.XXXXXX")
trap 'rm -rf "$d"' EXIT
url="https://github.com/gitleaks/gitleaks/releases/download/v$version/gitleaks_${version}_$asset.tar.gz"
curl -fsSL -o "$d/gitleaks.tar.gz" "$url"
if command -v sha256sum >/dev/null 2>&1; then
  echo "$sum  $d/gitleaks.tar.gz" | sha256sum -c - >/dev/null
else
  echo "$sum  $d/gitleaks.tar.gz" | shasum -a 256 -c - >/dev/null
fi
tar -xzf "$d/gitleaks.tar.gz" -C "$d" gitleaks
cd "$(dirname "$0")/.."
"$d/gitleaks" git --no-banner --redact --log-opts="--all" .
