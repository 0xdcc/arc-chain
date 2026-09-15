#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/bin:/bin

fail_closed() {
  printf '[Fail-Closed] %s\n' "$*" >&2
  exit 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
BWRAP_BIN="${BWRAP_BIN:-/usr/bin/bwrap}"
[[ -x "$BWRAP_BIN" ]] || fail_closed "未找到 bubblewrap 二进制文件: $BWRAP_BIN"

if [[ -d "$SRC_DIR/venv" ]]; then
  REAL_VENV="$(readlink -f "$SRC_DIR/venv")"
elif [[ "$SRC_DIR" == /sandbox/src && -x /sandbox/venv/bin/python ]]; then
  REAL_VENV=/sandbox/venv
else
  fail_closed "未找到本地 Python 解释器依赖 venv"
fi
PYTHON_HOST="$REAL_VENV/bin/python"
[[ -x "$PYTHON_HOST" ]] || fail_closed "未找到 Python 解释器: $PYTHON_HOST"

if ! BASE_PREFIX="$(/usr/bin/env -i PATH=/usr/bin:/bin "$PYTHON_HOST" -I -S -c \
  'import sys; print(sys.base_prefix)')"; then
  fail_closed "Python interpreter discovery failed"
fi
[[ "$BASE_PREFIX" == /* && "$BASE_PREFIX" != / && "$BASE_PREFIX" != /root \
   && "$BASE_PREFIX" != /home && "$BASE_PREFIX" != /tmp ]] \
  || fail_closed "Invalid interpreter root"
REAL_PYTHON="$(readlink -f "$PYTHON_HOST")"
[[ ( "$REAL_PYTHON" == "$(readlink -f "$BASE_PREFIX")"/bin/python* \
     || "$REAL_PYTHON" == "$REAL_VENV"/bin/python* ) \
   && -d "$BASE_PREFIX/lib/python3.12" ]] \
  || fail_closed "Interpreter does not match discovered runtime root"
[[ ! -L "$SCRIPT_DIR/test_safety_stage.py" && ! -L "$SRC_DIR/scripts" ]] \
  || fail_closed "Source helper symlink rejected"

STAGE_DIR="$(mktemp -d /tmp/dex-safety-stage-XXXXXXXX)"
cleanup() {
  /usr/bin/env -i PATH=/usr/bin:/bin "$PYTHON_HOST" -I -S \
    "$SCRIPT_DIR/test_safety_stage.py" "$SRC_DIR" "$STAGE_DIR" --cleanup
}
trap cleanup EXIT
/usr/bin/env -i PATH=/usr/bin:/bin "$PYTHON_HOST" -I -S \
  "$SCRIPT_DIR/test_safety_stage.py" "$SRC_DIR" "$STAGE_DIR" \
  || fail_closed "Pure-source staging failed"

BWRAP_ARGS=(
  --die-with-parent
  --unshare-net
  --unshare-pid
  --unshare-ipc
  --unshare-uts
  --cap-drop ALL
  --clearenv
  --setenv PATH /sandbox/venv/bin:/usr/bin:/bin
  --setenv LANG C.UTF-8
  --setenv LC_ALL C.UTF-8
  --setenv TMPDIR /tmp
  --setenv PYTHONDONTWRITEBYTECODE 1
  --setenv DEX_ENGINE_SAFE_CONFIG 1
  --setenv PYTHONPATH /sandbox/src
  --setenv PYTEST_ADDOPTS "-o cache_dir=/tmp/.pytest_cache"
  --setenv PYTEST_DISABLE_PLUGIN_AUTOLOAD 1
  --setenv MYPY_CACHE_DIR /tmp/.mypy_cache
  --setenv RUFF_CACHE_DIR /tmp/.ruff_cache
  --tmpfs /
  --dev /dev
  --proc /proc
  --tmpfs /tmp
  --tmpfs /run
  --tmpfs /home
  --dir /sandbox
  --dir /sandbox/src
  --ro-bind /usr /usr
)
for dir_entry in bin sbin lib lib64; do
  if [[ -L "/$dir_entry" ]]; then
    BWRAP_ARGS+=(--symlink "$(readlink "/$dir_entry")" "/$dir_entry")
  elif [[ -d "/$dir_entry" ]]; then
    BWRAP_ARGS+=(--ro-bind "/$dir_entry" "/$dir_entry")
  fi
done
if [[ -d /etc/ssl/certs ]]; then
  BWRAP_ARGS+=(--ro-bind /etc/ssl/certs /etc/ssl/certs)
fi
if [[ "$BASE_PREFIX" != /usr && "$BASE_PREFIX" != /usr/* ]]; then
  BWRAP_ARGS+=(--ro-bind "$BASE_PREFIX" "$BASE_PREFIX")
fi
BWRAP_ARGS+=(--ro-bind "$REAL_VENV" /sandbox/venv)
BWRAP_ARGS+=(--ro-bind "$STAGE_DIR" /sandbox/src)
BWRAP_ARGS+=(--chdir /sandbox/src)

if [[ $# -gt 0 ]]; then
  CMD=("$@")
  case "${CMD[0]}" in
    "$PYTHON_HOST"|"$SRC_DIR/venv/bin/python"|./venv/bin/python|venv/bin/python)
      CMD[0]=/sandbox/venv/bin/python ;;
  esac
else
  CMD=(/sandbox/venv/bin/python -m pytest tests/ -v)
fi
set +e
"$BWRAP_BIN" "${BWRAP_ARGS[@]}" -- "${CMD[@]}"
EXIT_CODE=$?
set -e
if [[ $EXIT_CODE -ne 0 ]]; then
  printf '[Fail-Closed] 隔离命令退出 %s；不降级裸跑。namespace 拒绝时请在宿主复验。\n' "$EXIT_CODE" >&2
fi
exit "$EXIT_CODE"
