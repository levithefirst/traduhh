from __future__ import annotations

import json
from datetime import datetime, timezone

STRATEGY_VERSION_ID = "sv_step5_reconstructed_v1"


def strategy_params(settings) -> dict:
    return {
        "version": "1",
        "setups": ["trend_pullback", "breakout_retest", "sweep_reclaim"],
        "risk_fraction": str(settings.risk_fraction),
        "min_r_after_costs": str(settings.min_r_after_costs),
        "ta_lib": "pandas_explicit_step4",
        "detectors": {"trend_pullback": {"touch_atr": 0.25, "target_r": 1.8}, "breakout_retest": {"lookback": 20, "break_atr": 0.10, "retest_atr": 0.20, "window": 12, "extension_atr": 2.0}, "sweep_reclaim": {"sweep_atr": 0.10, "reclaim_bars": 3, "stop_atr": 0.15, "target_r": 1.5}},
    }


def ensure_strategy_version(conn, settings, *, code_git_sha: str | None = None) -> str:
    params = strategy_params(settings)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy_version(id,created_at,params,code_git_sha,notes,frozen)
                           VALUES (%s,%s,%s::jsonb,%s,%s,%s)
                           ON CONFLICT (id) DO UPDATE SET params=EXCLUDED.params,code_git_sha=EXCLUDED.code_git_sha,
                           notes=EXCLUDED.notes,frozen=EXCLUDED.frozen""",
                        (STRATEGY_VERSION_ID, datetime.now(timezone.utc), json.dumps(params,separators=(",",":")), code_git_sha,
                         "Step 5 reconstructed from frozen specification; not recovered historical commit", True))
    return STRATEGY_VERSION_ID
