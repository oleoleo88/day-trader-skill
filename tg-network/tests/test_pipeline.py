"""End to end: sources -> scout -> editor -> fact-check -> writer -> reviewer -> owner approval -> channel."""

import json
import re
import time

from conftest import OWNER, rss, run

NEWS_BBC = {"title": "Arsenal sign Smith until 2029", "link": "https://bbc.test/news/1",
            "desc": "Arsenal have signed Smith on a deal until 2029 for 50 million pounds."}
NEWS_GUARDIAN = {"title": "Arsenal complete signing of Smith until 2029", "link": "https://guardian.test/a/2",
                 "desc": "Smith joins Arsenal until 2029, fee 50 million."}


def editor_reply(system, user):
    ids = [int(x) for x in re.findall(r'"id":(\d+)', user)]
    return {"stories": [{"id": i, "score": 85, "category": "transfer", "duplicate_of_recent": False, "reason": "топ"}
                        for i in ids]}


def judge_reply(system, user):
    refs = sorted(set(re.findall(r"\[(E\d+)\]", user)))
    return {"claims": [{"i": 0, "support": refs, "contradict": []}],
            "facts": [{"text": "Арсенал подписал Смита до 2029 года за 50 млн фунтов", "evidence": refs[:1]}],
            "quotes": [], "notes": ""}


SCRIPT = {
    "главный редактор": editor_reply,
    "разобрать материалы одного сюжета": {"headline": "Арсенал подписал Смита", "main_claim": "Арсенал подписал Смита до 2029",
                                          "claims": [], "keywords": ["Smith", "Смит", "Arsenal"], "rumor_language": False},
    "пронумерованные доказательства": judge_reply,
    "Ты – автор": {"text": "<b>🔁 Арсенал подписал Смита</b>\nКонтракт до 2029 года, сумма – 50 млн фунтов.",
                   "use_image": False},
    "выпускающий редактор": {"verdict": "approve", "issues": [], "fixed_text": "", "confidence": 0.93},
}


def test_full_pipeline_publishes_after_owner_approval(harness):
    h = harness(SCRIPT)
    app, web, db = h.app, h.web, h.db
    old = {"title": "Old news", "link": "https://bbc.test/news/0", "ts": time.time() - 3600}
    web.pages = {"https://bbc.test/rss": rss([old]), "https://guardian.test/rss": rss([]),
                 "https://insider.test/rss": rss([])}

    # first poll only bootstraps (no flood of old news on first start)
    run(app.scout.tick())
    assert db.scalar("SELECT COUNT(*) FROM stories") == 0

    web.pages["https://bbc.test/rss"] = rss([old, NEWS_BBC])
    web.pages["https://guardian.test/rss"] = rss([NEWS_GUARDIAN])
    db.x("UPDATE sources_state SET last_poll=0")
    assert run(app.scout.tick()) == 2
    assert db.scalar("SELECT COUNT(*) FROM stories") == 1, "two outlets, one story"

    assert run(app.editor.tick()) == 1
    assert run(app.verifier.tick()) == 1
    p = db.one("SELECT * FROM posts")
    assert p["status"] == "verified", p["owner_note"]
    sheet = json.loads(p["facts"])
    assert sheet["status"] == "confirmed"
    assert set(sheet["attribution"]) == {"BBC Sport", "The Guardian"}

    run(app.writer.tick())
    run(app.reviewer.tick())
    assert db.post(p["id"])["status"] == "ready"

    run(app.publisher.tick())
    assert db.post(p["id"])["status"] == "awaiting"
    approval = web.calls("sendMessage")[-1]
    assert approval["chat_id"] == OWNER
    assert "Арсенал подписал Смита" in approval["text"]
    assert approval["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"ap:{p['id']}"

    run(app.control.dispatch(app.control_bot, {"update_id": 1, "callback_query": {
        "id": "cb", "from": {"id": OWNER}, "data": f"ap:{p['id']}", "message": {"message_id": 5}}}))
    assert db.post(p["id"])["status"] == "scheduled"

    run(app.publisher.tick())
    post = db.post(p["id"])
    assert post["status"] == "published"
    sent = [c for c in web.calls("sendMessage") if c["chat_id"] == "@football"]
    assert len(sent) == 1 and "50 млн" in sent[0]["text"]
    # confirmed sources gain reliability
    assert db.scalar("SELECT alpha FROM sources_state WHERE source_id='bbc'") == 2


def test_hallucinated_number_is_sent_back_to_writer(harness):
    script = dict(SCRIPT)
    script["Ты – автор"] = {"text": "<b>Арсенал подписал Смита</b>\nКонтракт на 7 лет, сумма – 50 млн."}
    h = harness(script)
    app, db = h.app, h.db
    sheet = {"status": "confirmed", "facts": [{"text": "Контракт до 2029, 50 млн", "sources": ["BBC"], "tier": 2}],
             "attribution": ["BBC"], "links": []}
    pid = db.new_post("football_ru", None, "verified", facts=sheet, score=80, confidence=0.95)
    run(app.writer.tick())
    run(app.reviewer.tick())
    p = db.post(pid)
    assert p["status"] == "revise"
    assert "7" in json.loads(p["review"])["issues"][0]


def test_autopilot_publishes_confirmed_without_owner(harness):
    h = harness(SCRIPT, channel_overrides={"autonomy": "auto"})
    app, db, web = h.app, h.db, h.web
    sheet = {"status": "confirmed", "facts": [{"text": "x 2029 50", "sources": ["BBC"], "tier": 2}],
             "attribution": ["BBC"], "links": []}
    pid = db.new_post("football_ru", None, "ready", facts=sheet, final="<b>Новость</b> 2029", score=80, confidence=0.95)
    run(app.publisher.tick())
    assert db.post(pid)["status"] == "published"
    assert not [c for c in web.calls("sendMessage") if c["chat_id"] == OWNER]


def test_autopilot_still_asks_owner_about_rumors(harness):
    h = harness(SCRIPT, channel_overrides={"autonomy": "auto"})
    app, db = h.app, h.db
    sheet = {"status": "rumor", "facts": [], "attribution": ["Insider"], "links": []}
    pid = db.new_post("football_ru", None, "ready", facts=sheet, final="Слух: …", score=80, confidence=0.95)
    run(app.publisher.tick())
    assert db.post(pid)["status"] == "awaiting"


def test_stale_post_expires_instead_of_publishing(harness):
    h = harness(SCRIPT)
    app, db = h.app, h.db
    sid = db.x("INSERT INTO stories(pack,title,lang,simhash,first_seen,last_seen,best_tier) VALUES('football','t','en','0',?,?,2)",
               (time.time() - 20 * 3600, time.time())).lastrowid
    pid = db.new_post("football_ru", sid, "scheduled", final="x", scheduled_at=0, score=70)
    run(app.publisher.tick())
    assert db.post(pid)["status"] == "expired"


def test_quiet_hours_hold_normal_posts_but_not_breaking(harness):
    h = harness(SCRIPT, channel_overrides={"schedule": {"quiet_hours": "00:00-23:59", "min_interval_min": 0}})
    app, db = h.app, h.db
    normal = db.new_post("football_ru", None, "scheduled", final="обычная", scheduled_at=0, score=70)
    run(app.publisher.tick())
    assert db.post(normal)["status"] == "scheduled"
    breaking = db.new_post("football_ru", None, "scheduled", final="срочно", scheduled_at=0, score=95)
    run(app.publisher.tick())
    assert db.post(breaking)["status"] == "published"
