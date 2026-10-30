"""Command line: python -m tgnet <command>."""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from . import config as C
from .util import setup_logging


def _app(base: str):
    from .app import App

    cfg = C.load(base)
    setup_logging(cfg.log_level)
    return cfg, App(cfg)


async def cmd_run(base: str) -> int:
    cfg, app = _app(base)
    errs = C.validate(cfg)
    if errs:
        print("Ошибки конфигурации:\n  - " + "\n  - ".join(errs))
        return 2
    await app.run()
    return 0


async def cmd_check(base: str) -> int:
    from .telegram import Bot, TelegramError

    cfg = C.load(base)
    setup_logging("WARNING")
    ok = True
    errs = C.validate(cfg)
    for e in errs:
        print("✗", e)
        ok = False
    async with httpx.AsyncClient() as http:
        tokens = {cfg.bot_token} | {c.bot_token for c in cfg.channels.values() if c.enabled}
        bots = {}
        for tok in filter(None, tokens):
            b = Bot(tok, http)
            try:
                me = await b.get_me()
                bots[tok] = (b, me)
                print(f"✓ бот @{me['username']}")
            except TelegramError as e:
                print(f"✗ бот {tok.split(':')[0]}: {e}")
                ok = False
        for ch in cfg.channels.values():
            if not ch.enabled or ch.bot_token not in bots:
                continue
            b, me = bots[ch.bot_token]
            for chat, need in ((ch.chat_id, "can_post_messages"), (ch.discussion_chat_id, "can_delete_messages")):
                if not chat:
                    continue
                try:
                    m = await b.call("getChatMember", chat_id=chat, user_id=me["id"])
                    if m.get("status") == "creator" or m.get(need):
                        print(f"✓ {ch.id}: бот админ в {chat} ({need})")
                    else:
                        print(f"✗ {ch.id}: у бота нет права {need} в {chat}")
                        ok = False
                except TelegramError as e:
                    print(f"✗ {ch.id}: {chat}: {e}")
                    ok = False
        from .db import DB
        from .llm import LLMError, Router

        router = Router(cfg.llm, DB(":memory:"), http, cfg.timezone)
        router.cfg.retries = 0
        for alias, m in cfg.llm.models.items():
            prov = cfg.llm.providers.get(m.provider)
            if prov is None or (prov.optional and not prov.api_key and prov.kind == "gemini"):
                continue
            router.cfg.roles["__probe"] = [alias]
            try:
                res = await router.complete("__probe", "", "Ответь одним словом: работает?", max_tokens=20)
                print(f"✓ LLM {alias} ({m.provider}/{m.model}): {res.text.strip()[:40]!r}")
            except LLMError as e:
                mark = "·" if prov.optional else "✗"
                print(f"{mark} LLM {alias} ({m.provider}/{m.model}): {str(e)[:160]}")
                ok = ok and prov.optional
    print("\nВсё готово к запуску." if ok else "\nИсправь ошибки выше.")
    return 0 if ok else 1


async def cmd_sources(base: str) -> int:
    from . import sources as S

    cfg = C.load(base)
    setup_logging("WARNING")
    bad = 0
    async with httpx.AsyncClient() as http:
        async def one(src):
            try:
                entries, _, _ = await S.poll(http, src, None, None)
                n = len(entries or [])
                newest = max((e.published_at or 0 for e in entries or []), default=0)
                return src, n, newest, None
            except Exception as e:  # noqa: BLE001
                return src, 0, 0, str(e)

        results = await asyncio.gather(*(one(s) for s in cfg.sources()))
    from .util import iso

    for src, n, newest, err in sorted(results, key=lambda r: (r[0].pack, r[0].id)):
        if err or n == 0:
            bad += 1
            print(f"✗ [{src.pack}] {src.id:<22} T{src.tier} {src.url}\n    {err or 'пусто'}")
        else:
            print(f"✓ [{src.pack}] {src.id:<22} T{src.tier} материалов: {n:<3} свежий: {iso(newest)}")
    print(f"\nРаботает {len(results) - bad} из {len(results)}. Нерабочие выключи (enabled: false) или поправь url.")
    return 0 if bad == 0 else 1


async def cmd_whoami(base: str) -> int:
    from .telegram import Bot

    cfg = C.load(base)
    if not cfg.bot_token:
        print("Сначала укажи BOT_TOKEN в .env")
        return 2
    async with httpx.AsyncClient() as http:
        b = Bot(cfg.bot_token, http)
        me = await b.get_me()
        print(f"Напиши боту @{me['username']} любое сообщение, затем запусти команду ещё раз.\n")
        ups = await b.call("getUpdates", timeout=1)
        seen = set()
        for u in ups:
            m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
            chat = m.get("chat") or {}
            if chat and chat.get("id") not in seen:
                seen.add(chat.get("id"))
                print(f"{chat.get('type'):<11} id={chat.get('id'):<16} {chat.get('title') or chat.get('username') or chat.get('first_name')}")
    return 0


async def cmd_once(base: str, stage: str) -> int:
    cfg, app = _app(base)
    fn = {"scout": app.scout.tick, "editor": app.editor.tick, "verifier": app.verifier.tick,
          "writer": app.writer.tick, "reviewer": app.reviewer.tick, "publisher": app.publisher.tick,
          "analyst": app.analyst.collect, "specialists": app.specialists.tick, "hr": app.hr.run,
          "ads": app.ads.tick}.get(stage)
    if stage == "report":
        print(app.analyst.report())
        return 0
    if fn is None:
        print("Этапы: scout editor verifier writer reviewer publisher analyst specialists hr ads report")
        return 2
    app.specialists.seed_from_config()
    print(stage, "->", await fn())
    await app.http.aclose()
    return 0


async def cmd_stats(base: str) -> int:
    from .db import DB
    from .util import iso

    cfg = C.load(base)
    db = DB(cfg.db_path)
    print("Посты по статусам:")
    for r in db.all("SELECT channel_id, status, COUNT(*) c FROM posts GROUP BY channel_id, status ORDER BY channel_id"):
        print(f"  {r['channel_id']:<16} {r['status']:<10} {r['c']}")
    print("\nПоследние публикации:")
    for r in db.all("SELECT id, channel_id, published_at, substr(final,1,80) t FROM posts WHERE published_at IS NOT NULL "
                    "ORDER BY published_at DESC LIMIT 10"):
        print(f"  #{r['id']} {r['channel_id']} {iso(r['published_at'])} {r['t']!r}")
    print(f"\nМатериалов: {db.scalar('SELECT COUNT(*) FROM items')}, сюжетов: {db.scalar('SELECT COUNT(*) FROM stories')}, "
          f"уроков: {db.scalar('SELECT COUNT(*) FROM lessons WHERE active=1')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tgnet", description="Сеть Telegram-каналов на ИИ-агентах")
    ap.add_argument("--base", default=".", help="папка проекта (где config/ и .env)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="запустить сеть")
    sub.add_parser("check", help="проверить конфиг, ботов, права и LLM")
    sub.add_parser("sources", help="проверить все источники")
    sub.add_parser("whoami", help="узнать chat id (свой, канала, группы)")
    o = sub.add_parser("once", help="один шаг этапа (отладка)")
    o.add_argument("stage")
    sub.add_parser("stats", help="статистика из базы")
    a = ap.parse_args(argv)
    coro = {"run": lambda: cmd_run(a.base), "check": lambda: cmd_check(a.base), "sources": lambda: cmd_sources(a.base),
            "whoami": lambda: cmd_whoami(a.base), "once": lambda: cmd_once(a.base, a.stage),
            "stats": lambda: cmd_stats(a.base)}[a.cmd]
    try:
        return asyncio.run(coro())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
