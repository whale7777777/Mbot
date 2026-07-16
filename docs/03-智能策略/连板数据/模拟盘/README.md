# 连板模拟盘

> 初始资金 **20,000** 元 | 策略：晋级概率 + 封板质量 + 主线簇

## 机器人逻辑

每日收盘后读取 `每日/YYYYMMDD.json` 连板池，自动执行：

1. **卖出**：持仓股未晋级（T 日 n 板 → T+1 未达 n+1 板）则次日卖出
2. **买入**：对候选股评分，选评分 ≥ 55 且满足风控的标的
3. **记录**：写入本目录操作文档

### 评分因子（与连板策略一致）

| 加分 | 减分 |
|------|------|
| 晋级概率高 | 炸板 ≥ 3 次 |
| 强封 / 零炸板 | 一字板（换手 < 0.5%） |
| 封单占比 ≥ 20% | 晋级率 < 28% |
| 主线簇 ≥ 2 只 | 连板数 > 4 |

### 风控

- 最多持仓 **2** 只
- 单票不超过总资产 **40%**
- 冰点期（池子 ≤ 6 只）总仓位 ≤ **50%**
- 修复期总仓位 ≤ **80%**

## 文件说明

| 文件 | 说明 |
|------|------|
| `paper_config.json` | 参数配置（资金、仓位、评分阈值） |
| `持仓.json` | 当前现金与持仓 |
| `成交记录.json` | 全量买卖流水 |
| `操作记录.md` | 汇总日志 |
| `每日/YYYYMMDD.md` | 当日盘面观察与操作 |

## 命令

```bash
# 每日运行（先确保当日连板数据已生成）
python scripts/lianban.py today
python scripts/lianban.py paper

# 或
python scripts/lianban_paper.py run
python scripts/lianban_paper.py run --date 20260716

# 历史回测模拟
python scripts/lianban_paper.py backfill --from 20260710 --to 20260716 --reset

# 查看状态 / 重置
python scripts/lianban_paper.py status
python scripts/lianban_paper.py reset
```

## 免责声明

模拟盘仅用于策略验证，不构成投资建议。实盘需自行判断风险。
