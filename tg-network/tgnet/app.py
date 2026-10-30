"""Application context + orchestrator.

Every agent runs as an independent supervised loop: a crash or hang in one loop is logged,
reported to the owner and retried with backoff, while everything else keeps working.
A /health endpoint exposes loop heartbeats for Docker/systemd/uptime monitors.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import signal
import time

import httpx

from . import agents as A
from .agents.hr import TUNABLE
from .config import Channel, Network, Source
from .control import Control
from .db import DB
from .llm import Router
from .telegram import Bot, TelegramError
from .util import domain, domain_matches, local_dt, now, parse_hhmm

log = logging.getLogger("tgnet.app")


class App:
    def __init__(self, cfg: Network, *, db: DB | None = None, http: httpx.AsyncClient | None = None,
                 router: Router | None = None):
        self.cfg = cfg
        self.db = db or DB(cfg.db_path)
        self.http = http or httpx.AsyncClient(limits=httpx.Limits(max_connections=40), headers={"Accept-Encoding": "gzip"})
        self.router = router or Router(cfg.llm, self.db, self.http, cfg.timezone)
        self.bots: dict[str, Bot] = {}
        self.control_bot = self.bot(cfg.bot_token)
        self.scout = A.Scout(self)
        self.editor = A.Editor(self)
        self.verifier = A.Verifier(self)
        self.writer = A.Writer(self)
        self.reviewer = A.Reviewer(self)
        self.publisher = A.Publisher(self)
        self.moderator = A.Moderator(self)
        self.analyst = A.Analyst(self)
        self.learner = A.Learner(self)
        self.hr = A.HR(self)
        self.specialists = A.Specialists(self)
        self.ads = A.Ads(self)
        self.control = Control(self)
        self.heartbeats: dict[str, float] = {}
        self.tasks: set[asyncio.Task] = set()
        self.stop = asyncio.Event()
        self.apply_overrides()

    # ------------------------------------------------------------------ lookups
    def bot(self, token: str) -> Bot:
        if token not in self.bots:
            self.bots[token] = Bot(token, self.http)
        return self.bots[token]

    def bot_for(self, ch: Channel) -> Bot:
        return self.bot(ch.bot_token)

    def active_channels(self) -> list[Channel]:
        return [c for c in self.cfg.channels.values() if c.enabled]

    def channel(self, cid: str | None) -> Channel | None:
        c = self.cfg.channels.get(cid or "")
        return c if c and c.enabled else None

    def channel_by_discussion(self, chat: dict) -> Channel | None:
        cid, uname = str(chat.get("id")), (chat.get("username") or "").lower()
        for ch in self.active_channels():
            d = (ch.discussion_chat_id or "").lower()
            if d and (d == cid or d.lstrip("@") == uname):
                return ch
        return None

    def dynamic_sources(self) -> list[Source]:
        out = []
        for r in self.db.all("SELECT spec FROM dynamic_sources WHERE active=1"):
            s = json.loads(r["spec"])
            out.append(Source(**{k: v for k, v in s.items() if k in Source.__dataclass_fields__}))
        return out

    def all_sources(self) -> list[Source]:
        return self.cfg.sources() + self.dynamic_sources()

    def source(self, sid: str) -> Source | None:
        return self.cfg.source(sid) or next((s for s in self.dynamic_sources() if s.id == sid), None)

    def effective_tier(self, src: Source) -> int:
        """Configured tier, demoted by one level if the source keeps getting things wrong."""
        a, b = self.db.reliability(src.id)
        if a + b - 2 >= 8 and a / (a + b) < 0.6:
            return min(3, src.tier + 1)
        return src.tier

    def domain_info(self, host: str, ch: Channel) -> dict | None:
        host = (host or "").lower().strip()
        if not host or "." not in host:
            return None
        for s in self.all_sources():
            if s.pack in ch.packs and s.kind != "telegram" and domain_matches(host, domain(s.url)):
                return {"domain": domain(s.url), "tier": self.effective_tier(s), "group": s.group}
        for p in ch.packs:
            pack = self.cfg.packs.get(p)
            for d, tier in (pack.trusted_domains if pack else {}).items():
                if domain_matches(host, d):
                    return {"domain": d, "tier": tier, "group": f"web:{d}"}
        return None

    # ------------------------------------------------------------------ runtime state
    def paused(self, cid: str) -> bool:
        return bool(self.db.get("paused:all")) or bool(self.db.get(f"paused:{cid}"))

    def autonomy(self, ch: Channel) -> str:
        return self.db.get(f"autonomy:{ch.id}") or ch.autonomy

    def get_setting(self, ch: Channel, key: str):
        obj = ch
        *path, last = key.split(".")
        for p in path:
            obj = getattr(obj, p)
        return getattr(obj, last)

    def set_setting(self, ch: Channel, key: str, value) -> None:
        self.db.set(f"override:{ch.id}:{key}", value)
        obj = ch
        *path, last = key.split(".")
        for p in path:
            obj = getattr(obj, p)
        setattr(obj, last, value)

    def apply_overrides(self) -> None:
        for ch in self.cfg.channels.values():
            for key in TUNABLE:
                v = self.db.get(f"override:{ch.id}:{key}")
                if v is not None:
                    obj = ch
                    *path, last = key.split(".")
                    for p in path:
                        obj = getattr(obj, p)
                    setattr(obj, last, v)

    # ------------------------------------------------------------------ owner messaging
    async def notify(self, text: str, key: str | None = None, throttle: float = 3600) -> None:
        if key:
            last = self.db.get(f"notify:{key}") or 0
            if now() - last < throttle:
                return
            self.db.set(f"notify:{key}", now())
        if not self.cfg.owner_chat_id:
            log.warning("owner notification (no owner_chat_id): %s", text)
            return
        try:
            await self.control_bot.send_message(self.cfg.owner_chat_id, text, pace=False)
        except Exception as e:  # noqa: BLE001 - notifications must never crash a loop
            log.warning("owner notification failed: %s", e)

    async def mark_owner_msg(self, msg_id: int | None, note: str) -> None:
        if not msg_id:
            return
        try:
            await self.control_bot.call("editMessageReplyMarkup", chat_id=self.cfg.owner_chat_id, message_id=msg_id,
                                        reply_markup={"inline_keyboard": []})
        except TelegramError:
            pass
        try:
            await self.control_bot.send_message(self.cfg.owner_chat_id, note, pace=False, reply_to=msg_id)
        except TelegramError as e:
            log.info("owner note failed: %s", e)

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)

        def done(t: asyncio.Task):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception():
                log.error("background task failed: %r", t.exception())
                self.db.event("ERROR", "task", repr(t.exception()))

        task.add_done_callback(done)
        return task

    # ------------------------------------------------------------------ scheduled jobs
    def _due_daily(self, key: str, hhmm: str, weekday: int | None = None) -> bool:
        t = now()
        d = local_dt(t, self.cfg.timezone)
        h, m = parse_hhmm(hhmm)
        if weekday is not None and d.weekday() != weekday:
            return False
        if (d.hour, d.minute) < (h, m):
            return False
        today = d.strftime("%Y-%m-%d")
        if self.db.get(key) == today:
            return False
        self.db.set(key, today)
        return True

    async def daily_tick(self) -> None:
        if self._due_daily("last_report", self.cfg.report_time):
            await self.analyst.daily()
        if self._due_daily("last_decay", "04:00", weekday=0):
            self.learner.decay()

    async def hr_tick(self) -> None:
        if self._due_daily("last_hr", self.cfg.hr_time, weekday=self.cfg.hr_weekday):
            await self.hr.run()

    async def maintenance(self) -> None:
        t = now()
        self.db.x("UPDATE items SET content=NULL WHERE content IS NOT NULL AND fetched_at<?", (t - 3 * 86400,))
        self.db.x("DELETE FROM items WHERE fetched_at<?", (t - 21 * 86400,))
        self.db.x("DELETE FROM stories WHERE last_seen<?", (t - 21 * 86400,))
        self.db.x("DELETE FROM triage WHERE ts<?", (t - 21 * 86400,))
        self.db.x("DELETE FROM events WHERE ts<?", (t - 30 * 86400,))
        self.db.x("DELETE FROM llm_usage WHERE ts<?", (t - 90 * 86400,))
        self.db.x("DELETE FROM mod_actions WHERE ts<?", (t - 90 * 86400,))
        self.db.x("PRAGMA wal_checkpoint(TRUNCATE)")

    # ------------------------------------------------------------------ supervision
    def loop_ages(self) -> dict[str, float]:
        t = time.monotonic()
        return {k: round(t - v, 1) for k, v in self.heartbeats.items()}

    async def supervised(self, name: str, fn, interval: float, timeout: float) -> None:
        fails = 0
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(fn(), timeout)
                self.heartbeats[name] = time.monotonic()
                fails = 0
                delay = interval
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                fails += 1
                self.heartbeats[name] = time.monotonic()
                msg = f"{type(e).__name__}: {e}"
                log.error("loop %s failed (%d in a row): %s", name, fails, msg, exc_info=fails == 1)
                self.db.event("ERROR", name, msg)
                if fails in (3, 10) or fails % 50 == 0:
                    await self.notify(f"⚠️ Цикл <b>{name}</b> падает {fails} раз подряд:\n<code>{html.escape(msg[:400])}</code>",
                                      key=f"loop:{name}:{fails}")
                delay = min(300, 5 * 2 ** min(fails, 6))
            try:
                await asyncio.wait_for(self.stop.wait(), delay)
            except asyncio.TimeoutError:
                pass

    def loops(self) -> list[tuple[str, object, float, float]]:
        loops = [
            ("scout", self.scout.tick, 30, 180),
            ("editor", self.editor.tick, 90, 600),
            ("verifier", self.verifier.tick, 20, 1500),
            ("writer", self.writer.tick, 15, 900),
            ("reviewer", self.reviewer.tick, 15, 900),
            ("publisher", self.publisher.tick, 20, 300),
            ("ads", self.ads.tick, 30, 120),
            ("specialists", self.specialists.tick, 60, 900),
            ("analyst", self.analyst.collect, 3600, 300),
            ("daily", self.daily_tick, 60, 900),
            ("hr", self.hr_tick, 300, 900),
            ("maintenance", self.maintenance, 6 * 3600, 300),
        ]
        tokens = {self.cfg.bot_token} | {c.bot_token for c in self.active_channels()}
        for tok in sorted(t for t in tokens if t):
            bot = self.bot(tok)
            loops.append((f"telegram:{bot.bot_id}", lambda b=bot: self.control.poll(b), 0, 120))
        return loops

    async def health_server(self) -> asyncio.base_events.Server | None:
        if not self.cfg.health_port:
            return None

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                await asyncio.wait_for(reader.readline(), 5)
                ages = self.loop_ages()
                limits = {n: max(900, (i + to) * 2) for n, _, i, to in self.loops()}
                stale = [n for n, a in ages.items() if a > limits.get(n, 900)]
                backlog = {r["status"]: r["c"] for r in self.db.all(
                    "SELECT status, COUNT(*) c FROM posts WHERE status NOT IN ('published','rejected','expired') "
                    "GROUP BY status")}
                body = json.dumps({"ok": not stale, "stale": stale, "loops": ages, "backlog": backlog}).encode()
                code = "200 OK" if not stale else "503 Service Unavailable"
                writer.write(f"HTTP/1.1 {code}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                             f"Connection: close\r\n\r\n".encode() + body)
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                writer.close()

        try:
            return await asyncio.start_server(handle, "0.0.0.0", self.cfg.health_port)
        except OSError as e:
            log.warning("health server disabled: %s", e)
            return None

    def prepare(self) -> None:
        """Local, instant start-up work that must finish before the loops begin."""
        for ch in self.active_channels():
            self.bot_for(ch)
        self.specialists.seed_from_config()
        # posts claimed by a previous process that died are released immediately
        self.db.x("UPDATE posts SET lease_until=0 WHERE status IN ('selected','verified','revise','drafted','ready') "
                  "AND lease_until>?", (now(),))

    async def announce(self) -> None:
        for tok, bot in list(self.bots.items()):
            if not tok:
                continue
            try:
                me = await bot.get_me()
                log.info("bot @%s ready", me.get("username"))
            except TelegramError as e:
                log.error("bot %s unavailable: %s", bot.bot_id, e)
                self.db.event("ERROR", "startup", f"bot {bot.bot_id}: {e}")
        n_src = len(self.all_sources())
        await self.notify(f"🟢 Сеть запущена: каналов {len(self.active_channels())}, источников {n_src}. /status")

    async def startup(self) -> None:
        self.prepare()
        await self.announce()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        self.prepare()
        server = await self.health_server()
        workers = [asyncio.create_task(self.supervised(n, fn, i, to), name=n) for n, fn, i, to in self.loops()]
        self.spawn(self.announce())
        log.info("orchestrator started with %d loops", len(workers))
        await self.stop.wait()
        log.info("shutting down…")
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, *self.tasks, return_exceptions=True)
        if server:
            server.close()
        await self.notify("🔴 Сеть остановлена")
        await self.http.aclose()
        self.db.close()
