import threading
import unittest
from datetime import datetime, timezone

from night_market_foundation.api import route
from night_market_foundation.clock import FixedClock
from night_market_foundation.errors import (ConflictError, NotFoundError,
                                            PermissionDenied, ValidationError)
from night_market_foundation.quiz import QuizService
from night_market_foundation.storage import Database


class QuizTestCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = QuizService(self.database, FixedClock(
            datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="活动机构一")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                    display_name="操作员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="文化展示区",
                                   timezone_name="Asia/Shanghai")
        self._request_counter = 0

    def tearDown(self):
        self.database.close()

    def next_request_id(self, prefix):
        self._request_counter += 1
        return f"{prefix}-{self._request_counter}"

    @staticmethod
    def default_questions():
        return [
            {"question_id": "q1", "level": 1, "position": 1,
             "prompt": "清热解毒、花色由白转黄的药材？", "answer_key": "金银花",
             "points": 10, "min_age": 0, "knowledge_source": "《本草纲目·草部》",
             "hints": [{"hint_id": "h1", "text": "忍冬科植物", "min_age": 0},
                       {"hint_id": "h2", "text": "初开为白后转黄", "min_age": 6},
                       {"hint_id": "h3", "text": "炒炭可止血", "min_age": 12}]},
            {"question_id": "q2", "level": 1, "position": 2,
             "prompt": "滋补肝肾、益精明目的红色小果？", "answer_key": "枸杞",
             "points": 10, "min_age": 0, "knowledge_source": "《神农本草经》",
             "hints": [{"hint_id": "h1", "text": "宁夏道地药材", "min_age": 0}]},
            {"question_id": "q3", "level": 2, "position": 1,
             "prompt": "补血活血、调经止痛的常用药？", "answer_key": "当归",
             "points": 20, "min_age": 12, "knowledge_source": "《金匮要略》",
             "hints": [{"hint_id": "h1", "text": "岷县道地药材", "min_age": 12}]},
            {"question_id": "q4", "level": 2, "position": 2,
             "prompt": "补气固表、托毒生肌的药材？", "answer_key": "黄芪",
             "points": 20, "min_age": 0, "knowledge_source": "《神农本草经》",
             "hints": [{"hint_id": "h1", "text": "蒙古黄芪为正品", "min_age": 0}]},
        ]

    def make_bank(self, bank_id="bank1", questions=None):
        self.service.create_bank(request_id=self.next_request_id("bank"), actor_id="op1",
                                 site_id="s1", bank_id=bank_id, title="药材知识题库")
        for question in questions if questions is not None else self.default_questions():
            self.service.add_question(request_id=self.next_request_id("question"),
                                      actor_id="op1", bank_id=bank_id, **question)
        self.service.freeze_bank(request_id=self.next_request_id("freeze"),
                                 actor_id="op1", bank_id=bank_id)
        return bank_id

    def make_participant(self, alias, age, consent=True, team_id=None):
        receipt = self.service.register_participant(
            request_id=self.next_request_id("participant"), actor_id="op1", site_id="s1",
            alias=alias, age=age, consent_public=consent, team_id=team_id,
            participant_id=f"p{self._request_counter}")
        return receipt.resource_id

    def make_session(self, participant_id, bank_id="bank1", session_id=None):
        receipt = self.service.start_session(
            request_id=self.next_request_id("session"), actor_id="op1",
            participant_id=participant_id, bank_id=bank_id,
            session_id=session_id or f"session-{self._request_counter}")
        return receipt.resource_id

    @staticmethod
    def answer(seq, question_id, answer):
        return {"client_seq": seq, "kind": "answer", "question_id": question_id,
                "payload": {"answer": answer}}


class BankLifecycleTest(QuizTestCase):
    def test_freeze_requires_questions_and_blocks_edits(self):
        self.service.create_bank(request_id="bank-empty", actor_id="op1", site_id="s1",
                                 bank_id="bank-empty", title="空题库")
        with self.assertRaises(ValidationError):
            self.service.freeze_bank(request_id="freeze-empty", actor_id="op1",
                                     bank_id="bank-empty")
        self.service.add_question(request_id="q-empty", actor_id="op1", bank_id="bank-empty",
                                  **self.default_questions()[0])
        self.service.freeze_bank(request_id="freeze-ok", actor_id="op1", bank_id="bank-empty")
        with self.assertRaises(ConflictError):
            self.service.add_question(request_id="q-late", actor_id="op1",
                                      bank_id="bank-empty", **self.default_questions()[1])
        bank = self.service.get_bank("bank-empty")
        self.assertEqual("frozen", bank["status"])
        self.assertEqual(1, len(bank["questions"]))
        self.assertEqual("《本草纲目·草部》", bank["questions"][0]["knowledge_source"])

    def test_frozen_bank_keeps_knowledge_source_and_age(self):
        self.make_bank()
        bank = self.service.get_bank("bank1")
        q3 = next(q for q in bank["questions"] if q["question_id"] == "q3")
        self.assertEqual(12, q3["min_age"])
        self.assertEqual("《金匮要略》", q3["knowledge_source"])

    def test_draft_bank_cannot_start_session(self):
        self.service.create_bank(request_id="bank-draft", actor_id="op1", site_id="s1",
                                 bank_id="bank-draft", title="草稿题库")
        self.service.add_question(request_id="q-draft", actor_id="op1", bank_id="bank-draft",
                                  **self.default_questions()[0])
        participant = self.make_participant("体验者", 30)
        with self.assertRaises(ValidationError):
            self.make_session(participant, bank_id="bank-draft")

    def test_freeze_replay_uses_first_result(self):
        self.service.create_bank(request_id="bank-replay", actor_id="op1", site_id="s1",
                                 bank_id="bank-replay", title="幂等题库")
        self.service.add_question(request_id="q-replay", actor_id="op1",
                                  bank_id="bank-replay", **self.default_questions()[0])
        first = self.service.freeze_bank(request_id="freeze-replay", actor_id="op1",
                                         bank_id="bank-replay")
        self.assertFalse(first.replayed)
        again = self.service.freeze_bank(request_id="freeze-replay", actor_id="op1",
                                         bank_id="bank-replay")
        self.assertTrue(again.replayed)
        with self.assertRaises(ConflictError):
            self.service.freeze_bank(request_id="freeze-other", actor_id="op1",
                                     bank_id="bank-replay")


class EventMergeTest(QuizTestCase):
    def test_out_of_order_batches_produce_same_score(self):
        self.make_bank()
        session_a = self.make_session(self.make_participant("甲队", 30))
        session_b = self.make_session(self.make_participant("乙队", 30))
        events = [
            self.answer(1, "q1", "金银花"),
            {"client_seq": 2, "kind": "hint", "question_id": "q2", "payload": {"hint_id": "h1"}},
            self.answer(3, "q2", "枸杞"),
            {"client_seq": 4, "kind": "skip", "question_id": "q4", "payload": {}},
            self.answer(5, "q4", "黄芪"),
        ]
        self.service.submit_events(actor_id="op1", session_id=session_a, events=events)
        shuffled = [events[3], events[1], events[4], events[0], events[2]]
        self.service.submit_events(actor_id="op1", session_id=session_b, events=shuffled[:2])
        self.service.submit_events(actor_id="op1", session_id=session_b, events=shuffled[2:])
        score_a = self.service.session_score(session_a)
        score_b = self.service.session_score(session_b)
        self.assertEqual(40, score_a["score"])
        self.assertEqual(score_a["score"], score_b["score"])
        self.assertEqual([e["client_seq"] for e in score_a["effective_events"]],
                         [e["client_seq"] for e in score_b["effective_events"]])
        self.assertEqual(self.service.session_progress(session_a)["levels"],
                         self.service.session_progress(session_b)["levels"])

    def test_duplicate_upload_reuses_first_result(self):
        self.make_bank()
        session = self.make_session(self.make_participant("重传家庭", 30))
        events = [self.answer(1, "q1", "金银花"), self.answer(2, "q2", "枸杞")]
        first = self.service.submit_events(actor_id="op1", session_id=session, events=events)
        second = self.service.submit_events(actor_id="op1", session_id=session, events=events)
        self.assertEqual(["recorded", "recorded"],
                         [item["status"] for item in first["results"]])
        self.assertEqual(["replayed", "replayed"],
                         [item["status"] for item in second["results"]])
        self.assertEqual([item["event_id"] for item in first["results"]],
                         [item["event_id"] for item in second["results"]])
        log = self.service.session_events(session)
        self.assertEqual(2, len(log["events"]))
        self.assertEqual(20, self.service.session_score(session)["score"])

    def test_resume_after_reconnect(self):
        self.make_bank()
        session = self.make_session(self.make_participant("断点家庭", 30))
        self.service.submit_events(actor_id="op1", session_id=session,
                                   events=[self.answer(1, "q1", "金银花"),
                                           self.answer(2, "q2", "枸杞"),
                                           self.answer(3, "q4", "黄芪")])
        sync = self.service.session_sync(session)
        self.assertEqual([1, 2, 3], sync["recorded_seqs"])
        self.assertEqual(4, sync["next_seq"])
        resent = self.service.submit_events(
            actor_id="op1", session_id=session,
            events=[self.answer(2, "q2", "枸杞"), self.answer(3, "q4", "黄芪"),
                    self.answer(4, "q1", "金银花"), self.answer(5, "q2", "枸杞")])
        self.assertEqual(["replayed", "replayed", "recorded", "recorded"],
                         [item["status"] for item in resent["results"]])
        sync = self.service.session_sync(session)
        self.assertEqual([1, 2, 3, 4, 5], sync["recorded_seqs"])
        self.assertEqual(6, sync["next_seq"])

    def test_score_lists_adopted_effective_events(self):
        self.make_bank()
        session = self.make_session(self.make_participant("改答案家庭", 30))
        self.service.submit_events(
            actor_id="op1", session_id=session,
            events=[self.answer(1, "q1", "人参"),
                    {"client_seq": 2, "kind": "hint", "question_id": "q1",
                     "payload": {"hint_id": "h1"}},
                    self.answer(3, "q1", "金银花")])
        score = self.service.session_score(session)
        self.assertEqual(0, score["score"])
        self.assertEqual([1], [e["client_seq"] for e in score["effective_events"]])
        self.assertEqual("人参", score["effective_events"][0]["answer"])
        self.assertFalse(score["effective_events"][0]["correct"])
        log = self.service.session_events(session)
        self.assertEqual(3, len(log["events"]))
        settlement = self.service.settle_session(actor_id="op1", session_id=session)
        self.assertEqual([e["event_id"] for e in score["effective_events"]],
                         [e["event_id"] for e in settlement["effective_events"]])
        settled_score = self.service.session_score(session)
        self.assertEqual(settlement["effective_events"], settled_score["effective_events"])


class ConflictTest(QuizTestCase):
    def test_same_sequence_fork_is_kept_for_operator(self):
        self.make_bank()
        session = self.make_session(self.make_participant("分叉家庭", 30))
        self.service.submit_events(actor_id="op1", session_id=session,
                                   events=[self.answer(1, "q1", "金银花")])
        fork = self.service.submit_events(actor_id="op1", session_id=session,
                                          events=[self.answer(1, "q1", "黄芪")])
        self.assertEqual("conflict", fork["results"][0]["status"])
        again = self.service.submit_events(actor_id="op1", session_id=session,
                                           events=[self.answer(1, "q1", "黄芪")])
        self.assertEqual("conflict_replayed", again["results"][0]["status"])
        conflicts = self.service.list_conflicts(session_id=session, status="pending")
        self.assertEqual(1, len(conflicts))
        self.assertEqual(1, len(conflicts[0]["contenders"]))
        self.assertEqual("黄芪", conflicts[0]["contenders"][0]["payload"]["answer"])
        with self.assertRaises(ConflictError):
            self.service.settle_session(actor_id="op1", session_id=session)
        self.service.resolve_conflict(request_id="resolve-1", actor_id="op1",
                                      conflict_id=conflicts[0]["conflict_id"],
                                      decision="keep_first")
        replay = self.service.resolve_conflict(request_id="resolve-1", actor_id="op1",
                                               conflict_id=conflicts[0]["conflict_id"],
                                               decision="keep_first")
        self.assertTrue(replay.replayed)
        self.assertEqual(10, self.service.session_score(session)["score"])
        settlement = self.service.settle_session(actor_id="op1", session_id=session)
        self.assertEqual(10, settlement["score"])
        valid, _count = self.service.verify_audit()
        self.assertTrue(valid)

    def test_operator_can_adopt_contender(self):
        self.make_bank()
        session = self.make_session(self.make_participant("纠错家庭", 30))
        self.service.submit_events(actor_id="op1", session_id=session,
                                   events=[self.answer(1, "q1", "人参")])
        self.service.submit_events(actor_id="op1", session_id=session,
                                   events=[self.answer(1, "q1", "金银花")])
        conflict = self.service.list_conflicts(session_id=session)[0]
        contender_hash = conflict["contenders"][0]["content_hash"]
        self.service.resolve_conflict(request_id="resolve-2", actor_id="op1",
                                      conflict_id=conflict["conflict_id"],
                                      decision="use_contender",
                                      contender_hash=contender_hash)
        score = self.service.session_score(session)
        self.assertEqual(10, score["score"])
        self.assertEqual("金银花", score["effective_events"][0]["answer"])
        log = self.service.session_events(session)
        statuses = sorted(event["status"] for event in log["events"])
        self.assertEqual(["effective", "superseded"], statuses)
        self.assertEqual("resolved", log["conflicts"][0]["status"])
        with self.assertRaises(ConflictError):
            self.service.resolve_conflict(request_id="resolve-again", actor_id="op1",
                                          conflict_id=conflict["conflict_id"],
                                          decision="keep_first")

    def test_invalid_contender_cannot_be_adopted(self):
        self.make_bank()
        session = self.make_session(self.make_participant("异常家庭", 30))
        self.service.submit_events(actor_id="op1", session_id=session,
                                   events=[self.answer(1, "q1", "金银花")])
        self.service.submit_events(actor_id="op1", session_id=session,
                                   events=[self.answer(1, "q9", "金银花")])
        conflict = self.service.list_conflicts(session_id=session)[0]
        with self.assertRaises(ValidationError):
            self.service.resolve_conflict(request_id="resolve-bad", actor_id="op1",
                                          conflict_id=conflict["conflict_id"],
                                          decision="use_contender",
                                          contender_hash=conflict["contenders"][0]["content_hash"])
        self.assertEqual(1, self.service.list_conflicts(session_id=session,
                                                        status="pending")[0]["client_seq"])


class BankVersionTest(QuizTestCase):
    def test_withdrawal_only_affects_new_sessions(self):
        self.make_bank()
        old_session = self.make_session(self.make_participant("旧版家庭", 30))
        self.service.submit_events(actor_id="op1", session_id=old_session,
                                   events=[self.answer(1, "q3", "当归"),
                                           self.answer(2, "q1", "金银花")])
        receipt = self.service.withdraw_question(request_id="withdraw-1", actor_id="op1",
                                                 bank_id="bank1", question_id="q3",
                                                 new_bank_id="bank2")
        self.assertEqual("bank2", receipt.resource_id)
        self.assertEqual(2, self.service.get_bank("bank2")["version"])
        with self.assertRaises(ConflictError):
            self.make_session(self.make_participant("迟到家庭", 30), bank_id="bank1")
        new_session = self.make_session(self.make_participant("新版家庭", 30), bank_id="bank2")
        old_progress = self.service.session_progress(old_session)
        new_progress = self.service.session_progress(new_session)
        old_level2 = next(l for l in old_progress["levels"] if l["level"] == 2)
        new_level2 = next(l for l in new_progress["levels"] if l["level"] == 2)
        self.assertEqual(2, old_level2["questions"])
        self.assertEqual(1, new_level2["questions"])
        self.assertEqual(30, self.service.session_score(old_session)["score"])
        banks = self.service.list_banks("s1")
        self.assertEqual([1, 2], [b["version"] for b in banks])

    def test_withdrawal_does_not_rewrite_settled_scores(self):
        self.make_bank()
        session = self.make_session(self.make_participant("已结算家庭", 30))
        self.service.submit_events(actor_id="op1", session_id=session,
                                   events=[self.answer(1, "q1", "金银花"),
                                           self.answer(2, "q2", "枸杞"),
                                           self.answer(3, "q3", "当归")])
        settlement = self.service.settle_session(actor_id="op1", session_id=session)
        self.assertEqual(40, settlement["score"])
        self.service.withdraw_question(request_id="withdraw-2", actor_id="op1",
                                       bank_id="bank1", question_id="q3",
                                       new_bank_id="bank2")
        score = self.service.session_score(session)
        self.assertEqual(40, score["score"])
        self.assertEqual(3, len(score["effective_events"]))
        replay = self.service.settle_session(actor_id="op1", session_id=session)
        self.assertTrue(replay["replayed"])
        self.assertEqual(40, replay["score"])
        with self.assertRaises(ConflictError):
            self.service.submit_events(actor_id="op1", session_id=session,
                                       events=[self.answer(4, "q4", "黄芪")])

    def test_withdrawal_requires_latest_frozen_version(self):
        self.make_bank()
        self.service.withdraw_question(request_id="withdraw-3", actor_id="op1",
                                       bank_id="bank1", question_id="q3",
                                       new_bank_id="bank2")
        with self.assertRaises(ConflictError):
            self.service.withdraw_question(request_id="withdraw-4", actor_id="op1",
                                           bank_id="bank1", question_id="q4",
                                           new_bank_id="bank3")
        receipt = self.service.withdraw_question(request_id="withdraw-5", actor_id="op1",
                                                 bank_id="bank2", question_id="q4",
                                                 new_bank_id="bank3")
        self.assertFalse(receipt.replayed)
        self.assertEqual(3, self.service.get_bank("bank3")["version"])

    def test_replays_survive_state_changes(self):
        self.make_bank()
        participant = self.make_participant("重放家庭", 30)
        first = self.service.start_session(request_id="session-replay", actor_id="op1",
                                           participant_id=participant, bank_id="bank1",
                                           session_id="session-replay")
        withdraw = self.service.withdraw_question(request_id="withdraw-replay",
                                                  actor_id="op1", bank_id="bank1",
                                                  question_id="q3", new_bank_id="bank2")
        replay = self.service.start_session(request_id="session-replay", actor_id="op1",
                                            participant_id=participant, bank_id="bank1",
                                            session_id="session-replay")
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        again = self.service.withdraw_question(request_id="withdraw-replay", actor_id="op1",
                                               bank_id="bank1", question_id="q3",
                                               new_bank_id="bank2")
        self.assertTrue(again.replayed)
        self.assertEqual(withdraw.resource_id, again.resource_id)

    def test_add_question_replay_after_freeze(self):
        self.service.create_bank(request_id="bank-ar", actor_id="op1", site_id="s1",
                                 bank_id="bank-ar", title="重放题库")
        question = self.default_questions()[0]
        first = self.service.add_question(request_id="aq-replay", actor_id="op1",
                                          bank_id="bank-ar", **question)
        self.service.freeze_bank(request_id="freeze-ar", actor_id="op1", bank_id="bank-ar")
        replay = self.service.add_question(request_id="aq-replay", actor_id="op1",
                                           bank_id="bank-ar", **question)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)


class UnlockTest(QuizTestCase):
    def test_level_unlock_depends_on_prerequisites(self):
        self.make_bank()
        session = self.make_session(self.make_participant("闯关家庭", 30))
        progress = self.service.session_progress(session)
        self.assertEqual([True, False], [l["unlocked"] for l in progress["levels"]])
        self.assertEqual(1, progress["next_level"])
        self.service.submit_events(actor_id="op1", session_id=session,
                                   events=[self.answer(1, "q1", "金银花")])
        progress = self.service.session_progress(session)
        self.assertEqual([True, False], [l["unlocked"] for l in progress["levels"]])
        self.service.submit_events(actor_id="op1", session_id=session,
                                   events=[self.answer(2, "q2", "枸杞")])
        progress = self.service.session_progress(session)
        self.assertEqual([True, True], [l["unlocked"] for l in progress["levels"]])
        self.assertEqual(2, progress["next_level"])

    def test_child_level_completion_uses_applicable_questions(self):
        self.make_bank()
        session = self.make_session(self.make_participant("小朋友", 8))
        self.service.submit_events(actor_id="op1", session_id=session,
                                   events=[self.answer(1, "q1", "金银花"),
                                           self.answer(2, "q2", "枸杞"),
                                           self.answer(3, "q4", "黄芪")])
        progress = self.service.session_progress(session)
        level2 = next(l for l in progress["levels"] if l["level"] == 2)
        self.assertEqual(1, level2["questions"])
        self.assertTrue(level2["completed"])
        self.assertIsNone(progress["next_level"])
        self.assertEqual(40, self.service.session_score(session)["score"])


class PrivacyViewTest(QuizTestCase):
    def test_leaderboard_and_team_views_respect_consent_and_age(self):
        self.make_bank()
        self.service.create_team(request_id="team-1", actor_id="op1", site_id="s1",
                                 team_id="t1", name="张家")
        adult_open = self.make_participant("公开家长", 40, consent=True, team_id="t1")
        adult_shy = self.make_participant("低调家长", 35, consent=False, team_id="t1")
        child = self.make_participant("小明", 8, consent=True, team_id="t1")
        outsider = self.make_participant("路人甲", 28, consent=True)
        for participant in (adult_open, adult_shy, child, outsider):
            session = self.make_session(participant)
            self.service.submit_events(actor_id="op1", session_id=session,
                                       events=[self.answer(1, "q1", "金银花")])
        board = self.service.leaderboard("s1")
        self.assertEqual(["公开家长", "路人甲"], [row["alias"] for row in board])
        self.assertEqual(10, board[0]["score"])
        team = self.service.team_progress("t1")
        self.assertEqual({"公开家长", "小明"}, {m["alias"] for m in team["members"]})
        self.assertEqual(1, team["hidden_members"])
        self.assertEqual(10, team["members"][0]["sessions"][0]["score"])

    def test_consent_update_controls_team_sharing(self):
        self.make_bank()
        self.service.create_team(request_id="team-2", actor_id="op1", site_id="s1",
                                 team_id="t2", name="李家")
        member = self.make_participant("犹豫成员", 33, consent=False, team_id="t2")
        self.make_session(member)
        self.assertEqual(0, len(self.service.team_progress("t2")["members"]))
        self.service.update_consent(request_id="consent-1", actor_id="op1",
                                    participant_id=member, consent_public=True)
        self.assertEqual(1, len(self.service.team_progress("t2")["members"]))
        self.assertEqual(0, self.service.team_progress("t2")["hidden_members"])

    def test_child_only_sees_age_appropriate_hints(self):
        self.make_bank()
        child_session = self.make_session(self.make_participant("小红", 8))
        adult_session = self.make_session(self.make_participant("家长", 38))
        child_view = self.service.question_view(child_session, "q1")
        self.assertEqual(["h1", "h2"], [h["hint_id"] for h in child_view["hints"]])
        adult_view = self.service.question_view(adult_session, "q1")
        self.assertEqual(["h1", "h2", "h3"], [h["hint_id"] for h in adult_view["hints"]])
        self.assertNotIn("answer_key", child_view)
        with self.assertRaises(ValidationError):
            self.service.submit_events(
                actor_id="op1", session_id=child_session,
                events=[{"client_seq": 1, "kind": "hint", "question_id": "q1",
                         "payload": {"hint_id": "h3"}}])
        with self.assertRaises(PermissionDenied):
            self.service.question_view(child_session, "q3")
        with self.assertRaises(ValidationError):
            self.service.submit_events(actor_id="op1", session_id=child_session,
                                       events=[self.answer(1, "q3", "当归")])


class ConcurrencyTest(QuizTestCase):
    def test_concurrent_settlement_settles_once(self):
        self.make_bank()
        session = self.make_session(self.make_participant("并发家庭", 30))
        self.service.submit_events(actor_id="op1", session_id=session,
                                   events=[self.answer(1, "q1", "金银花"),
                                           self.answer(2, "q2", "枸杞")])

        def settle():
            return self.service.settle_session(actor_id="op1", session_id=session)

        with _Threads(8) as threads:
            results = threads.map(settle)
        fresh = [item for item in results if not item["replayed"]]
        self.assertEqual(1, len(fresh))
        self.assertEqual({20}, {item["score"] for item in results})
        count = self.database.connection.execute(
            "SELECT COUNT(*) AS count FROM quiz_settlements WHERE session_id=?",
            (session,)).fetchone()["count"]
        self.assertEqual(1, count)

    def test_concurrent_duplicate_events_record_once(self):
        self.make_bank()
        session = self.make_session(self.make_participant("抢传家庭", 30))
        event = self.answer(1, "q1", "金银花")

        def submit():
            return self.service.submit_events(actor_id="op1", session_id=session,
                                              events=[event])

        with _Threads(6) as threads:
            batches = threads.map(submit)
        statuses = [item["results"][0]["status"] for item in batches]
        self.assertEqual(1, statuses.count("recorded"))
        self.assertEqual(5, statuses.count("replayed"))
        self.assertEqual(1, len(self.service.session_events(session)["events"]))


class _Threads:
    """简单的线程组上下文，收集每个线程的调用结果。"""

    def __init__(self, size):
        self.size = size

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, fn):
        results = [None] * self.size
        errors = []

        def run(index):
            try:
                results[index] = fn()
            except Exception as exc:  # pragma: no cover - 失败时集中抛出
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(self.size)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise errors[0]
        return results


class PermissionTest(QuizTestCase):
    def test_auditor_cannot_manage_or_submit(self):
        self.make_bank()
        session = self.make_session(self.make_participant("权限家庭", 30))
        with self.assertRaises(PermissionDenied):
            self.service.create_bank(request_id="bank-denied", actor_id="au1", site_id="s1",
                                     bank_id="bank-denied", title="越权题库")
        with self.assertRaises(PermissionDenied):
            self.service.submit_events(actor_id="au1", session_id=session,
                                       events=[self.answer(1, "q1", "金银花")])
        with self.assertRaises(PermissionDenied):
            self.service.settle_session(actor_id="au1", session_id=session)

    def test_unknown_objects_raise_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.session_score("missing-session")
        with self.assertRaises(NotFoundError):
            self.service.get_bank("missing-bank")
        with self.assertRaises(NotFoundError):
            self.service.team_progress("missing-team")


class QuizApiTest(QuizTestCase):
    def test_http_roundtrip(self):
        self.make_bank()
        participant = self.make_participant("接口家庭", 30)
        session = self.make_session(participant)
        status, payload = route(self.service, "POST", "/quiz/sessions/events",
                                {"session_id": session,
                                 "events": [self.answer(1, "q1", "金银花")]},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertEqual("recorded", payload["results"][0]["status"])
        status, payload = route(self.service, "GET",
                                f"/quiz/sessions/score?session_id={session}", None)
        self.assertEqual(200, status)
        self.assertEqual(10, payload["score"])
        status, payload = route(self.service, "POST", "/quiz/sessions/settle",
                                {"session_id": session}, {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertFalse(payload["replayed"])
        status, payload = route(self.service, "POST", "/quiz/sessions/settle",
                                {"session_id": session}, {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        status, payload = route(self.service, "GET", "/quiz/leaderboard?site_id=s1", None)
        self.assertEqual(200, status)
        self.assertEqual(["接口家庭"], [row["alias"] for row in payload["items"]])
        status, payload = route(self.service, "GET", "/quiz/sessions/score", None)
        self.assertEqual(400, status)
        status, payload = route(self.service, "GET",
                                f"/quiz/sessions/question?session_id={session}&question_id=q1",
                                None)
        self.assertEqual(200, status)
        self.assertEqual(3, len(payload["hints"]))


if __name__ == "__main__":
    unittest.main()
