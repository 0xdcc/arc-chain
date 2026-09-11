#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ $# -ne 0 ]]; then
  printf 'check.sh 必须执行全部层，不接受筛选参数。单层诊断请直接调用分层脚本。\n' >&2
  exit 2
fi
exec /usr/bin/env -i PATH=/usr/bin:/bin ./venv/bin/python -I -S \
  scripts/test_safety_layers.py
