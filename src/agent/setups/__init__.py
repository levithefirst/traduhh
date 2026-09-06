from .base import Detection
from .breakout_retest import detect as detect_breakout_retest
from .sweep_reclaim import detect as detect_sweep_reclaim
from .trend_pullback import detect as detect_trend_pullback

SETUP_IDS = ("trend_pullback", "breakout_retest", "sweep_reclaim")

__all__ = ["Detection", "SETUP_IDS", "detect_trend_pullback", "detect_breakout_retest", "detect_sweep_reclaim"]
