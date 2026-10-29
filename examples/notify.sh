#!/usr/bin/env bash
# Usage: NOTIFY_API_TOKEN=... ./examples/notify.sh 'Title' 'Message' [severity] [source]
set -euo pipefail
if [[ $# -lt 2 || $# -gt 4 ]]; then
    echo "Usage: $0 TITLE MESSAGE [info|warning|critical] [source]" >&2
    exit 2
fi
: "${NOTIFY_API_TOKEN:?Set NOTIFY_API_TOKEN first}"

# Delegate JSON encoding and HTTP handling to the stdlib Python client.
# Multiline output, quotation marks, and Unicode remain valid JSON.
task_examples_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$task_examples_dir/notify.py" "$1" "$2" \
    --severity "${3:-info}" --source "${4:-shell-script}"
