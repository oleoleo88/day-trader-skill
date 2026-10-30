"""Writer: turns a verified fact sheet into a Telegram post in the channel's voice."""

from __future__ import annotations

import json

from ..llm import BudgetExceeded
from ..telegram import sanitize
from ..util import dumps, now
from .base import Agent
from .verifier import load_sheet

STATUS_RULES = {
    "confirmed": "Факт подтверждён официальным источником или несколькими независимыми СМИ. Пиши уверенно.",
    "reported": ("Факт сообщает одно крупное издание. ОБЯЗАТЕЛЬНО укажи источник в тексте: "
                 "«по данным <издание>», «сообщает <издание>». Не выдавай за официальный факт."),
    "rumor": ("Это НЕПОДТВЕРЖДЁННАЯ информация от инсайдера. Начни пост с пометки «Слух:» или «Инсайд:», "
              "назови источник и прямо скажи, что официального подтверждения нет."),
}


class Writer(Agent):
    name = "writer"
    role = "writer"
    prompt = "writer"
    lesson_role = "writer"

    async def tick(self, limit: int = 4) -> int:
        n = 0
        while n < limit:
            p = self.db.claim(("verified", "revise"), lease_s=600)
            if p is None:
                break
            n += 1
            try:
                await self.process(p)
            except BudgetExceeded as e:
                self.db.update_post(p["id"], lease_until=now() + 1800, last_error=str(e))
                break
            except Exception as e:  # noqa: BLE001
                self.log.exception("write post %s failed", p["id"])
                self.db.fail(p["id"], f"write: {e}")
        return n

    def examples(self, channel_id: str, k: int = 3) -> list[str]:
        rows = self.db.all(
            "SELECT p.final, MAX(m.views) AS v FROM posts p JOIN metrics m ON m.post_id=p.id "
            "WHERE p.channel_id=? AND p.published_at>? AND p.kind='news' GROUP BY p.id ORDER BY v DESC LIMIT ?",
            (channel_id, now() - 14 * 86400, k))
        return [r["final"] for r in rows if r["final"]]

    async def process(self, p) -> None:
        ch = self.app.channel(p["channel_id"])
        if ch is None:
            self.db.advance(p["id"], "rejected", owner_note="канал отключён")
            return
        sheet = load_sheet(p["facts"])
        status = sheet.get("status", "confirmed")
        parts = [f"Факт-лист (единственный источник правды):\n{dumps(sheet)}",
                 f"Категория: {p['category'] or 'other'}",
                 f"Статус проверки: {status}. {STATUS_RULES.get(status, '')}"
                 + (" Источники подают это осторожно (переговоры/интерес/возможно) – не усиливай формулировки."
                    if sheet.get("hedged") else ""),
                 f"Картинка к посту: {'есть' if p['image'] and ch.images else 'нет'} "
                 f"(лимит текста {'1024' if p['image'] and ch.images else '4096'} символов)."]
        if p["status"] == "revise" and p["draft"]:
            review = json.loads(p["review"] or "{}")
            parts.append("Твой прошлый вариант:\n" + p["draft"])
            parts.append("Замечания редактора – исправь ВСЕ:\n" + "\n".join(f"- {i}" for i in review.get("issues", [])))
        ex = self.examples(ch.id)
        if ex:
            parts.append("Посты этого канала, которые зашли аудитории лучше всего (ориентир по стилю, НЕ по фактам):\n\n"
                         + "\n---\n".join(ex))
        data, _ = await self.ask_json("\n\n".join(parts), ch, temperature=0.7)
        text = sanitize(str(data.get("text") or ""))
        if not text:
            raise ValueError("writer returned empty text")
        image = p["image"] if (ch.images and data.get("use_image", True)) else None
        self.db.advance(p["id"], "drafted", draft=text, image=image)
