from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
LOCAL_EDGE_PATH = os.getenv("MPAU_EDGE_PATH", "")
LOCAL_EDGE_HEADLESS = os.getenv("MPAU_HEADLESS", "true").strip().lower() not in {"0", "false", "no", "off"}
DEBUG_MODE = os.getenv("MPAU_DEBUG", "true").strip().lower() not in {"0", "false", "no", "off"}

# Upstream 小红书/抖音 uploaders are Chrome-oriented. Keep aliases here so
# their modules stay decoupled from the existing Edge-first Tmall/JD code.
LOCAL_CHROME_PATH = os.getenv("MPAU_CHROME_PATH", LOCAL_EDGE_PATH)
LOCAL_CHROME_HEADLESS = LOCAL_EDGE_HEADLESS
