"""Learner: turns owner edits, rejections and error reports into durable lessons,
keeps per-source reliability scores, and suggests autopilot once the drafts are good enough.

Lessons are short rules stored per channel and role; every agent injects its top lessons
into its prompt. Lessons that stop being reinforced decay and retire.
"""

from __future__ import annotations

import json

from ..telegram import kb, plain
from ..util import now
from .base import Agent
from .verifier import load_sheet

ROLES = ("triage", "factcheck", "writer", "reviewer", "moderator")


class Learner(Agent):
    name = "learner"
    role = "learner"
    prompt = "learner"

    # ------------------------------------------------------------------ source reliability
    def on_published(self, p) -> None:
        sheet = load_sheet(p["facts"])
        if sheet.get("status") != "confirmed":
            return
        for sid in json.loads(p["sources"] or "[]"):
            self.db.source_state(sid)
            self.db.x("UPDATE sources_state SET alpha=alpha+1 WHERE source_id=?", (sid,))

    def penalize_sources(self, p, weight: float = 2.0) -> list[str]:
        ids = json.loads(p["sources"] or "[]")
        for sid in ids:
            self.db.source_state(sid)
            self.db.x("UPDATE sources_state SET beta=beta+? WHERE source_id=?", (weight, sid))
        return ids

    # ------------------------------------------------------------------ distillation
    def existing(self, channel_id: str) -> list:
        return self.db.all("SELECT id, role, text FROM lessons WHERE active=1 AND (channel_id=? OR channel_id IS NULL) "
                           "ORDER BY weight DESC LIMIT 60", (channel_id,))

    async def distill(self, channel_id: str, situation: str, material: str, origin: str) -> list[int]:
        ch = self.app.channel(channel_id)
        ex = self.existing(channel_id)
        user = (f"Ситуация: {situation}\n\n{material}\n\nУже существующие уроки (id: роль – текст):\n"
                + ("\n".join(f"{r['id']}: {r['role']} – {r['text']}" for r in ex) or "(нет)"))
        data, _ = await self.ask_json(user, ch, temperature=0.2)
        ids = []
        for rid in data.get("reinforce") or []:
            try:
                self.db.x("UPDATE lessons SET weight=MIN(weight+0.5, 5) WHERE id=?", (int(rid),))
            except (TypeError, ValueError):
                continue
        for les in (data.get("lessons") or [])[:3]:
            if not isinstance(les, dict):
                continue
            role, text = les.get("role"), str(les.get("text") or "").strip()
            if role in ROLES and 10 <= len(text) <= 400:
                ids.append(self.db.add_lesson(channel_id, role, text, origin))
        self.prune(channel_id)
        return ids

    async def learn_from_edit(self, post_id: int, draft: str, final: str) -> list[int]:
        p = self.db.post(post_id)
        if not p or plain(draft).strip() == plain(final).strip():
            return []
        material = (f"Факт-лист:\n{p['facts']}\n\nЧЕРНОВИК АГЕНТА:\n{draft}\n\nВЕРСИЯ ВЛАДЕЛЬЦА (эталон):\n{final}")
        return await self.distill(p["channel_id"], "владелец канала отредактировал пост перед публикацией", material,
                                  "owner_edit")

    async def learn_from_reject(self, post_id: int, reason: str) -> list[int]:
        p = self.db.post(post_id)
        if not p:
            return []
        material = (f"Категория: {p['category']}\nФакт-лист:\n{p['facts']}\n\nЧерновик:\n{p['final'] or p['draft']}\n\n"
                    f"Причина отказа со слов владельца: {reason or 'не указана'}")
        return await self.distill(p["channel_id"], "владелец отклонил пост", material, "owner_reject")

    async def learn_from_error(self, post_id: int, description: str) -> list[int]:
        p = self.db.post(post_id)
        if not p:
            return []
        penalized = self.penalize_sources(p)
        self.db.update_post(post_id, owner_note=f"ошибка: {description}"[:500])
        material = (f"Опубликованный пост:\n{p['final']}\n\nФакт-лист:\n{p['facts']}\n\n"
                    f"Источники, на которые опирались: {', '.join(penalized) or '–'}\n\nЧто было не так: {description}")
        return await self.distill(p["channel_id"], "в опубликованном посте нашли фактическую ошибку", material,
                                  "error_report")

    def prune(self, channel_id: str, keep_per_role: int = 40) -> None:
        for role in ROLES:
            rows = self.db.all("SELECT id FROM lessons WHERE active=1 AND channel_id=? AND role=? "
                               "ORDER BY weight DESC, created_at DESC", (channel_id, role))
            for r in rows[keep_per_role:]:
                self.db.x("UPDATE lessons SET active=0 WHERE id=?", (r["id"],))

    def decay(self) -> None:
        """Weekly: unreinforced lessons fade; owner rules never fade."""
        self.db.x("UPDATE lessons SET weight=weight*0.9 WHERE active=1 AND origin NOT IN ('owner_rule')")
        self.db.x("UPDATE lessons SET active=0 WHERE active=1 AND weight<0.3")

    # ------------------------------------------------------------------ autopilot graduation
    async def maybe_suggest_autopilot(self, channel_id: str) -> None:
        ch = self.app.channel(channel_id)
        if ch is None or self.app.autonomy(ch) != "approve" or self.db.get(f"autopilot_suggested:{ch.id}"):
            return
        need = int(self.cfg.learning.get("autopilot_after_decisions", 30))
        rate = float(self.cfg.learning.get("autopilot_min_clean_rate", 0.9))
        rows = self.db.all("SELECT owner_action FROM posts WHERE channel_id=? AND owner_action IS NOT NULL "
                           "ORDER BY updated_at DESC LIMIT ?", (ch.id, need))
        if len(rows) < need:
            return
        clean = sum(1 for r in rows if r["owner_action"] in ("approved", "approved_now")) / len(rows)
        if clean >= rate:
            self.db.set(f"autopilot_suggested:{ch.id}", now())
            await self.app.control_bot.send_message(
                self.cfg.owner_chat_id,
                f"🎓 <b>{ch.title}</b>: {clean:.0%} из последних {len(rows)} черновиков ты одобрил без правок.\n"
                f"Включить автопилот? Подтверждённые новости будут выходить сами, слухи и сомнительное – "
                f"по-прежнему через тебя.",
                reply_markup=kb([[("🚀 Включить автопилот", f"auto:{ch.id}"), ("Позже", "noop:0")]]), pace=False)

    def stats(self) -> dict:
        return {"lessons": self.db.scalar("SELECT COUNT(*) FROM lessons WHERE active=1", (), 0)}


def lesson_list(db, channel_id: str | None) -> list:
    if channel_id:
        return db.all("SELECT * FROM lessons WHERE active=1 AND (channel_id=? OR channel_id IS NULL) "
                      "ORDER BY role, weight DESC", (channel_id,))
    return db.all("SELECT * FROM lessons WHERE active=1 ORDER BY channel_id, role, weight DESC")

