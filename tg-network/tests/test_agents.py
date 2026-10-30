import json
import time

from conftest import OWNER, FakeLLM, run
from tgnet.agents.ads import marked
from tgnet.config import AdSlot


# --------------------------------------------------------------------------- verifier verdicts
def _ev(*specs):
    return [{"tier": t, "group": g, "src_name": g, "url": f"https://{g}.test/x", "source_id": g} for t, g in specs]


def _story(age_h):
    return {"first_seen": time.time() - age_h * 3600}


def test_verdicts_by_tier_and_independence(harness):
    h = harness()
    v, ch = h.app.verifier, h.cfg.channels["football_ru"]
    judge = {"claims": [{"i": 0, "support": ["E1", "E2"], "contradict": []}]}
    # two outlets of the SAME group are one confirmation
    same = v.score(["c"], judge, _ev((2, "bbc"), (2, "bbc")), ch)[0]
    assert same["status"] == "reported"
    indep = v.score(["c"], judge, _ev((2, "bbc"), (2, "guardian")), ch)[0]
    assert indep["status"] == "confirmed"
    official = v.score(["c"], {"claims": [{"i": 0, "support": ["E1"]}]}, _ev((1, "club")), ch)[0]
    assert official["status"] == "confirmed" and official["confidence"] >= 0.95
    contra = v.score(["c"], {"claims": [{"i": 0, "support": ["E1"], "contradict": ["E2"]}]},
                     _ev((1, "club"), (2, "bbc")), ch)[0]
    assert contra["status"] == "contradicted"


def test_decide_waits_then_attributes(harness):
    h = harness()
    v, ch = h.app.verifier, h.cfg.channels["football_ru"]
    reported = {"status": "reported", "contradicted": False}
    assert v.decide(reported, _story(0.2), {"checks": 0}, ch, time.time()) == "waiting"
    assert v.decide(reported, _story(0.2), {"checks": 2}, ch, time.time()) == "reported"
    rumor = {"status": "rumor", "contradicted": False}
    assert v.decide(rumor, _story(5), {"checks": 0}, ch, time.time()) == "rumor"
    ch.verification.allow_rumors = False
    assert v.decide(rumor, _story(5), {"checks": 0}, ch, time.time()) == "rejected"
    assert v.decide({"status": "contradicted", "contradicted": True}, _story(0.1), {"checks": 0}, ch,
                    time.time()) == "rejected"


def test_grounding_counts_only_whitelisted_domains(harness):
    h = harness()
    ch = h.cfg.channels["football_ru"]
    assert h.app.domain_info("www.arsenal.com", ch)["tier"] == 1
    assert h.app.domain_info("news.bbc.test", ch)["group"] == "bbc"  # configured source domain
    assert h.app.domain_info("random-blog.com", ch) is None


def test_unreliable_source_gets_demoted(harness):
    h = harness()
    src = h.app.source("bbc")
    assert h.app.effective_tier(src) == 2
    h.db.update_source("bbc", alpha=2, beta=9)
    assert h.app.effective_tier(src) == 3


# --------------------------------------------------------------------------- reviewer guards
def test_reviewer_guards(harness):
    h = harness()
    r, ch = h.app.reviewer, h.cfg.channels["football_ru"]
    sheet = {"status": "rumor", "facts": [{"text": "50 млн"}], "attribution": ["Fabrizio Romano"],
             "links": ["https://bbc.test/a"]}
    issues = r.guards(ch, 'Смит перейдёт за 50 млн <a href="https://evil.test">тут</a>', sheet, False)
    text = " ".join(issues)
    assert "источник" in text and "слух" in text and "evil.test" in text
    ok = r.guards(ch, 'Слух: по данным Fabrizio Romano, Смит перейдёт за 50 млн. '
                      '<a href="https://bbc.test/a">Источник</a>', sheet, False)
    assert ok == []
    assert any("запрещённая" in i for i in r.guards(ch, "Это верняк на сегодня", {"status": "confirmed"}, False))
    assert any("лимита" in i for i in r.guards(ch, "x" * 1100, {"status": "confirmed"}, True))


# --------------------------------------------------------------------------- moderator
def _msg(uid, text, mid=1, **kw):
    return {"message_id": mid, "chat": {"id": -100500, "type": "supergroup"}, "from": {"id": uid}, "text": text, **kw}


def test_moderator_bans_new_spammer_and_keeps_fans(harness):
    h = harness({"модератор комментариев": {"label": "ok", "severity": 0}})
    app, web = h.app, h.web
    ch = app.cfg.channels["football_ru"]
    assert run(app.moderator.handle(app.control_bot, ch, _msg(42, "Заходи t.me/freebets"))) == "ban"
    assert web.calls("deleteMessage") and web.calls("banChatMember")
    assert run(app.moderator.handle(app.control_bot, ch, _msg(43, "Судья опять убил игру, кошмар", 2))) == "ok"
    assert run(app.moderator.handle(app.control_bot, ch, _msg(5, "t.me/admin-link", 3))) == "admin"
    assert run(app.moderator.handle(app.control_bot, ch, {**_msg(0, "post", 4), "is_automatic_forward": True})) \
        == "channel_post"


def test_moderator_llm_catches_toxicity_and_escalates(harness):
    h = harness({"модератор комментариев": {"label": "toxic", "severity": 2, "reason": "оскорбление"}})
    app = h.app
    ch = app.cfg.channels["football_ru"]
    assert run(app.moderator.handle(app.control_bot, ch, _msg(50, "ты дебил", 1))) == "delete"
    assert run(app.moderator.handle(app.control_bot, ch, _msg(50, "ты дебил", 2))) == "mute24h"
    assert h.db.scalar("SELECT strikes FROM mod_users WHERE user_id=50") == 2


def test_moderator_flood(harness):
    h = harness({"модератор комментариев": {"label": "ok", "severity": 0}})
    app = h.app
    ch = app.cfg.channels["football_ru"]
    results = [run(app.moderator.handle(app.control_bot, ch, _msg(60, f"гол {i}", i))) for i in range(7)]
    assert "mute10m" in results


def test_discussion_messages_are_routed_to_moderator(harness):
    h = harness({"модератор комментариев": {"label": "ok", "severity": 0}})
    run(h.app.control.dispatch(h.app.control_bot, {"update_id": 1, "message": _msg(70, "реклама в лс https://x.ru")}))
    assert h.db.scalar("SELECT action FROM mod_actions") == "ban"


# --------------------------------------------------------------------------- learning loop
def _awaiting(db, draft="<b>Черновик</b> текст"):
    sheet = {"status": "confirmed", "facts": [], "attribution": [], "links": []}
    return db.new_post("football_ru", None, "awaiting", facts=sheet, final=draft, draft=draft, score=80, confidence=0.9)


def test_owner_edit_is_published_and_turned_into_lessons(harness):
    h = harness({"наставник команды": {"lessons": [{"role": "writer", "text": "Не используй оценочные эпитеты, только факты"}],
                                       "reinforce": []}})
    app, db = h.app, h.db
    pid = _awaiting(db)
    run(app.control.dispatch(app.control_bot, {"update_id": 1, "callback_query": {
        "id": "c", "from": {"id": OWNER}, "data": f"ed:{pid}", "message": {"message_id": 9}}}))
    assert db.get("pending:edit") == pid

    async def edit_and_wait():
        await app.control.dispatch(app.control_bot, {"update_id": 2, "message": {
            "message_id": 10, "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER},
            "text": "Смит подписал контракт", "entities": [{"type": "bold", "offset": 0, "length": 4}]}})
        for t in list(app.tasks):
            await t

    run(edit_and_wait())
    p = db.post(pid)
    assert p["status"] == "scheduled" and p["owner_action"] == "edited"
    assert p["final"] == "<b>Смит</b> подписал контракт"
    [lesson] = db.lessons("football_ru", "writer")
    assert "эпитет" in lesson["text"]
    # the lesson is now part of the writer's prompt
    assert "эпитет" in app.writer.system(app.cfg.channels["football_ru"])


def test_reject_with_reason_and_teach_command(harness):
    h = harness({"наставник команды": {"lessons": [{"role": "triage", "text": "Не бери новости о низших дивизионах Англии"}]}})
    app, db = h.app, h.db
    pid = _awaiting(db)

    async def flow():
        await app.control.dispatch(app.control_bot, {"update_id": 1, "callback_query": {
            "id": "c", "from": {"id": OWNER}, "data": f"rj:{pid}", "message": {"message_id": 9}}})
        await app.control.dispatch(app.control_bot, {"update_id": 2, "message": {
            "message_id": 11, "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}, "text": "Лига 2 никому не интересна"}})
        for t in list(app.tasks):
            await t
        return await app.control.run_command("/teach", ["all", "writer", "Всегда указывай счёт в первой строке"],
                                             "/teach all writer Всегда указывай счёт в первой строке")

    reply = run(flow())
    assert db.post(pid)["status"] == "rejected"
    assert db.lessons("football_ru", "triage")[0]["origin"] == "owner_reject"
    assert "Правило" in reply and db.lessons("esports_ru", "writer")[0]["origin"] == "owner_rule"


def test_non_owner_cannot_approve(harness):
    h = harness()
    pid = _awaiting(h.db)
    run(h.app.control.dispatch(h.app.control_bot, {"update_id": 1, "callback_query": {
        "id": "c", "from": {"id": 1234}, "data": f"ap:{pid}", "message": {"message_id": 9}}}))
    assert h.db.post(pid)["status"] == "awaiting"


def test_error_report_penalizes_sources(harness):
    h = harness({"наставник команды": {"lessons": []}})
    pid = h.db.new_post("football_ru", None, "published", final="x", sources=["bbc"], facts={"status": "confirmed"})
    run(h.app.learner.learn_from_error(pid, "сумма трансфера была другой"))
    assert h.db.scalar("SELECT beta FROM sources_state WHERE source_id='bbc'") == 3


def test_autopilot_suggested_after_clean_streak(harness):
    h = harness(network_overrides={"learning": {"autopilot_after_decisions": 3, "autopilot_min_clean_rate": 0.9}})
    for _ in range(3):
        h.db.new_post("football_ru", None, "published", owner_action="approved")
    run(h.app.learner.maybe_suggest_autopilot("football_ru"))
    msg = h.web.calls("sendMessage")[-1]
    assert msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "auto:football_ru"
    run(h.app.control.dispatch(h.app.control_bot, {"update_id": 3, "callback_query": {
        "id": "c", "from": {"id": OWNER}, "data": "auto:football_ru", "message": {"message_id": 1}}}))
    assert h.app.autonomy(h.cfg.channels["football_ru"]) == "auto"


# --------------------------------------------------------------------------- HR + specialists
SPEC = {"name": "weekly_transfers", "channel_id": "football_ru", "title": "Трансферы недели",
        "schedule": {"every_hours": 24}, "lookback_hours": 48, "min_posts": 2, "instructions": "Список трансферов"}


def test_hr_proposals_are_validated_and_applied(harness):
    h = harness({"HR-директор": {"proposals": [
        {"kind": "create_agent", "spec": SPEC, "rationale": "трансферы заходят в 2 раза лучше"},
        {"kind": "tune_channel", "channel_id": "football_ru", "key": "triage_threshold", "value": 70, "rationale": "r"},
        {"kind": "tune_channel", "channel_id": "football_ru", "key": "triage_threshold", "value": 5, "rationale": "bad"},
        {"kind": "delete_everything"}]}})
    app, db = h.app, h.db
    assert run(app.hr.run()) == 2
    asks = [c for c in h.web.calls("sendMessage") if "hr_ok" in json.dumps(c.get("reply_markup"))]
    assert len(asks) == 2
    for p in db.all("SELECT id FROM proposals"):
        run(app.hr.apply(p["id"]))
    assert db.one("SELECT status, origin FROM agents WHERE name='weekly_transfers'")["origin"] == "hr"
    assert app.cfg.channels["football_ru"].triage_threshold == 70
    assert db.get("override:football_ru:triage_threshold") == 70


def test_specialist_writes_from_verified_posts_only(harness):
    h = harness({"агент-специалист": {"text": "<b>Трансферы недели</b>\n1. Смит – Арсенал, 50 млн"}})
    app, db = h.app, h.db
    db.x("INSERT INTO agents(name,channel_id,spec,status,origin,created_at) VALUES(?,?,?,?,?,?)",
         ("weekly_transfers", "football_ru", json.dumps(SPEC), "active", "hr", time.time()))
    for i, status in enumerate(["confirmed", "reported", "rumor"]):
        db.new_post("football_ru", None, "published", kind="news", category="transfer", published_at=time.time(),
                    facts={"status": status, "headline": f"h{i}", "facts": [{"text": f"fact {i} 50 млн"}],
                           "attribution": ["BBC"]}, score=80)
    assert run(app.specialists.tick()) == 1
    p = db.one("SELECT * FROM posts WHERE kind='specialist'")
    assert p["status"] == "drafted"
    material = h.llm.calls[-1][1]
    assert "fact 0" in material and "fact 1" in material and "fact 2" not in material  # rumors never reused
    assert run(app.specialists.tick()) == 0  # not due again


# --------------------------------------------------------------------------- ads
def test_ad_marking_and_quiet_window(harness):
    h = harness()
    app, db = h.app, h.db
    slot = AdSlot(id="bk", text="<b>Бонус</b>", advertiser="ООО «Тест», ИНН 1", erid="2Vtz", times=["00:00"],
                  quiet_after_minutes=60)
    assert marked(slot).endswith("<i>Реклама. ООО «Тест», ИНН 1. erid: 2Vtz</i>")
    ch = app.cfg.channels["football_ru"]
    run(app.ads.publish(ch, slot))
    assert db.get("ad_quiet_until:football_ru") > time.time()
    pid = db.new_post("football_ru", None, "scheduled", final="новость", scheduled_at=0, score=95)
    run(app.publisher.tick())
    assert db.post(pid)["status"] == "scheduled"  # even breaking news waits out the paid window


def test_report_mentions_channels_and_spend(harness):
    h = harness()
    text = h.app.analyst.report()
    assert "Футбол" in text and "LLM сегодня" in text


def test_llm_outage_does_not_block_moderation(harness):
    h = harness()
    h.router.providers["fake"] = FakeLLM(fail=100)
    h.router.providers["fake2"] = FakeLLM(fail=100)
    ch = h.app.cfg.channels["football_ru"]
    assert run(h.app.moderator.handle(h.app.control_bot, ch, _msg(80, "обычный комментарий без ссылок"))) == "ok"


# --------------------------------------------------------------------------- orchestration
def test_supervisor_survives_crashing_loop(harness):
    import asyncio

    h = harness()
    app = h.app
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("source exploded")
        app.stop.set()

    async def go():
        real_wait_for = asyncio.wait_for

        async def fast_wait_for(aw, timeout):  # skip the backoff sleep
            return await real_wait_for(aw, min(timeout, 0.01))

        asyncio.wait_for = fast_wait_for
        try:
            await app.supervised("flaky", flaky, 0, 5)
        finally:
            asyncio.wait_for = real_wait_for

    run(go())
    assert calls["n"] == 2
    assert h.db.scalar("SELECT msg FROM events WHERE component='flaky'") == "RuntimeError: source exploded"
    assert "flaky" in app.loop_ages()


def test_crashed_process_leases_are_released_on_startup(harness):
    h = harness()
    pid = h.db.new_post("football_ru", None, "drafted", lease_until=time.time() + 600)
    run(h.app.startup())
    assert h.db.post(pid)["lease_until"] == 0
