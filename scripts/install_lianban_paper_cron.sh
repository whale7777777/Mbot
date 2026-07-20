#!/usr/bin/env bash
# 安装连板模拟盘竞价后飞书推送
#
# 推荐：GitHub Actions（.github/workflows/lianban-paper-notify.yml）
#   在仓库 Settings → Secrets 配置 FEISHU_APP_ID / FEISHU_APP_SECRET 后自动运行。
#
# 本机备选：系统 crontab（需持久化服务器，Cloud Agent 环境重启后会丢失）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-/home/ubuntu/miniconda3/bin/python}"
HERMES_SCRIPT="$HOME/.hermes/scripts/lianban_paper_notify.py"
CHAT_ID="oc_b082d116980b38638fd17cf4807be8d0"
JOB_NAME="连板模拟盘竞价后推送"

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
  hermes cron list 2>/dev/null | grep -q "连板模拟盘" && \
    hermes cron remove "连板模拟盘盘前推送" 2>/dev/null || true
  hermes cron list 2>/dev/null | grep -q "连板模拟盘" && \
    hermes cron remove "$JOB_NAME" 2>/dev/null || true
  # UTC 01:25 = 北京时间 09:25；任务内会等待竞价结束、拉数据、分析完再输出
  hermes cron add "25 1 * * 1-5" \
    --name "$JOB_NAME" \
    --script lianban_paper_notify.py \
    --no-agent \
    --deliver "feishu:${CHAT_ID}" \
    --workdir "$ROOT"
  echo "已创建 Hermes cron：工作日 09:25 触发 → 竞价后分析 → 飞书推送"
fi

if command -v crontab >/dev/null 2>&1; then
  MARKER="# lianban-paper-notify"
  CRON_LINE="25 1 * * 1-5 cd ${ROOT} && TZ=Asia/Shanghai ${PYTHON} scripts/lianban_paper_notify.py >> /tmp/lianban_paper_notify.log 2>&1"
  if crontab -l 2>/dev/null | grep -qF "$MARKER"; then
    crontab -l 2>/dev/null | grep -vF "$MARKER" | grep -vF "lianban_paper_notify.py" | crontab - || true
  fi
  (crontab -l 2>/dev/null || true; echo "$MARKER"; echo "$CRON_LINE") | crontab -
  echo "已安装系统 crontab（UTC 01:25 = 北京时间 09:25 触发）"
else
  echo "提示：系统无 crontab。"
  echo "推荐改用 GitHub Actions：.github/workflows/lianban-paper-notify.yml"
  echo "请在仓库 Settings → Secrets 添加 FEISHU_APP_ID / FEISHU_APP_SECRET"
fi
