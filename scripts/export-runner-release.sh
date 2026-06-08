#!/usr/bin/env bash
# Build a clean, OSWorld-style runner-only release tree from this workspace.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

OUT_DIR="${1:-dist/mypcbench-runner}"
TARBALL="${2:-dist/mypcbench-runner.tar.gz}"
MANIFEST="release-files.txt"
OUT_DIR_ABS="$(realpath -m "$OUT_DIR")"
TARBALL_ABS="$(realpath -m "$TARBALL")"

case "$OUT_DIR_ABS" in
  "/"|"$REPO_ROOT")
    echo "Refusing unsafe export directory: $OUT_DIR_ABS" >&2
    exit 1
    ;;
esac

case "$TARBALL_ABS" in
  "$OUT_DIR_ABS"/*)
    echo "Refusing tarball path inside export directory: $TARBALL_ABS" >&2
    exit 1
    ;;
esac

if [ ! -f "$MANIFEST" ]; then
  echo "Missing $MANIFEST" >&2
  exit 1
fi

missing=0
while IFS= read -r path; do
  case "$path" in
    ""|\#*) continue ;;
  esac
  if [ ! -e "$path" ]; then
    echo "Missing release path: $path" >&2
    missing=1
  fi
done < "$MANIFEST"
if [ "$missing" -ne 0 ]; then
  exit 1
fi

rm -rf "$OUT_DIR" "$TARBALL"
mkdir -p "$OUT_DIR" "$(dirname "$TARBALL")"

tar \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  --exclude='results' \
  --exclude='mypcbench-vm' \
  --exclude='*.qcow2' \
  --exclude='OVMF_*.fd' \
  --exclude-vcs \
  -cf - \
  --files-from <(grep -vE '^[[:space:]]*(#|$)' "$MANIFEST") \
  | tar -xf - -C "$OUT_DIR"

find "$OUT_DIR" -type f | sed "s#^$OUT_DIR/##" | sort > "$OUT_DIR/RELEASE_FILE_LIST.txt"

if grep -E '(^|/)(web-apps|vm-setup|generated_data|data|results|node_modules|\.venv|MyPCBenchHUMAN|COLM|mypcbench-vm)(/|$)' "$OUT_DIR/RELEASE_FILE_LIST.txt"; then
  echo "Runner-only export contains a forbidden build/audit path" >&2
  exit 1
fi

tar \
  --sort=name \
  --mtime="@${SOURCE_DATE_EPOCH:-0}" \
  --owner=0 \
  --group=0 \
  --numeric-owner \
  --transform='s#^\.$#mypcbench-runner#;s#^\./#mypcbench-runner/#' \
  -cf - \
  -C "$OUT_DIR" \
  . \
  | gzip -n > "$TARBALL"

echo "Wrote $OUT_DIR"
echo "Wrote $TARBALL"
