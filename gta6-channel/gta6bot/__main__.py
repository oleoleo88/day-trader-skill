"""Точка входа: python -m gta6bot [run | check | dry-run]."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import __version__
from .config import Config, ConfigError, load_config


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for noisy in ("aiogram.event", "httpx2", "httpx", "anthropic", "trafilatura", "htmldate", "courlan", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def has_claude_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


# ── check ────────────────────────────────────────────────────────────────────

async def cmd_check(cfg: Config) -> int:
    """Проверка настроек перед запуском: Telegram, права, Claude API, источники."""
    from .app import StartupError, close_context, create_context, startup

    failed = False
    print(f"Настройки прочитаны: {cfg.base_dir / 'config.yaml'}")
    ctx = await create_context(cfg)
    try:
        print("\n— Telegram —")
        try:
            problems = await startup(ctx)
            print(f"✅ Бот @{ctx.bot_username}, канал «{ctx.channel_title}» ({ctx.channel_id})"
                  + (f", чат комментариев {ctx.discussion_id}" if ctx.discussion_id else ""))
            for p in problems:
                print(f"⚠️  {p}")
            try:
                chat = await ctx.bot.get_chat(ctx.admin_chat)
                print(f"✅ Админ-чат доступен: {chat.title or chat.full_name}")
            except Exception as e:  # noqa: BLE001
                failed = True
                print(f"❌ Админ-чат {ctx.admin_chat} недоступен: {e}\n"
                      f"   Личка: откройте @{ctx.bot_username} и нажмите Start. Группа: добавьте в неё бота.")
        except StartupError as e:
            failed = True
            print(f"❌ {e}")

        print("\n— Claude API —")
        if not has_claude_key():
            failed = True
            print("❌ В .env не задан ANTHROPIC_API_KEY (ключ создаётся на console.anthropic.com)")
        else:
            for role, model, error in await ctx.llm.check_models():
                if error:
                    failed = True
                    print(f"❌ {role}: {model} — {error}")
                else:
                    print(f"✅ {role}: {model}")

        print("\n— Источники —")
        bad = 0
        for src, ok, detail in await ctx.collector.check_sources():
            print(f"{'✅' if ok else '❌'} {src.name}: {detail}")
            bad += not ok
        if bad:
            print(f"\n{bad} источников не ответили. Бот запустится и без них, но лучше поправить url в config.yaml "
                  f"или выключить их (enabled: false).")
    finally:
        await close_context(ctx)
    print("\n" + ("❌ Есть проблемы, которые помешают работе — исправьте их и запустите проверку снова."
                  if failed else "✅ Можно запускать: docker compose up -d"))
    return 1 if failed else 0


# ── dry-run ──────────────────────────────────────────────────────────────────

async def cmd_dry_run(cfg: Config, limit: int) -> int:
    """Пробный прогон без Telegram: собрать новости, показать решения редактора и готовые посты."""
    from .app import close_context, create_context
    from .editor import item_note
    from .textutil import visible_text

    if not has_claude_key():
        print("❌ Для пробного прогона нужен ANTHROPIC_API_KEY в .env")
        return 1
    cfg.secrets.data_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.secrets.data_dir / "dry-run.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        p = path.with_name(path.name + suffix)
        if p.exists():
            p.unlink()
    ctx = await create_context(cfg, with_bot=False, db_path=path)
    ctx.dry_run = True
    ctx.channel_username = "your_channel"
    try:
        print("Опрашиваю источники…")
        sources = [s for s in cfg.collector.sources if s.enabled]
        fresh = 0
        for src in sources:
            try:
                fresh += await ctx.collector.poll_source(src, force=True)
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠️ {src.name}: {e}")
        print(f"Записей по теме за последние {cfg.collector.backfill_hours} ч: {fresh}. Отдаю редактору (не больше {limit})…\n")
        stats = await ctx.editor.process_new(limit=limit)
        for item in await ctx.db.all("SELECT * FROM items WHERE status NOT IN ('seeded', 'new') ORDER BY id"):
            note = item_note(item)
            print(f"[{item['status']:>9}] {item['source']}: {item['title'][:90]}"
                  + (f"\n            ↳ {note['reason']}" if note.get("reason") else ""))
        posts = await ctx.db.all("SELECT * FROM posts ORDER BY id")
        print(f"\nГотовых постов: {len(posts)} (всего обработано: {sum(stats.values())}, ошибок: {stats['errors']})")
        for post in posts:
            route = "на согласование" if ctx.editor.wants_review(post) else "в очередь"
            print("\n" + "═" * 78)
            print(f"#{post['id']} · {post['kind']} · {post['category']} · важность {post['importance']} · "
                  f"уверенность {post['confidence']:.0%} · {route}")
            print("─" * 78)
            print(visible_text(post["text"]))
    finally:
        await close_context(ctx)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gta6bot", description="Автоматизация Telegram-канала о GTA 6")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check", "dry-run", "version"],
                        help="run — запустить бота; check — проверить настройки; dry-run — пробный прогон без Telegram")
    parser.add_argument("--home", help="папка проекта с config.yaml и .env (по умолчанию текущая)")
    parser.add_argument("--limit", type=int, default=12, help="dry-run: сколько записей отдать редактору")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный журнал")
    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    setup_logging(args.verbose)
    try:
        cfg = load_config(args.home, require_secrets=args.command != "dry-run")
    except ConfigError as e:
        print(f"❌ Ошибка настроек: {e}", file=sys.stderr)
        return 2
    try:
        if args.command == "check":
            return asyncio.run(cmd_check(cfg))
        if args.command == "dry-run":
            return asyncio.run(cmd_dry_run(cfg, max(1, args.limit)))
        if not has_claude_key():
            print("❌ В .env не задан ANTHROPIC_API_KEY: без него ИИ-редактор и ИИ-модератор не работают.", file=sys.stderr)
            return 2
        from .app import StartupError, run
        try:
            asyncio.run(run(cfg))
        except StartupError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
