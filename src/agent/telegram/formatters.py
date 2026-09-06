"""Pure Telegram message rendering. No DB, no network, no secrets.

Every function here takes plain dicts and returns plain text, so the whole
operator-facing surface is unit testable without a live Telegram bot.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

COMMANDS = (
    ("/help", "List commands + current mode"),
    ("/status", "mode, equity, day PnL, open paper count, last scan, integrity"),
    ("/health", "HL last success, DB ok, LLM last success, stale flags"),
    ("/regime", "Latest primary+secondary per asset per TF"),
    ("/ideas [n]", "Last n ideas (default 5) with decision"),
    ("/idea <uuid>", "Full idea summary"),
    ("/positions", "Open paper positions + MFE/MAE"),
    ("/stats [setup]", "Cell table: n, mean R, PF, max DD, CODE vs LLM split"),
    ("/journal [n]", "Compact journal"),
    ("/version", "strategy_version_id, prompt_version_id, git sha"),
    ("/halt [reason]", "Set halted; stop new TRADE_PAPER"),
    ("/resume", "Clear halt only if integrity.ok"),
    ("/mode", "Show mode; cannot switch to testnet in MVP"),
    ("/verbose on|off", "WAIT alerts on/off"),
)


def _num(value: Any, digits: int = 2, default: str = "n/a") -> str:
    if value is None:
        return default
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return default


def format_help(mode: str) -> str:
    lines = ["COMMANDS"]
    lines += [f"{name} — {desc}" for name, desc in COMMANDS]
    lines.append(f"MODE: {mode}")
    lines.append("Parameters, risk fraction and universe are code-level; no command can change them.")
    return "\n".join(lines)


def format_status(status: Mapping[str, Any]) -> str:
    return "\n".join([
        f"MODE: {status.get('mode', 'unknown')}",
        f"EQUITY: {_num(status.get('equity'))} USD",
        f"DAY PnL: {_num(status.get('day_pnl_usd'))} USD ({_num(status.get('day_pnl_pct', 0) and float(status.get('day_pnl_pct', 0)) * 100, 2)}%)",
        f"OPEN PAPER: {status.get('open_positions', 0)}",
        f"LAST SCAN: {status.get('last_scan_ts') or 'never'}",
        f"INTEGRITY: {'ok' if status.get('integrity_ok', True) else 'FAIL'}",
        f"HALT REASONS: {', '.join(status.get('halt_reasons') or []) or 'none'}",
    ])


def format_health(health: Mapping[str, Any]) -> str:
    return "\n".join([
        f"DB: {'ok' if health.get('db_ok') else 'FAIL'}",
        f"HL LAST SUCCESS: {health.get('hl_last_success') or 'never'}",
        f"HL DOWN: {'yes' if health.get('hl_down') else 'no'}",
        f"LLM LAST SUCCESS: {health.get('llm_last_success') or 'never'}",
        f"STALE FLAGS: {', '.join(health.get('flags') or []) or 'none'}",
    ])


def format_regime(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "No regime snapshots yet."
    lines = ["REGIME"]
    for row in rows:
        secondary = ", ".join(row.get("secondary") or []) or "-"
        lines.append(
            f"{row.get('asset')} {row.get('timeframe')}: {row.get('label')} [{secondary}] "
            f"conf={_num(row.get('confidence'))} @ {row.get('open_time')}"
        )
    return "\n".join(lines)


def format_ideas(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "No ideas recorded yet."
    lines = ["IDEAS"]
    for row in rows:
        lines.append(
            f"{row.get('created_at')} | {row.get('asset')} {row.get('timeframe')} | "
            f"{row.get('setup_id')} {row.get('direction')} | {row.get('decision')} | {row.get('id')}"
        )
    return "\n".join(lines)


def format_idea(idea: Mapping[str, Any] | None) -> str:
    if not idea:
        return "Idea not found."
    geometry = idea.get("geometry") or {}
    costs = idea.get("costs") or {}
    gates = idea.get("gates") or {}
    hard = gates.get("hard") or {}
    failed = sorted(name for name, passed in hard.items() if not passed)
    llm = idea.get("llm_review") or {}
    targets = geometry.get("targets") or []
    lines = [
        f"IDEA {idea.get('id')}",
        f"{idea.get('asset')} {idea.get('timeframe')} | {idea.get('setup_id')} | {idea.get('direction')}",
        f"DECISION: {idea.get('decision')} ({', '.join(idea.get('decision_reason') or []) or 'no reasons'})",
        f"ENTRY / STOP / T1: {_num(geometry.get('entry'), 4)} / {_num(geometry.get('stop'), 4)} / "
        f"{_num(targets[0] if targets else None, 4)}",
        f"PLANNED R AFTER COSTS: {_num(costs.get('planned_r_after_costs'))}",
        f"HARD GATES FAILED: {', '.join(failed) or 'none'}",
        f"CONFIDENCE: {_num(idea.get('confidence'))}",
        f"SV: {idea.get('strategy_version_id')} | PV: {idea.get('prompt_version_id') or 'n/a'}",
    ]
    if llm:
        lines.append(
            f"LLM: {llm.get('recommendation', 'n/a')} agree={llm.get('agree_with_code', 'n/a')} "
            f"conf={_num(llm.get('confidence'))}"
        )
        if llm.get("thesis"):
            lines.append(f"THESIS: {llm['thesis']}")
    outcome = idea.get("outcome")
    if outcome:
        lines.append(
            f"OUTCOME: {outcome.get('exit_reason')} realized_R={_num(outcome.get('realized_r'))} "
            f"MFE={_num(outcome.get('mfe_r'))} MAE={_num(outcome.get('mae_r'))}"
        )
    return "\n".join(lines)


def format_positions(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "No open paper positions."
    lines = ["OPEN PAPER POSITIONS"]
    for row in rows:
        lines.append(
            f"{row.get('asset')} {row.get('tf')} {row.get('direction')} | entry {_num(row.get('entry'), 4)} "
            f"stop {_num(row.get('stop'), 4)} | MFE {_num(row.get('mfe_r'))}R MAE {_num(row.get('mae_r'))}R "
            f"| bars {row.get('bars_held', 0)} | {row.get('id')}"
        )
    return "\n".join(lines)


def format_journal(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "Journal is empty."
    lines = ["JOURNAL"]
    for row in rows:
        realized = row.get("realized_r")
        tail = f"{_num(realized)}R {row.get('outcome_class') or ''}".strip() if realized is not None else "open/no position"
        lines.append(
            f"{row.get('created_at')} | {row.get('asset')} {row.get('timeframe')} {row.get('setup_id')} "
            f"| {row.get('decision')} | {tail}"
        )
    return "\n".join(lines)


def format_version(version: Mapping[str, Any]) -> str:
    return "\n".join([
        f"STRATEGY VERSION: {version.get('strategy_version_id')}",
        f"PROMPT VERSION: {version.get('prompt_version_id') or 'n/a'}",
        f"GIT SHA: {version.get('code_git_sha') or 'unknown'}",
        f"MODE: {version.get('mode')}",
    ])


def format_mode(mode: str) -> str:
    return "\n".join([
        f"MODE: {mode}",
        "Allowed modes in MVP: paper, halted.",
        "testnet_exec and mainnet_exec are not implemented and cannot be selected.",
    ])
