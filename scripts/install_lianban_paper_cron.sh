#!/usr/bin/env bash
# 安装连板模拟盘盘前飞书推送定时任务（交易日 9:25 北京时间）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-/home/ubuntu/miniconda3/bin/python}"
HERMES_SCRIPT="$HOME/.hermes/scripts/lianban_paper_notify.py"
CHAT_ID="oc_b082d116980b38638fd17cf4807be8d0"

mkdir -p "$(dirname "$HERMES_SCRIPT")"
cat > "$HERMES_SCRIPT" <<EOF
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import subprocess, sys
ROOT = "$ROOT"
PY = "$PYTHON"
proc = subprocess.run(
    [PY, f"{ROOT}/scripts/lianban_paper_notify.py", "--print-only"],
    cwd=ROOT, capture_output=True, text=True,
)
if proc.returncode != 0:
    sys.stderr.write(proc.stderr)
    raise SystemExit(proc.returncode)
print(proc.stdout, end="")
EOF
chmod +x "$HERMES_SCRIPT"

if command -v hermes >/dev/null 2>&1; then
  source "$HOME/.bashrc" 2>/dev/null || true
  hermes cron list 2>/dev/null | grep -q "连板模拟盘盘前推送" && \
    hermes cron remove "连板模拟盘盘前推送" 2>/dev/null || true
  # 云主机 UTC：北京时间 09:25 = UTC 01:25
  hermes cron add "25 1 * * 1-5" \
    --name "连板模拟盘盘前推送" \
    --script lianban_paper_notify.py \
    --no-agent \
    --deliver "feishu:${CHAT_ID}" \
    --workdir "$ROOT"
  echo "已创建 Hermes cron：工作日 09:25（北京时间）→ 飞书群"
fi

if command -v crontab >/dev/null 2>&1; then
  MARKER="# lianban-paper-notify"
  CRON_LINE="25 1 * * 1-5 cd ${ROOT} && TZ=Asia/Shanghai ${PYTHON} scripts/lianban_paper_notify.py >> /tmp/lianban_paper_notify.log 2>&1"
  if crontab -l 2>/dev/null | grep -qF "$MARKER"; then
    crontab -l 2>/dev/null | grep -vF "$MARKER" | grep -vF "lianban_paper_notify.py" | crontab - || true
  fi
  (crontab -l 2>/dev/null || true; echo "$MARKER"; echo "$CRON_LINE") | crontab -
  echo "已安装系统 crontab（UTC 01:25 = 北京时间 09:25）"
else
  echo "提示：系统无 crontab，已依赖 Hermes cron（若已安装）"
fi
