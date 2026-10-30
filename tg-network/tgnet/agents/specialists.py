"""Specialist agents: declarative workers created from a spec (by you in YAML, or "hired" by HR).

A specialist runs on a schedule, reads only material that already passed fact-checking on its
channel, writes a format post (daily digest, weekly top transfers, match-day card, ...), and
hands it to the same reviewer and publisher as everything else. No code needed to add one.
"""

from __future__ import annotations

import json

from ..config import Channel
from ..telegram import sanitize
from ..util import dumps, local_dt, now, parse_hhmm
from .base import Agent
from .verifier import load_sheet

SPEC_KEYS = {"name", "channel_id", "title", "purpose", "schedule", "lookback_hours", "categories", "min_posts",
             "instructions", "score"}


def validate_spec(spec: dict, channels: dict) -> list[str]:
    errs = []
    if not isinstance(spec, dict):
        return ["spec не объект"]
    name = str(spec.get("name") or "")
    if not name or not name.replace("_", "").isalnum() or len(name) > 60:
        errs.append("name: латиница/цифры/_ до 60 символов")
    if spec.get("channel_id") not in channels:
        errs.append(f"channel_id '{spec.get('channel_id')}' не найден")
    sch = spec.get("schedule") or {}
    if "daily_at" in sch:
        try:
            h, m = parse_hhmm(str(sch["daily_at"]))
            assert 0 <= h < 24 and 0 <= m < 60
        except (ValueError, AssertionError):
            errs.append("schedule.daily_at должен быть HH:MM")
    elif "every_hours" in sch:
        try:
            assert 1 <= float(sch["every_hours"]) <= 168
        except (TypeError, ValueError, AssertionError):
            errs.append("schedule.every_hours от 1 до 168")
    else:
        errs.append("schedule: нужен daily_at или every_hours")
    if not str(spec.get("instructions") or "").strip():
        errs.append("instructions пустые")
    return errs


class Specialists(Agent):
    name = "specialist"
    role = "writer"
    prompt = "specialist"
    lesson_role = "writer"

    def seed_from_config(self) -> None:
        for ch in self.cfg.channels.values():
            for spec in ch.specialists:
                spec = {**spec, "channel_id": ch.id}
                errs = validate_spec(spec, self.cfg.channels)
                if errs:
                    self.log.warning("specialist %s invalid: %s", spec.get("name"), errs)
                    continue
                row = self.db.one("SELECT origin FROM agents WHERE name=?", (spec["name"],))
                if row is None:
                    self.db.x("INSERT INTO agents(name,channel_id,spec,status,origin,created_at) VALUES(?,?,?,?,?,?)",
                              (spec["name"], ch.id, dumps(spec), "active", "config", now()))
                elif row["origin"] == "config":
                    self.db.x("UPDATE agents SET spec=?, channel_id=? WHERE name=?", (dumps(spec), ch.id, spec["name"]))

    def due(self, spec: dict, last_run: float, t: float) -> bool:
        sch = spec.get("schedule") or {}
        if "daily_at" in sch:
            h, m = parse_hhmm(str(sch["daily_at"]))
            at = local_dt(t, self.cfg.timezone).replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
            return at <= t < at + 3 * 3600 and last_run < at
        return t - last_run >= float(sch.get("every_hours", 24)) * 3600

    async def tick(self) -> int:
        t = now()
        n = 0
        for row in self.db.all("SELECT * FROM agents WHERE status='active'"):
            spec = json.loads(row["spec"])
            ch = self.app.channel(row["channel_id"])
            if ch is None or not ch.enabled or self.app.paused(ch.id) or not self.due(spec, row["last_run"] or 0, t):
                continue
            self.db.x("UPDATE agents SET last_run=?, runs=runs+1 WHERE id=?", (t, row["id"]))
            try:
                if await self.run(spec, ch):
                    n += 1
            except Exception as e:  # noqa: BLE001
                self.log.exception("specialist %s failed", spec.get("name"))
                self.db.x("UPDATE agents SET notes=? WHERE id=?", (f"ошибка: {e}"[:300], row["id"]))
        return n

    def material(self, spec: dict, ch: Channel) -> list[tuple]:
        since = now() - float(spec.get("lookback_hours", 24)) * 3600
        cats = spec.get("categories") or []
        rows = self.db.all("SELECT * FROM posts WHERE channel_id=? AND status='published' AND kind='news' "
                           "AND published_at>? ORDER BY score DESC", (ch.id, since))
        out = []
        for r in rows:
            sheet = load_sheet(r["facts"])
            if sheet.get("status") not in ("confirmed", "reported"):
                continue
            if cats and r["category"] not in cats:
                continue
            out.append((r, sheet))
        return out

    async def run(self, spec: dict, ch: Channel) -> int | None:
        mat = self.material(spec, ch)
        if len(mat) < int(spec.get("min_posts", 3)):
            return None
        blocks = []
        facts, attribution, links, statuses = [], set(), [], set()
        for r, sheet in mat[:25]:
            statuses.add(sheet.get("status"))
            attribution.update(sheet.get("attribution") or [])
            links.extend(sheet.get("links") or [])
            for f in sheet.get("facts") or []:
                facts.append(f)
            blocks.append(dumps({"headline": sheet.get("headline"), "status": sheet.get("status"),
                                 "category": r["category"], "facts": [f.get("text") for f in sheet.get("facts") or []],
                                 "attribution": sheet.get("attribution")}))
        user = (f"Задача: {spec.get('title') or spec['name']}\n{spec.get('purpose') or ''}\n\n"
                f"Инструкции:\n{spec['instructions']}\n\nПроверенный материал (только это можно использовать):\n"
                + "\n".join(blocks))
        data, _ = await self.ask_json(user, ch, temperature=0.6)
        if data.get("skip"):
            return None
        text = str(data.get("text") or "").strip()
        if not text:
            return None
        sheet = {"status": "confirmed" if statuses <= {"confirmed"} else "reported", "headline": spec.get("title") or "",
                 "facts": facts, "attribution": sorted(attribution), "links": links[:10], "quotes": []}
        pid = self.db.new_post(ch.id, None, "drafted", kind="specialist", agent=spec["name"], category=spec["name"],
                               facts=sheet, draft=sanitize(text), score=float(spec.get("score", 70)), confidence=0.9)
        self.log.info("[%s] specialist %s drafted post %s", ch.id, spec["name"], pid)
        return pid
