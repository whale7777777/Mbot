# -*- coding: utf-8 -*-
"""已合并至 lianban.py，保留本文件兼容旧命令。"""

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    script = Path(__file__).resolve().parent / "lianban.py"
    subprocess.run([sys.executable, str(script), "all"], cwd=script.parent.parent, check=True)
