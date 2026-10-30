"""Editor-in-chief: decides which fresh stories are worth a post on each channel."""

from __future__ import annotations

import json

from ..config import Channel
from ..util import clip, day_start, dumps, now
from .base import Agent

CATEGORIES = ("breaking", "transfer", "result", "match_preview", "injury", "official", "rumor",
              "tournament", "roster", "patch", "analysis", "other")


class Editor(Agent):
    name = "editor"
    role = "triage"
    prompt = "editor"
    lesson_role = "triage"

    async def tick(self) -> int:
        made = 0
        for ch in self.app.active_channels():
            if not self.app.paused(ch.id):
                made += await self.triage_channel(ch)
        return made

    def candidates(self, ch: Channel, t: float) -> list:
        qs = ",".join("?" * len(ch.packs))
        return self.db.all(
            f"SELECT s.*, tr.item_count AS tr_items FROM stories s "
            f"LEFT JOIN triage tr ON tr.channel_id=? AND tr.story_id=s.id "
            f"WHERE s.pack IN ({qs}) AND s.first_seen>? AND s.last_seen>? AND "
            f"(tr.story_id IS NULL OR (tr.decision='skip' AND s.item_count>=tr.item_count+2 AND s.first_seen>?)) "
            f"ORDER BY s.best_tier, s.last_seen DESC LIMIT 30",
            (ch.id, *ch.packs, t - 12 * 3600, t - 6 * 3600, t - 6 * 3600),
        )

    def keyword_ok(self, ch: Channel, text: str) -> bool:
        low = text.lower()
        if ch.exclude_keywords and any(k.lower() in low for k in ch.exclude_keywords):
            return False
        if ch.include_keywords and not any(k.lower() in low for k in ch.include_keywords):
            return False
        return True

    def recent_headlines(self, ch: Channel, t: float) -> list[str]:
        rows = self.db.all(
            "SELECT p.facts, s.title FROM posts p LEFT JOIN stories s ON s.id=p.story_id "
            "WHERE p.channel_id=? AND p.created_at>? AND p.status NOT IN ('rejected','failed','expired') "
            "ORDER BY p.created_at DESC LIMIT 40", (ch.id, t - 48 * 3600))
        out = []
        for r in rows:
            head = None
            if r["facts"]:
                try:
                    head = json.loads(r["facts"]).get("headline")
                except ValueError:
                    head = None
            out.append(head or r["title"] or "")
        return [h for h in out if h]

    async def triage_channel(self, ch: Channel) -> int:
        t = now()
        rows = self.candidates(ch, t)
        if not rows:
            return 0
        lines, by_id = [], {}
        for s in rows:
            items = self.db.all("SELECT source_id,title FROM items WHERE story_id=? ORDER BY published_at LIMIT 8",
                                (s["id"],))
            blob = s["title"] + " " + " ".join(i["title"] for i in items)
            if not self.keyword_ok(ch, blob):
                self.save(ch, s, 0, "other", "filtered", "keyword filter")
                continue
            srcs = []
            for i in items:
                src = self.app.source(i["source_id"])
                if src and src.name not in [x[0] for x in srcs]:
                    srcs.append((src.name, self.app.effective_tier(src)))
            age = int((t - s["first_seen"]) / 60)
            alt = [i["title"] for i in items if i["title"] != s["title"]][:2]
            lines.append(dumps({"id": s["id"], "title": s["title"], "also": alt,
                                "sources": [f"{n} (T{tier})" for n, tier in srcs],
                                "items": s["item_count"], "age_min": age}))
            by_id[s["id"]] = s
        if not lines:
            return 0
        recent = self.recent_headlines(ch, t)
        published_today = self.db.scalar("SELECT COUNT(*) FROM posts WHERE channel_id=? AND published_at>=?",
                                         (ch.id, day_start(t, self.cfg.timezone)), 0)
        user = ("Кандидаты (JSON по строке):\n" + "\n".join(lines)
                + "\n\nУже опубликовано или в работе за 48 часов:\n"
                + ("\n".join(f"- {clip(h, 140)}" for h in recent) or "- (пусто)")
                + f"\n\nОпубликовано сегодня: {published_today} из лимита {ch.schedule.max_posts_per_day}.")
        data, _ = await self.ask_json(user, ch, categories=", ".join(CATEGORIES))
        picked = []
        for d in data.get("stories") or data.get("items") or []:
            try:
                sid = int(d.get("id"))
            except (TypeError, ValueError):
                continue
            s = by_id.get(sid)
            if s is None:
                continue
            score = max(0.0, min(100.0, float(d.get("score") or 0)))
            cat = d.get("category") if d.get("category") in CATEGORIES else "other"
            dup = bool(d.get("duplicate_of_recent"))
            threshold = ch.triage_threshold
            if published_today >= ch.schedule.max_posts_per_day:
                threshold = max(threshold, ch.schedule.breaking_score)
            take = score >= threshold and not dup
            self.save(ch, s, score, cat, "take" if take else "skip", clip(str(d.get("reason") or ""), 300))
            if take and not self.db.one("SELECT 1 FROM posts WHERE channel_id=? AND story_id=?", (ch.id, sid)):
                picked.append((score, s, cat))
        picked.sort(key=lambda x: -x[0])
        for score, s, cat in picked[: ch.max_selected_per_cycle]:
            self.db.new_post(ch.id, s["id"], "selected", score=score, category=cat, agent=self.name)
            self.log.info("[%s] selected story %s (%.0f, %s): %s", ch.id, s["id"], score, cat, clip(s["title"], 90))
        return len(picked[: ch.max_selected_per_cycle])

    def save(self, ch: Channel, s, score: float, cat: str, decision: str, reason: str):
        self.db.x(
            "INSERT INTO triage(channel_id,story_id,score,category,decision,reason,item_count,ts) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(channel_id,story_id) DO UPDATE SET score=excluded.score, category=excluded.category, "
            "decision=excluded.decision, reason=excluded.reason, item_count=excluded.item_count, ts=excluded.ts",
            (ch.id, s["id"], score, cat, decision, reason, s["item_count"], now()))
