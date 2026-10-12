"""Shared live/replay risk accounting; all timestamps are exchange milliseconds."""
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

D = lambda v: Decimal(str(v))


def refresh(state, now, equity):
    day = datetime.fromtimestamp(now / 1000, ZoneInfo("Asia/Seoul")).date().isoformat()
    if state.get("day") != day:
        state.update(day=day, entries=0, daily_equity=str(equity))
    state["peak"] = str(max(D(state.get("peak", equity)), D(equity)))
    return state


def budget(state, settings, equity):
    equity = D(equity)
    daily = D(settings.capital) * D(settings.daily_loss_percent) / 100 - max(D(0), D(state["daily_equity"]) - equity)
    total = D(settings.capital) * D(settings.total_loss_percent) / 100 - max(D(0), D(settings.capital) - equity)
    peak = D(state["peak"])
    drawdown = peak * D(settings.peak_drawdown_percent) / 100 - (peak - equity)
    return max(D(0), min(daily, total, drawdown))


def entry_gate(state, settings, now, equity):
    if budget(state, settings, equity) <= 0:
        return "일일·누적·고점 대비 손실 한도"
    if state.get("entries", 0) >= settings.max_entries_per_day:
        return "하루 신규 진입 횟수 한도"
    if now < state.get("blocked_until", 0):
        return "청산 후 재진입 대기 / 연속 손실 휴식"
    return None


def entered(state):
    state["entries"] = state.get("entries", 0) + 1


def exited(state, settings, now, net):
    streak = state.get("loss_streak", 0) + 1 if D(net) < 0 else 0
    pause = settings.cooldown_minutes
    if streak >= settings.loss_streak_limit:
        pause = max(pause, settings.loss_pause_minutes)
        streak = 0
    state.update(loss_streak=streak, blocked_until=max(state.get("blocked_until", 0), now + pause * 60_000))
