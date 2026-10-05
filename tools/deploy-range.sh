#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 || $1 == -* || $2 != /* ]]; then
    echo "Usage: $0 user@host /absolute/target/directory" >&2
    exit 2
fi

target=$1
# Quote for the remote login shell, including paths containing spaces or apostrophes.
escaped_quote="'\\''"
directory="'${2//\'/$escaped_quote}'"
cd "$(dirname "$0")/.."

architecture=$(ssh "$target" "set -eu; cd $directory; test -f .env; docker compose version >/dev/null; docker version --format '{{.Server.Arch}}'")
case "$architecture" in
    amd64|arm64) ;;
    *) echo "Unsupported target architecture: $architecture" >&2; exit 1 ;;
esac

backend=ghcr.io/meral-it/meyton-live-display-backend:range-test
frontend=ghcr.io/meral-it/meyton-live-display-frontend:range-test
docker build --platform "linux/$architecture" -t "$backend" -f backend/Dockerfile .
docker build --platform "linux/$architecture" -t "$frontend" -f frontend/Dockerfile .

archive=$(mktemp)
trap 'rm -f "$archive"' EXIT
docker save -o "$archive" "$backend" "$frontend"

# Keep installation-specific Compose edits (e.g. local SDF mounts).
ssh "$target" "set -eu; cd $directory; if test -f compose.yaml; then cat >/dev/null; else cat >compose.yaml; fi" <compose.yaml
ssh "$target" "set -eu; cd $directory
archive=\$(mktemp ./range-images.XXXXXX)
trap 'rm -f \"\$archive\"' EXIT
cat >\"\$archive\"
docker load -i \"\$archive\"
IMAGE_TAG=range-test docker compose -f compose.yaml up -d --pull never --no-build
" <"$archive"
