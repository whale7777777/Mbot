import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lianban_recalibrate_15d as recalibrate


class LianbanRecalibrateTest(unittest.TestCase):
    def test_local_row_factors_uses_daily_json_fields(self):
        factors = recalibrate.local_row_factors(
            {
                "首次封板时间": "101500",
                "炸板次数": 0,
                "封单占比": "15.4%",
                "弱转强": "是",
            }
        )

        self.assertEqual(
            factors,
            {
                "early_seal": True,
                "zero_zhaban": True,
                "heavy_seal": False,
                "big_order_seal": True,
                "weak_to_strong": True,
            },
        )

    def test_build_verification_records_reads_adjacent_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily_dir = Path(tmp)
            first = {
                "trade_date": "20260105",
                "stocks": [
                    {
                        "代码": "123",
                        "名称": "晋级股",
                        "连板数": 2,
                        "晋级概率": 0.3,
                        "首次封板时间": "093000",
                        "炸板次数": 0,
                        "封单占比": "25%",
                        "弱转强": "否",
                    },
                    {
                        "代码": "456",
                        "名称": "断板股",
                        "连板数": 2,
                        "晋级概率_pct": 20,
                    },
                    {"代码": "789", "名称": "首板股", "连板数": 1},
                ],
            }
            second = {
                "trade_date": "20260106",
                "stocks": [
                    {"代码": "000123", "名称": "晋级股", "连板数": 3},
                    {"代码": "000456", "名称": "断板股", "连板数": 2},
                ],
            }
            (daily_dir / "20260105.json").write_text(
                json.dumps(first, ensure_ascii=False), encoding="utf-8"
            )
            (daily_dir / "20260106.json").write_text(
                json.dumps(second, ensure_ascii=False), encoding="utf-8"
            )

            with patch.object(recalibrate, "DAILY_DIR", daily_dir):
                records = recalibrate.build_verification_records(
                    ["20260105", "20260106"], min_boards=2
                )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["code"], "000123")
        self.assertEqual(records[0]["actual"], 1)
        self.assertEqual(records[1]["actual"], 0)
        self.assertAlmostEqual(records[1]["pred"], 0.2)

    def test_walk_forward_starts_after_warmup_pairs(self):
        records = []
        for idx in range(7):
            records.append(
                {
                    "T": f"202601{idx + 1:02d}",
                    "T1": f"202601{idx + 2:02d}",
                    "code": f"{idx:06d}",
                    "name": "样本",
                    "boards": 2,
                    "pred": 0.25,
                    "actual": idx % 2,
                    "factors": {
                        key: False for key in recalibrate.FACTOR_KEYS
                    },
                }
            )

        evaluated = recalibrate.walk_forward_predictions(
            records, min_boards=2, alpha=1.0, shrink=0.15
        )

        self.assertEqual([row["T"] for row in evaluated], ["20260106", "20260107"])
        self.assertTrue(all(0.02 <= row["pred_rolling"] <= 0.92 for row in evaluated))


if __name__ == "__main__":
    unittest.main()
