import json
import os
import threading
import time
from pathlib import Path

from . import config, ctx

_lock = threading.Lock()
_FILE = config.DATA_DIR / "autopilot.json"

DEFAULTS = {
    "enabled": False,
    "auto_upload_youtube": True,
    "auto_upload_rumble": True,
    "dynamic_clips": True,        # "clip as many as are perfect"
    "channel_top_count": 5,
    "min_score": 65,
}


def get_settings() -> dict:
    with _lock:
        if _FILE.is_file():
            try:
                data = json.loads(_FILE.read_text(encoding="utf-8"))
                return {**DEFAULTS, **(data if isinstance(data, dict) else {})}
            except Exception:
                pass
        return dict(DEFAULTS)


def save_settings(data: dict) -> dict:
    with _lock:
        cur = get_settings()
        cur.update({k: v for k, v in data.items() if k in DEFAULTS})
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=2), encoding="utf-8")
        for i in range(10):
            try:
                tmp.replace(_FILE)
                break
            except OSError:
                if i == 9:
                    raise
                time.sleep(0.05)
        ctx.bump()
        return cur


def is_autopilot() -> bool:
    return bool(get_settings().get("enabled", False))
