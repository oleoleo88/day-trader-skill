"""HR / supervisor: looks at how the network performs and proposes structural changes –
hire a new specialist agent, retire a weak one, add a source, tune a channel setting.

Every proposal is validated and sent to the owner with ✅/❌ buttons (or auto-applied for safe
kinds when hr_auto_approve is on). Applying needs no code change and no restart.
"""

from __future__ import annotations

import html
import json

from .. import sources as S
from ..config import Source
from ..telegram import kb
from ..util import dumps, now
from .base import Agent
from .specialists import SPEC_KEYS, validate_spec

TUNABLE = {  # key -> (min, max, type)
    "triage_threshold": (40, 95, int),
    "max_selected_per_cycle": (1, 10, int),
    "auto_confidence": (0.6, 0.99, float),
    "schedule.min_interval_min": (5, 180, int),
    "schedule.max_posts_per_day": (3, 60, int),
    "schedule.max_age_hours": (1, 24, float),
}
SAFE_AUTO = ("tune_channel", "create_agent", "retire_agent")


class HR(Agent):
    name = "hr"
    role = "hr"
    prompt = "hr"

    def snapshot(self) -> dict:
        t = now()
        since = t - 7 * 86400
        out = {"channels": {}, "sources": [], "agents": []}
        for ch in self.app.active_channels():
            tri = self.db.all("SELECT category, decision, COUNT(*) c, AVG(score) s FROM triage WHERE channel_id=? AND ts>? "
                              "GROUP BY category, decision", (ch.id, since))
            perf = self.db.all(
                "SELECT p.category, COUNT(*) n, AVG(v) avg_views FROM (SELECT p.id, p.category, MAX(m.views) v FROM posts p "
                "LEFT JOIN metrics m ON m.post_id=p.id WHERE p.channel_id=? AND p.published_at>? GROUP BY p.id) p "
                "GROUP BY p.category", (ch.id, since))
            rej = self.db.all("SELECT owner_note, COUNT(*) c FROM posts WHERE channel_id=? AND status IN "
                              "('rejected','expired','failed') AND updated_at>? GROUP BY owner_note ORDER BY c DESC LIMIT 8",
                              (ch.id, since))
            out["channels"][ch.id] = {
                "title": ch.title, "focus": ch.focus, "packs": ch.packs, "autonomy": self.app.autonomy(ch),
                "settings": {k: self.app.get_setting(ch, k) for k in TUNABLE},
                "triage": [dict(r) for r in tri], "published_by_category": [dict(r) for r in perf],
                "drop_reasons": [dict(r) for r in rej],
            }
        for s in self.app.all_sources():
            st = self.db.source_state(s.id)
            out["sources"].append({"id": s.id, "pack": s.pack, "tier": s.tier, "failures": st["failures"],
                                   "reliability": round(st["alpha"] / (st["alpha"] + st["beta"]), 2),
                                   "items": st["items_total"]})
        for a in self.db.all("SELECT * FROM agents WHERE status='active'"):
            views = self.db.scalar("SELECT AVG(v) FROM (SELECT MAX(m.views) v FROM posts p JOIN metrics m ON m.post_id=p.id "
                                   "WHERE p.agent=? AND p.published_at>? GROUP BY p.id)", (a["name"], since))
            out["agents"].append({"name": a["name"], "channel": a["channel_id"], "runs": a["runs"],
                                  "avg_views": views, "spec": json.loads(a["spec"])})
        return out

    async def run(self) -> int:
        data, _ = await self.ask_json("Состояние сети за 7 дней:\n" + dumps(self.snapshot()), None, temperature=0.4)
        n = 0
        for prop in (data.get("proposals") or [])[:4]:
            if not isinstance(prop, dict):
                continue
            err = self.validate(prop)
            if err:
                self.log.info("HR proposal dropped (%s): %s", err, prop)
                continue
            pid = self.db.x("INSERT INTO proposals(kind,payload,rationale,status,created_at) VALUES(?,?,?,?,?)",
                            (prop["kind"], dumps(prop), str(prop.get("rationale") or "")[:1000], "pending", now())).lastrowid
            n += 1
            if self.cfg.hr_auto_approve and prop["kind"] in SAFE_AUTO:
                await self.apply(pid)
            else:
                await self.ask_owner(pid, prop)
        return n

    def validate(self, prop: dict) -> str | None:
        kind = prop.get("kind")
        if kind == "create_agent":
            spec = prop.get("spec") or {}
            errs = validate_spec(spec, self.cfg.channels)
            if self.db.one("SELECT 1 FROM agents WHERE name=?", (spec.get("name"),)):
                errs.append("агент с таким именем уже есть")
            return "; ".join(errs) or None
        if kind == "retire_agent":
            return None if self.db.one("SELECT 1 FROM agents WHERE name=? AND status='active'", (prop.get("name"),)) \
                else "нет такого активного агента"
        if kind == "add_source":
            s = prop.get("source") or {}
            if s.get("kind") not in ("rss", "telegram", "steam") or not s.get("url") or not s.get("id"):
                return "источник: нужны id, kind, url"
            if s.get("pack") not in self.cfg.packs:
                return "неизвестный pack"
            if self.app.source(s["id"]):
                return "источник уже есть"
            return None
        if kind == "tune_channel":
            if prop.get("channel_id") not in self.cfg.channels or prop.get("key") not in TUNABLE:
                return "неизвестный канал или параметр"
            lo, hi, typ = TUNABLE[prop["key"]]
            try:
                v = typ(prop.get("value"))
            except (TypeError, ValueError):
                return "значение не число"
            return None if lo <= v <= hi else f"значение вне диапазона {lo}..{hi}"
        return "неизвестный тип предложения"

    def describe(self, prop: dict) -> str:
        k = prop["kind"]
        if k == "create_agent":
            s = prop["spec"]
            return (f"🧑‍💼 <b>Нанять агента</b> «{html.escape(s.get('title') or s['name'])}» для {s['channel_id']}\n"
                    f"Расписание: {html.escape(dumps(s.get('schedule')))}\n<i>{html.escape(s.get('instructions', '')[:600])}</i>")
        if k == "retire_agent":
            return f"👋 <b>Уволить агента</b> {html.escape(prop['name'])}"
        if k == "add_source":
            s = prop["source"]
            return (f"📡 <b>Добавить источник</b> {html.escape(s.get('name') or s['id'])} (T{s.get('tier', 3)}, {s['kind']})\n"
                    f"{html.escape(s['url'])}")
        return (f"⚙️ <b>Настройка</b> {prop['channel_id']}: {prop['key']} = {prop['value']} "
                f"(сейчас {self.app.get_setting(self.cfg.channels[prop['channel_id']], prop['key'])})")

    async def ask_owner(self, pid: int, prop: dict) -> None:
        text = f"{self.describe(prop)}\n\nПочему: {html.escape(str(prop.get('rationale') or '–'))}"
        msg = await self.app.control_bot.send_message(
            self.cfg.owner_chat_id, text, pace=False,
            reply_markup=kb([[("✅ Принять", f"hr_ok:{pid}"), ("❌ Отклонить", f"hr_no:{pid}")]]))
        self.db.x("UPDATE proposals SET msg_id=? WHERE id=?", (msg["message_id"], pid))

    async def apply(self, pid: int) -> str:
        row = self.db.one("SELECT * FROM proposals WHERE id=?", (pid,))
        if row is None or row["status"] != "pending":
            return "уже обработано"
        prop = json.loads(row["payload"])
        err = self.validate(prop)
        if err:
            self.db.x("UPDATE proposals SET status='invalid', decided_at=? WHERE id=?", (now(), pid))
            return f"не применено: {err}"
        k = prop["kind"]
        if k == "create_agent":
            s = {key: v for key, v in prop["spec"].items() if key in SPEC_KEYS}
            self.db.x("INSERT INTO agents(name,channel_id,spec,status,origin,created_at,last_run) VALUES(?,?,?,?,?,?,?)",
                      (s["name"], s["channel_id"], dumps(s), "active", "hr", now(), now()))
            result = f"агент {s['name']} нанят"
        elif k == "retire_agent":
            self.db.x("UPDATE agents SET status='retired' WHERE name=?", (prop["name"],))
            result = f"агент {prop['name']} уволен"
        elif k == "add_source":
            s = prop["source"]
            src = Source(id=s["id"], name=s.get("name") or s["id"], kind=s["kind"], url=s["url"],
                         tier=min(3, max(2, int(s.get("tier", 3)))), group=s.get("group") or s["id"], pack=s["pack"],
                         lang=s.get("lang", "en"))
            try:
                entries, _, _ = await S.poll(self.app.http, src, None, None)
            except Exception as e:  # noqa: BLE001
                self.db.x("UPDATE proposals SET status='failed', decided_at=? WHERE id=?", (now(), pid))
                return f"источник недоступен: {e}"
            if not entries:
                self.db.x("UPDATE proposals SET status='failed', decided_at=? WHERE id=?", (now(), pid))
                return "источник пуст"
            spec = {k2: getattr(src, k2) for k2 in ("id", "name", "kind", "url", "tier", "group", "pack", "lang")}
            self.db.x("INSERT OR REPLACE INTO dynamic_sources(id,pack,spec,created_at,active) VALUES(?,?,?,?,1)",
                      (src.id, src.pack, dumps(spec), now()))
            result = f"источник {src.id} добавлен ({len(entries)} материалов)"
        else:
            typ = TUNABLE[prop["key"]][2]
            self.app.set_setting(self.cfg.channels[prop["channel_id"]], prop["key"], typ(prop["value"]))
            result = f"{prop['channel_id']}.{prop['key']} = {prop['value']}"
        self.db.x("UPDATE proposals SET status='applied', decided_at=? WHERE id=?", (now(), pid))
        self.db.event("INFO", "hr", result)
        return result

    def reject(self, pid: int) -> None:
        self.db.x("UPDATE proposals SET status='rejected', decided_at=? WHERE id=? AND status='pending'", (now(), pid))
