#!/bin/sh
# Usage: aves-entrypoint web|worker
set -eu

role="${1:-web}"
mkdir -p "$AVES_DATA_DIR" "$AVES_MODELS_DIR"

models_current() {
    # True when the installed models match the revision pinned in model-version.json.
    python - <<'EOF'
import json, os, pathlib, sys
wanted = json.loads(pathlib.Path("/app/model-version.json").read_text())
info = pathlib.Path(os.environ["AVES_MODELS_DIR"]) / "model-build-info.json"
try:
    installed = json.loads(info.read_text())
except (OSError, ValueError):
    sys.exit(1)
present = all((info.parent / name).exists() for name in wanted["files"])
sys.exit(0 if present and installed.get("revision") == wanted["revision"] else 1)
EOF
}

case "$role" in
    worker)
        # Only the worker downloads, so two containers never fetch the 1.8 GB model bundle at once.
        if ! models_current; then
            echo "Downloading the Aves Caesar models (about 1.8 GB) into $AVES_MODELS_DIR ..."
            python /app/scripts/models/download_release_models.py --output-dir "$AVES_MODELS_DIR"
        fi
        exec python -m aves_server.worker
        ;;
    web)
        exec uvicorn aves_server.api:app --host 0.0.0.0 --port "${AVES_PORT:-8080}" --proxy-headers --forwarded-allow-ips='*'
        ;;
    *)
        exec "$@"
        ;;
esac
