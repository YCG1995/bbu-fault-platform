"""BBU 故障诊断平台。

零第三方依赖：仅使用 Python 3.11+ 标准库；PyYAML 以纯 Python 源码随包分发在 vendor/。
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VENDOR_DIR = BASE_DIR / "vendor"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

__version__ = "1.10.0"
