#!/usr/bin/env bash
# Foreground Dashboard. Ctrl+C stops this process.
# Usage: ./start-dashboard.sh --host 192.168.99.201
set -euo pipefail
cd "$(dirname "$0")"

HOST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host|-host)
      HOST="${2:-}"
      shift 2
      ;;
    *)
      echo "未知参数: $1" >&2
      echo "用法: $0 --host <IP>" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$HOST" ]]; then
  echo "用法: $0 --host <IP>" >&2
  exit 2
fi
if [[ "$HOST" == "0.0.0.0" || "$HOST" == "::" ]]; then
  echo "不能绑定通配地址，请传入具体 IP，例如 192.168.99.201" >&2
  exit 2
fi

if [[ -f "$HOME/.openrobotsoul-go2.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$HOME/.openrobotsoul-go2.env"
  set +a
fi

# 方案 A：唤醒后按 KWS 时间戳裁剪 ASR 音频（默认关闭，这里显式打开）
# 已在环境或 ~/.openrobotsoul-go2.env 中设置时保留原值。
export ROBOTSOUL_VOICE_WAKE_CROP="${ROBOTSOUL_VOICE_WAKE_CROP:-1}"
export ROBOTSOUL_VOICE_WAKE_GUARD_MS="${ROBOTSOUL_VOICE_WAKE_GUARD_MS:-80}"
export ROBOTSOUL_VOICE_COMMAND_PREROLL_MS="${ROBOTSOUL_VOICE_COMMAND_PREROLL_MS:-120}"

exec .venv/bin/python -m robotsoul dashboard \
  --host "$HOST" \
  --port 8765 \
  --tls-cert certs/dashboard.crt \
  --tls-key certs/dashboard.key
