#!/usr/bin/env bash
# Run dprobe's tests.
#
#   ./test.sh               the unit tests; no database needed
#   ./test.sh -i            also the integration tests: starts the four databases in
#                           tests/compose.yaml (about 5 GB of memory) and leaves them running
#   ./test.sh -i --down     the same, then stops the databases
#   ./test.sh -k binds -x   anything else goes to pytest
#
# The integration tests read $DPROBE_IT_CONFIG, default tests/it.example.yaml;
# tests/it.example.override.yaml beside it is merged in, e.g. for local passwords.

set -euo pipefail
cd "$(dirname "$0")"

integration=false
down=false
pytest_args=()
for arg in "$@"; do
    case "$arg" in
        -i|--integration) integration=true ;;
        --down) down=true ;;
        -h|--help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
        *) pytest_args+=("$arg") ;;
    esac
done

if ! $integration; then
    exec uv run pytest ${pytest_args[@]+"${pytest_args[@]}"}
fi

if ! command -v docker >/dev/null; then
    echo "test.sh: --integration needs docker (with the compose plugin)" >&2
    exit 1
fi
compose=(docker compose -f tests/compose.yaml)
if $down; then
    trap '"${compose[@]}" down' EXIT
fi
# A no-op when they're already up; --wait returns once every health check passes.
"${compose[@]}" up -d --wait
DPROBE_IT_CONFIG=${DPROBE_IT_CONFIG:-tests/it.example.yaml} uv run pytest -rs ${pytest_args[@]+"${pytest_args[@]}"}
