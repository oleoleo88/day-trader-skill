"""Reviewer: hard deterministic guards + LLM line-edit against the fact sheet.

Deterministic guards catch what LLM reviewers miss: every number in the post must exist in
the fact sheet, every link must be a source link, attribution and rumor labels must be
present when the verdict needs them, Telegram length limits, forbidden phrases.
"""

from __future__ import annotations

import re

from ..config import Channel
from ..llm import BudgetExceeded
from ..telegram import plain, sanitize, visible_len
from ..util import domain, dumps, now, numbers_in
from .base import Agent
from .verifier import load_sheet

MAX_REVISIONS = 2
RUMOR_MARKERS = ("слух", "инсайд", "по данным", "по информации", "сообща", "утвержда", "неофициальн")
ATTRIBUTION_MARKERS = ("по данным", "по информации", "сообща", "пишет", "как стало известно", "источник")
DEFAULT_FORBIDDEN = (
    "100% проход", "100% ставка", "гарантированный выигрыш", "беспроигрышн", "верняк", "договорн",
    "железобетон", "заработок на ставках", "ставка без риска",
)
_HREF = re.compile(r'href="([^"]+)"')


class Reviewer(Agent):
    name = "reviewer"
    role = "reviewer"
    prompt = "reviewer"
    lesson_role = "reviewer"

    async def tick(self, limit: int = 4) -> int:
        n = 0
        while n < limit:
            p = self.db.claim(("drafted",), lease_s=600)
            if p is None:
                break
            n += 1
            try:
                await self.process(p)
            except BudgetExceeded as e:
                self.db.update_post(p["id"], lease_until=now() + 1800, last_error=str(e))
                break
            except Exception as e:  # noqa: BLE001
                self.log.exception("review post %s failed", p["id"])
                self.db.fail(p["id"], f"review: {e}")
        return n

    def guards(self, ch: Channel, text: str, sheet: dict, has_image: bool) -> list[str]:
        issues = []
        body = plain(text)
        if not body.strip():
            return ["пустой текст"]
        limit = 1024 if has_image else 4096
        total = visible_len(text) + (visible_len(ch.signature) + 2 if ch.signature else 0)
        if total > limit:
            issues.append(f"текст длиннее лимита: {total} > {limit} символов (с подписью) – сократи")
        allowed = numbers_in(dumps(sheet)) | numbers_in(plain(ch.signature))
        bad = sorted(numbers_in(body) - allowed)
        if bad:
            issues.append("числа, которых нет в факт-листе (убери или исправь): " + ", ".join(bad[:10]))
        ok_domains = {domain(u) for u in sheet.get("links", [])}
        if ch.username:
            ok_domains.add("t.me")
        for href in _HREF.findall(text):
            if domain(href) not in ok_domains:
                issues.append(f"ссылка не из источников: {href}")
        low = body.lower()
        status = sheet.get("status")
        names = [a.lower() for a in sheet.get("attribution", [])]
        if status in ("reported", "rumor"):
            if names and not any(n in low or n.split(" ")[0] in low for n in names):
                issues.append("не указан источник информации (" + ", ".join(sheet.get("attribution", [])) + ")")
            if status == "rumor" and not any(m in low for m in RUMOR_MARKERS):
                issues.append("нет пометки, что это слух/инсайд без официального подтверждения")
            if status == "reported" and not any(m in low for m in ATTRIBUTION_MARKERS) and not names:
                issues.append("нет атрибуции («по данным …»)")
        for ph in list(DEFAULT_FORBIDDEN) + ch.forbidden_phrases:
            if ph.lower() in low:
                issues.append(f"запрещённая формулировка: «{ph}»")
        return issues

    async def process(self, p) -> None:
        ch = self.app.channel(p["channel_id"])
        if ch is None:
            self.db.advance(p["id"], "rejected", owner_note="канал отключён")
            return
        sheet = load_sheet(p["facts"])
        text = p["draft"] or ""
        has_image = bool(p["image"])
        hard = self.guards(ch, text, sheet, has_image)
        user = (f"Факт-лист:\n{dumps(sheet)}\n\nЧерновик поста (Telegram HTML):\n{text}\n\n"
                f"Автоматические проверки нашли: {('; '.join(hard)) if hard else 'ничего'}")
        data, _ = await self.ask_json(user, ch, temperature=0.1)
        verdict = str(data.get("verdict") or "revise")
        issues = [str(i) for i in (data.get("issues") or [])]
        fixed = sanitize(str(data.get("fixed_text") or "")) if data.get("fixed_text") else ""
        if fixed and verdict in ("approve", "revise"):
            fixed_hard = self.guards(ch, fixed, sheet, has_image)
            if not fixed_hard:
                text, hard, verdict, issues = fixed, [], "approve", []
        if verdict == "reject":
            self.db.advance(p["id"], "rejected", review={"issues": issues, "verdict": verdict},
                            owner_note="редактор отклонил: " + "; ".join(issues)[:300])
            return
        if hard or verdict != "approve":
            all_issues = hard + issues
            if (p["revisions"] or 0) >= MAX_REVISIONS:
                self.db.advance(p["id"], "rejected", review={"issues": all_issues, "verdict": "revise"},
                                owner_note="не удалось довести до стандарта: " + "; ".join(all_issues)[:300])
                return
            self.db.advance(p["id"], "revise", review={"issues": all_issues, "verdict": verdict},
                            revisions=(p["revisions"] or 0) + 1)
            return
        conf = float(data.get("confidence") or 0.8)
        final_conf = min(conf, p["confidence"] if p["confidence"] is not None else 1.0)
        self.db.advance(p["id"], "ready", final=text, confidence=final_conf,
                        review={"issues": [], "verdict": "approve", "confidence": conf})
