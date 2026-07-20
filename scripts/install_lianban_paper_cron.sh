#!/usr/bin/env bash
# 安装连板模拟盘飞书推送（竞价后 + 收盘后）
#
# 推荐：GitHub Actions
#   - .github/workflows/lianban-paper-notify.yml       （09:25 竞价后）
#   - .github/workflows/lianban-paper-close-notify.yml  （15:00 收盘后）
#   在仓库 Settings → Secrets 配置 FEISHU_APP_ID / FEISHU_APP_SECRET 后自动运行。
#
# 本机备选：系统 crontab（需持久化服务器，Cloud Agent 环境重启后会丢失）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-/home/ubuntu/miniconda3/bin/python}"
CHAT_ID="oc_b082d116980b38638fd17cf4807be8d0"
AUCTION_JOB="连板模拟盘竞价后推送"
CLOSE_JOB="连板模拟盘收盘后推送"

install_hermes_script() {
  local name="$1"
  local script="$2"
  local hermes_path="$HOME/.hermes/scripts/${script}"
  mkdir -p "$(dirname "$hermes_path")"
  cat > "$hermes_path" <<EOF
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import subprocess, sys
ROOT = "$ROOT"
PY = "$PYTHON"
proc = subprocess.run(
    [PY, f"{ROOT}/scripts/${script}", "--print-only"],
    cwd=ROOT, capture_output=True, text=True,
)
if proc.returncode != 0:
    sys.stderr.write(proc.stderr)
    raise SystemExit(proc.returncode)
print(proc.stdout, end="")
EOF
  chmod +x "$hermes_path"
}

install_hermes_script "$AUCTION_JOB" "lianban_paper_notify.py"
install_hermes_script "$CLOSE_JOB" "lianban_paper_close_notify.py"

if command -v hermes >/dev/null 2>&1; then
  source "$HOME/.bashrc" 2>/dev/null || true
  hermes cron list 2>/dev/null | grep -q "连板模拟盘" && \
    hermes cron remove "连板模拟盘盘前推送" 2>/dev/null || true
  hermes cron list 2>/dev/null | grep -q "$AUCTION_JOB" && \
    hermes cron remove "$AUCTION_JOB" 2>/dev/null || true
  hermes cron list 2>/dev/null | grep -q "$CLOSE_JOB" && \
    hermes cron remove "$CLOSE_JOB" 2>/dev/null || true

  # UTC 01:25 = 北京时间 09:25
  hermes cron add "25 1 * * 1-5" \
    --name "$AUCTION_JOB" \
    --script lianban_paper_notify.py \
    --no-agent \
    --deliver "feishu:${CHAT_ID}" \
    --workdir "$ROOT"

  # UTC 07:00 = 北京时间 15:00
  hermes cron add "0 7 * * 1-5" \
    --name "$CLOSE_JOB" \
    --script lianban_paper_close_notify.py \
    --no-agent \
    --deliver "feishu:${CHAT_ID}" \
    --workdir "$ROOT"

  echo "已创建 Hermes cron：竞价后 + 收盘后飞书推送"
fi

if command -v crontab >/dev/null 2>&1; then
  AUCTION_MARKER="# lianban-paper-notify"
  CLOSE_MARKER="# lianban-paper-close-notify"
  AUCTION_LINE="25 1 * * 1-5 cd ${ROOT} && TZ=Asia/Shanghai ${PYTHON} scripts/lianban_paper_notify.py >> /tmp/lianban_paper_notify.log 2>&1"
  CLOSE_LINE="0 7 * * 1-5 cd ${ROOT} && TZ=Asia/Shanghai ${PYTHON} scripts/lianban_paper_close_notify.py >> /tmp/lianban_paper_close_notify.log 2>&1"

  crontab -l 2>/dev/null | \
    grep -vF "$AUCTION_MARKER" | grep -vF "lianban_paper_notify.py" | \
    grep -vF "$CLOSE_MARKER" | grep -vF "lianban_paper_close_notify.py" | \
    crontab - || true

  (crontab -l 2>/dev/null || true
   echo "$AUCTION_MARKER"
   echo "$AUCTION_LINE"
   echo "$CLOSE_MARKER"
   echo "$CLOSE_LINE") | crontab -

  echo "已安装系统 crontab：UTC 01:25（竞价后）+ UTC 07:00（收盘后）"
else
  echo "提示：系统无 crontab。"
  echo "推荐改用 GitHub Actions："
  echo "  - .github/workflows/lianban-paper-notify.yml"
  echo "  - .github/workflows/lianban-paper-close-notify.yml"
  echo "请在仓库 Settings → Secrets 添加 FEISHU_APP_ID / FEISHU_APP_SECRET"
fi
