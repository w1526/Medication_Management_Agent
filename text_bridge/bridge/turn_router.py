"""Reviewed, conservative mappings for the V1 medication replies."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class TurnDecision:
    action: str | None
    delay_minutes: int | None = None
    clarification: str | None = None


CLARIFY_TEXT = "没听清，您是吃了、晚点再吃、跳过，还是再说一遍？"


def normalize_text(text: str | None) -> str:
    return re.sub(r"[\s，。！!？?、,.；;：:\"“”‘’]+", "", str(text or "").lower())


def route_user_text(text: str | None) -> TurnDecision:
    """Map only reviewed short replies; never use substring confirmation matching."""
    compact = normalize_text(text)
    if not compact:
        return TurnDecision(None, clarification=CLARIFY_TEXT)

    # Safety-first negative/ambiguous expressions must be checked before any
    # positive token. In particular, ``不吃了`` must never become CONFIRM_TAKEN.
    if any(term in compact for term in ("没吃", "不吃", "都吃了", "不知道", "吃没吃")):
        return TurnDecision(None, clarification=CLARIFY_TEXT)

    if compact in {"吃了", "已经吃了", "吃过了", "吃完了", "我服了", "服了", "已服"}:
        return TurnDecision("CONFIRM_TAKEN")
    if compact in {"跳过", "跳过这次", "这次跳过"}:
        return TurnDecision("SKIP")
    if compact in {"再说一遍", "重复一下", "再讲一遍", "再播一遍"}:
        return TurnDecision("REPEAT")
    if compact in {"晚点", "晚点再吃", "等会儿", "等一下", "过会儿", "稍后"}:
        return TurnDecision("DELAY", delay_minutes=30)

    minute_match = re.fullmatch(r"(\d{1,3})分钟(?:后|以后)?", compact)
    if minute_match:
        minutes = int(minute_match.group(1))
        if minutes > 0:
            return TurnDecision("DELAY", delay_minutes=minutes)
    if compact in {"半小时后", "半个小时后"}:
        return TurnDecision("DELAY", delay_minutes=30)
    if compact in {"一小时后", "1小时后"}:
        return TurnDecision("DELAY", delay_minutes=60)
    return TurnDecision(None, clarification=CLARIFY_TEXT)
