#!/bin/sh
set -eu

if [ -n "${FORKKEEPER_APP_PRIVATE_KEY_FILE:-}" ]; then
    FORKKEEPER_APP_PRIVATE_KEY=$(cat "$FORKKEEPER_APP_PRIVATE_KEY_FILE")
    export FORKKEEPER_APP_PRIVATE_KEY
fi

interval=${FORKKEEPER_INTERVAL_SECONDS:-0}
case "$interval" in
    ''|*[!0-9]*)
        echo "FORKKEEPER_INTERVAL_SECONDS must be a nonnegative integer" >&2
        exit 2
        ;;
esac

if [ "$interval" -eq 0 ]; then
    exec forkkeeper-public "$@"
fi

child=
stop() {
    trap - INT TERM
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
    fi
    exit 0
}
trap stop INT TERM

while :; do
    forkkeeper-public "$@" &
    child=$!
    wait "$child" || true
    child=
    sleep "$interval" &
    child=$!
    wait "$child" || true
    child=
done
