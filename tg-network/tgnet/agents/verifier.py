"""Fact-checker: turns a story into a verified fact sheet, or refuses to.

Pipeline: read the full articles -> extract claims -> search every other source we track
for corroboration -> LLM judges which evidence supports/contradicts each claim ->
deterministic verdict by source tier and independence (never by the LLM's say-so) ->
optional Google-grounded web check that only counts whitelisted domains.

Verdicts:
  confirmed  tier-1 (official) source, or N independent tier<=2 outlets
  reported   one major outlet; publishable only with attribution ("по данным ...")
  rumor      only insiders/aggregators; publishable only if the channel allows, labelled
  waiting    too early to tell; re-checked in a few minutes
  rejected   contradicted, stale, or unsupported
"""

from __future__ import annotations

import json

from ..config import Channel
from ..llm import BudgetExceeded
from ..sources import fetch_article
from ..util import clip, domain, dumps, now
from .base import Agent

MAX_EVIDENCE = 12


class Verifier(Agent):
    name = "verifier"
    role = "factcheck"
    prompt = "verifier_extract"
    lesson_role = "factcheck"

    async def tick(self, limit: int = 4) -> int:
        n = 0
        while n < limit:
            p = self.db.claim(("selected", "waiting"), lease_s=900)
            if p is None:
                break
            n += 1
            try:
                await self.process(p)
            except BudgetExceeded as e:
                self.db.update_post(p["id"], lease_until=now() + 1800, last_error=str(e))
                break
            except Exception as e:  # noqa: BLE001
                self.log.exception("verify post %s failed", p["id"])
                if self.db.fail(p["id"], f"verify: {e}"):
                    await self.app.notify(f"❌ Пост #{p['id']} не прошёл проверку фактов после повторов: {e}",
                                          key=f"fail:{p['id']}")
        return n

    # ------------------------------------------------------------------ evidence
    async def enrich(self, items: list[dict]) -> None:
        for it in items:
            if it.get("content") or "t.me/" in it["url"]:
                continue
            try:
                art = await fetch_article(self.app.http, it["url"])
            except Exception as e:  # noqa: BLE001
                self.log.debug("article fetch failed %s: %s", it["url"], e)
                art = {}
            text = (art.get("text") or "")[:12000]
            it["content"] = text
            if not it.get("image") and art.get("image"):
                it["image"] = art["image"]
            self.db.x("UPDATE items SET content=?, image=COALESCE(image, ?) WHERE id=?",
                      (text or None, art.get("image") or None, it["id"]))

    def item_dicts(self, rows) -> list[dict]:
        out = []
        for r in rows:
            d = dict(r)
            src = self.app.source(d["source_id"])
            if src is None:
                continue
            d["src_name"], d["tier"], d["group"] = src.name, self.app.effective_tier(src), src.group
            out.append(d)
        return out

    def search(self, ch: Channel, keywords: list[str], exclude_story: int, t: float) -> list[dict]:
        kws = sorted({k.lower().replace("ё", "е").strip() for k in keywords if len(k.strip()) >= 3})
        if not kws:
            return []
        qs = ",".join("?" * len(ch.packs))
        rows = self.db.all(
            f"SELECT * FROM items WHERE pack IN ({qs}) AND fetched_at>? AND (story_id IS NULL OR story_id!=?) "
            f"ORDER BY fetched_at DESC LIMIT 3000", (*ch.packs, t - 48 * 3600, exclude_story))
        need = 1 if len(kws) == 1 else 2
        scored = []
        for r in rows:
            blob = f"{r['title']} {r['summary'] or ''} {(r['content'] or '')[:3000]}".lower().replace("ё", "е")
            hits = sum(1 for k in kws if k in blob)
            if hits >= need:
                scored.append((hits, r))
        scored.sort(key=lambda x: -x[0])
        return self.item_dicts([r for _, r in scored[:20]])

    @staticmethod
    def evidence_text(i: int, it: dict, t: float) -> str:
        age = int((t - (it.get("published_at") or t)) / 60)
        body = (it.get("content") or it.get("summary") or "")[:2200]
        return (f"[E{i}] {it['src_name']} (уровень T{it['tier']}, {age} мин назад) {it['url']}\n"
                f"Заголовок: {it['title']}\n{body}")

    # ------------------------------------------------------------------ main
    async def process(self, p) -> None:
        ch = self.app.channel(p["channel_id"])
        story = self.db.one("SELECT * FROM stories WHERE id=?", (p["story_id"],))
        t = now()
        if ch is None or story is None:
            self.db.advance(p["id"], "rejected", owner_note="канал или сюжет не найден")
            return
        if t - story["first_seen"] > ch.schedule.max_age_hours * 3600:
            self.db.advance(p["id"], "expired", owner_note="сюжет устарел")
            return
        items = self.item_dicts(self.db.all("SELECT * FROM items WHERE story_id=? ORDER BY published_at",
                                            (story["id"],)))
        items.sort(key=lambda d: (d["tier"], d.get("published_at") or 0))
        if not items:
            self.db.advance(p["id"], "rejected", owner_note="нет материалов")
            return
        newest = max((d.get("published_at") or 0) for d in items)
        if t - newest > ch.verification.max_item_age_hours * 3600:
            self.db.advance(p["id"], "expired", owner_note="материалы старше лимита свежести")
            return
        await self.enrich(items[:3])

        # 1) claims + search keywords
        primary = "\n\n".join(self.evidence_text(i + 1, it, t) for i, it in enumerate(items[:4]))
        ext, _ = await self.ask_json(f"Материалы сюжета:\n\n{primary}", ch)
        claims = [c if isinstance(c, str) else str(c.get("text", "")) for c in (ext.get("claims") or [])]
        claims = [c for c in claims if c.strip()][:6]
        main = str(ext.get("main_claim") or (claims[0] if claims else "")).strip()
        if not main:
            self.db.advance(p["id"], "rejected", owner_note="не удалось выделить проверяемое утверждение")
            return
        if main not in claims:
            claims.insert(0, main)

        # 2) corroboration across everything we track
        seen_urls = {it["url"] for it in items}
        extra = [c for c in self.search(ch, list(ext.get("keywords") or []), story["id"], t) if c["url"] not in seen_urls]
        extra.sort(key=lambda d: d["tier"])
        await self.enrich([c for c in extra[:3] if c["tier"] <= 2])
        evidence = (items + extra)[:MAX_EVIDENCE]
        ev_text = "\n\n".join(self.evidence_text(i + 1, it, t) for i, it in enumerate(evidence))
        claims_text = "\n".join(f"{i}. {c}" for i, c in enumerate(claims))
        judge, _ = await self.ask_json(
            f"Утверждения (0 – главное):\n{claims_text}\n\nДоказательства:\n\n{ev_text}", ch,
            system=self.judge_system(ch))

        verdicts = self.score(claims, judge, evidence, ch)
        for c, v in zip(claims, verdicts):
            v["text"] = c
        main_v = verdicts[0]

        # 3) optional web check through Google grounding, whitelisted domains only
        if main_v["status"] != "confirmed" and ch.verification.use_grounding and not main_v["contradicted"]:
            web = await self.grounded_check(main, ch)
            if web:
                main_v["web"] = web
                groups = set(main_v["groups"]) | {w["group"] for w in web if w["tier"] <= 2}
                if any(w["tier"] == 1 for w in web) or len(groups) >= ch.verification.independent_confirmations:
                    main_v["status"] = "confirmed"

        status = self.decide(main_v, story, p, ch, t)
        if status == "waiting":
            self.db.update_post(p["id"], status="waiting", lease_until=t + ch.verification.recheck_minutes * 60,
                                attempts=0, checks=(p["checks"] or 0) + 1, updated_at=t,
                                owner_note=f"ждём подтверждения: {main_v['status']}")
            return
        if status == "rejected":
            reason = "противоречие в источниках" if main_v["contradicted"] else f"недостаточно подтверждений ({main_v['status']})"
            self.db.advance(p["id"], "rejected", owner_note=reason, facts=self.sheet(ext, judge, verdicts, evidence, status))
            return
        sheet = self.sheet(ext, judge, verdicts, evidence, status)
        image = next((it.get("image") for it in evidence if it.get("image") and it["tier"] <= 2), None)
        support_ids = sorted({evidence[i]["source_id"] for i in main_v["support_idx"]})
        self.db.advance(p["id"], "verified", facts=sheet, image=image, sources=support_ids,
                        confidence=main_v["confidence"])
        self.log.info("[%s] post %s verified as %s: %s", ch.id, p["id"], status, clip(main, 100))

    def judge_system(self, ch: Channel) -> str:
        from .base import load_prompt, render

        return render(load_prompt("verifier_judge", self.cfg.base_dir), **self.channel_vars(ch)) + self.lessons_block(ch)

    # ------------------------------------------------------------------ scoring (deterministic)
    @staticmethod
    def _refs(refs, n: int) -> list[int]:
        out = []
        for r in refs or []:
            s = str(r).strip().upper().lstrip("E")
            if s.isdigit() and 1 <= int(s) <= n:
                out.append(int(s) - 1)
        return sorted(set(out))

    def score(self, claims: list[str], judge: dict, evidence: list[dict], ch: Channel) -> list[dict]:
        by_i = {}
        for c in judge.get("claims") or []:
            try:
                by_i[int(c.get("i"))] = c
            except (TypeError, ValueError):
                continue
        out = []
        need = ch.verification.independent_confirmations
        for i, _ in enumerate(claims):
            c = by_i.get(i, {})
            sup = self._refs(c.get("support"), len(evidence))
            con = self._refs(c.get("contradict"), len(evidence))
            tiers = [evidence[j]["tier"] for j in sup]
            groups = sorted({evidence[j]["group"] for j in sup if evidence[j]["tier"] <= 2})
            contradicted = any(evidence[j]["tier"] <= 2 for j in con)
            if contradicted:
                status = "contradicted"
            elif 1 in tiers or len(groups) >= need:
                status = "confirmed"
            elif any(t <= 2 for t in tiers):
                status = "reported"
            elif tiers:
                status = "rumor"
            else:
                status = "unsupported"
            conf = {"confirmed": 0.95, "reported": 0.75, "rumor": 0.5}.get(status, 0.1)
            if status == "confirmed" and 1 not in tiers:
                conf = min(0.97, 0.85 + 0.04 * (len(groups) - need))
            out.append({"status": status, "support_idx": sup, "groups": groups, "contradicted": contradicted,
                        "confidence": conf})
        return out

    def decide(self, v: dict, story, p, ch: Channel, t: float) -> str:
        young = t - story["first_seen"] < ch.verification.recheck_window_hours * 3600
        checks = p["checks"] or 0
        st = v["status"]
        if st == "contradicted":
            return "rejected"
        if st == "confirmed":
            return "confirmed"
        # give corroboration a chance to arrive before settling for attribution
        if young and checks < 2 and st in ("reported", "rumor", "unsupported"):
            return "waiting"
        if st == "reported":
            return "reported"  # attribution is mandatory; hedged wording is flagged in the sheet
        if st == "rumor" and ch.verification.allow_rumors:
            return "rumor"
        if young and checks < 6:
            return "waiting"
        return "rejected"

    def sheet(self, ext: dict, judge: dict, verdicts: list[dict], evidence: list[dict], status: str) -> str:
        n = len(evidence)
        claims = []
        for v in verdicts:
            claims.append({"text": v.get("text", ""), "status": v["status"],
                           "sources": sorted({evidence[j]["src_name"] for j in v["support_idx"]})})
        facts = []
        for f in judge.get("facts") or []:
            refs = self._refs(f.get("evidence"), n) if isinstance(f, dict) else []
            txt = f.get("text") if isinstance(f, dict) else str(f)
            if txt and refs:
                facts.append({"text": txt, "sources": sorted({evidence[j]["src_name"] for j in refs}),
                              "tier": min(evidence[j]["tier"] for j in refs)})
        quotes = []
        for q in judge.get("quotes") or []:
            if not isinstance(q, dict):
                continue
            refs = self._refs([q.get("evidence")] if isinstance(q.get("evidence"), str) else q.get("evidence"), n)
            if q.get("text") and refs:
                quotes.append({"text": q["text"], "who": q.get("who", ""), "source": evidence[refs[0]]["src_name"]})
        main = verdicts[0]
        attribution = sorted({evidence[j]["src_name"] for j in main["support_idx"]})
        links = [evidence[j]["url"] for j in main["support_idx"] if not evidence[j]["url"].startswith("https://t.me/")][:3]
        web = [{"domain": w["domain"], "tier": w["tier"]} for w in main.get("web", [])]
        return dumps({"status": status, "headline": ext.get("headline") or "", "main_claim": verdicts[0].get("text", ""),
                      "facts": facts, "quotes": quotes, "attribution": attribution, "links": links, "web": web,
                      "event_date": ext.get("event_date") or "", "notes": judge.get("notes") or "",
                      "hedged": bool(ext.get("rumor_language")),
                      "claims": claims})

    async def grounded_check(self, claim: str, ch: Channel) -> list[dict]:
        from .base import load_prompt

        try:
            data, res = await self.app.router.json(
                "grounding", load_prompt("verifier_ground", self.cfg.base_dir), f"Утверждение: {claim}",
                temperature=0, grounding=True)
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001 - grounding is optional
            self.log.info("grounding unavailable: %s", e)
            return []
        if str(data.get("verdict")) != "supported":
            return []
        found, seen = [], set()
        candidates = [c.get("title", "") for c in res.citations] + [domain(c.get("url", "")) for c in res.citations]
        candidates += [domain(s.get("url", "")) for s in data.get("sources") or [] if isinstance(s, dict)]
        for host in candidates:
            info = self.app.domain_info(host, ch)
            if info and info["domain"] not in seen:
                seen.add(info["domain"])
                found.append(info)
        return found


def load_sheet(raw: str | None) -> dict:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}
