"""pytest 全局配置。"""

from __future__ import annotations

import sys
from pathlib import Path

# 让 `pytest` 从 backend/ 跑时，import app.* 直接可用
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
