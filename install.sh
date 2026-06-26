#!/usr/bin/env bash
# Thin wrapper around `golem.py install`.
# Extra args are passed through, e.g.:  ./install.sh --config ~/my-config.toml
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/golem.py" install "$@"
