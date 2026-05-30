#!/usr/bin/env bash
# Build Lambda deployment zip.
#
# Strategy: stage handler.py + core/ + any pip dependencies into a clean directory,
# then zip. boto3 is NOT shipped — the Lambda Python 3.11 runtime includes it.
# If you add deps to requirements-lambda.txt, they get installed here.
#
# Output: dist/function.zip + dist/function.zip.sha256

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
STAGE="$DIST/stage"
ZIPFILE="$DIST/function.zip"

echo "==> clean stage"
rm -rf "$STAGE"
mkdir -p "$STAGE"

echo "==> copy handler + core"
cp "$ROOT/lambda/handler.py" "$STAGE/handler.py"
cp -r "$ROOT/core" "$STAGE/core"

# strip pyc files from core copy
find "$STAGE/core" -name "*.pyc" -delete
find "$STAGE/core" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# install Lambda-specific pip dependencies (boto3 excluded — runtime provides it)
REQS="$ROOT/requirements-lambda.txt"
if [[ -s "$REQS" ]]; then
    echo "==> pip install into stage"
    pip install \
        --quiet \
        --requirement "$REQS" \
        --target "$STAGE" \
        --platform manylinux2014_x86_64 \
        --python-version 3.11 \
        --only-binary=:all: \
        --upgrade
else
    echo "==> no Lambda-specific deps (requirements-lambda.txt empty or missing)"
fi

echo "==> zip"
mkdir -p "$DIST"
rm -f "$ZIPFILE"
(cd "$STAGE" && zip -r "$ZIPFILE" . -x "*.pyc" -x "*/__pycache__/*" -x "*.dist-info/*" -x "bin/*")

# checksum so CI can detect accidental redeploys of the same artifact
if command -v sha256sum &>/dev/null; then
    sha256sum "$ZIPFILE" > "$ZIPFILE.sha256"
elif command -v shasum &>/dev/null; then
    shasum -a 256 "$ZIPFILE" > "$ZIPFILE.sha256"
fi

SIZE=$(du -sh "$ZIPFILE" | cut -f1)
echo "==> built: $ZIPFILE ($SIZE)"
[[ -f "$ZIPFILE.sha256" ]] && cat "$ZIPFILE.sha256"
