"""药材知识闯关模块的自动化测试。

覆盖：题库冻结与换版、乱序补传、断点恢复、同序号分叉冲突、关卡解锁、
并发结算，以及运营/家庭/儿童/公开四种隐私视图。
"""

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from night_market_foundation.clock import FixedClock
from night_market_foundation.errors import ConflictError, PermissionDenied, ValidationError
from night_market_foundation.quiz import QuizService
from night_market_foundation.storage import Database


LEVELS = [
    {"code": "L1", "title": "初识本草", "prerequisites": []},
    {"code": "L2", "title": "方剂配伍", "prerequisites": ["L1"]},
]
QUESTIONS = [
    {"code": "q1", "level_code": "L1", "prompt": "人参的功效", "options": ["大补元气", "发汗解表"],
     "answer_key": "大补元气", "knowledge_source": "神农本草经", "min_age": 0},
    {"code": "q2", "level_code": "L1", "prompt": "甘草调和", "options": ["调和诸药", "攻下逐水"],
     "answer_key": "调和诸药", "knowledge_source": "本草纲目", "min_age": 6},
    {"code": "q3", "level_code": "L2", "prompt": "四君子汤组成", "options": ["参术苓草", "麻黄桂枝"],
     "answer_key": "参术苓草", "knowledge_source": "太平惠民和剂局方", "min_age": 0},
]
MEMBERS = [
    {"member_alias": "山爸", "is_child": False, "age": 38, "share_consent": False},
    {"member_alias": "小豆", "is_child": True, "age": 5, "share_consent": False},
]


class QuizTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = QuizService(self.database, FixedClock(datetime(2026, 9, 27, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="夜市机构")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="运营", role="operator", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="文化展示区", timezone_name="Asia/Shanghai")
        self.service.create_bank(request_id="bank", actor_id="op1", bank_id="b1",
                                 site_id="s1", name="本草闯关题库")
        self.service.create_bank_version(request_id="ver", actor_id="op1", bank_id="b1")
        self.service.replace_version_content(request_id="content", actor_id="op1",
                                             version_id="b1-v1", levels=LEVELS, questions=QUESTIONS)
        self.service.freeze_bank_version(request_id="freeze", actor_id="op1", version_id="b1-v1")

    def tearDown(self):
        self.database.close()

    def open_session(self, session_id="fam1", members=None):
        self.service.open_session(request_id=f"sess-{session_id}", actor_id="op1",
                                  session_id=session_id, site_id="s1", bank_id="b1",
                                  family_alias="山药一家", members=members or MEMBERS)
        return session_id

    def answer(self, seq, question, answer, alias="山爸"):
        return {"client_seq": seq, "event_kind": "answer", "question_code": question,
                "answer": answer, "member_alias": alias, "client_occurred_at": f"2026-09-27T10:{seq:02d}:00Z"}

    # ------------------------------------------------------------------
    # 题库冻结与换版
    # ------------------------------------------------------------------

    def test_frozen_version_is_immutable_and_has_content_hash(self):
        version = self.service.get_bank_version(actor_id="op1", version_id="b1-v1")
        self.assertEqual("frozen", version["status"])
        self.assertEqual(64, len(version["content_hash"]))
        with self.assertRaises(ConflictError):
            self.service.replace_version_content(
                request_id="tamper", actor_id="op1", version_id="b1-v1",
                levels=LEVELS, questions=QUESTIONS[:2])

    def test_cannot_open_session_before_any_freeze(self):
        self.service.create_bank(request_id="bank2", actor_id="op1", bank_id="b2",
                                 site_id="s1", name="未冻结题库")
        self.service.create_bank_version(request_id="ver2", actor_id="op1", bank_id="b2")
        with self.assertRaises(ConflictError):
            self.service.open_session(request_id="x", actor_id="op1", session_id="famX",
                                      site_id="s1", bank_id="b2", family_alias="家",
                                      members=[{"member_alias": "甲", "is_child": False,
                                                "age": 30, "share_consent": True}])

    def test_withdraw_creates_new_version_and_old_session_keeps_score(self):
        old_session = self.open_session("fam-old")
        self.service.withdraw_question(request_id="wd", actor_id="op1",
                                       bank_id="b1", question_code="q1")
        versions = self.service.list_bank_versions(actor_id="op1", bank_id="b1")
        self.assertEqual(["b1-v1", "b1-v2"], [v["version_id"] for v in versions])
        self.assertEqual("frozen", versions[1]["status"])
        self.assertNotEqual(versions[0]["content_hash"], versions[1]["content_hash"])
        # 老会话仍钉在 v1，撤回的 q1 依然有效。
        backend = self.service.get_session_backend(actor_id="op1", session_id=old_session)
        self.assertEqual("b1-v1", backend["bank_version_id"])
        self.service.ingest_events(request_id="old-1", actor_id="op1", session_id=old_session,
                                   batch_id="batch-old", events=[self.answer(1, "q1", "大补元气")])
        # 新会话钉在 v2，q1 已撤回，补传 q1 被拒绝。
        self.open_session("fam-new", [{"member_alias": "乙", "is_child": False,
                                       "age": 40, "share_consent": True}])
        result = self.service.ingest_events(request_id="new-1", actor_id="op1", session_id="fam-new",
                                            batch_id="batch-new", events=[self.answer(1, "q1", "大补元气", "乙")])
        self.assertEqual("rejected", result.results[0]["status"])

    def test_question_withdraw_does_not_rewrite_finalized_score(self):
        session_id = self.open_session("fam-done")
        events = [self.answer(1, "q1", "大补元气"),
                  {"client_seq": 2, "event_kind": "skip", "question_code": "q2", "member_alias": "山爸"},
                  self.answer(3, "q3", "参术苓草")]
        self.service.ingest_events(request_id="ev", actor_id="op1", session_id=session_id,
                                   batch_id="bev", events=events)
        settlement = self.service.finalize_session(request_id="fin", actor_id="op1", session_id=session_id)
        self.assertEqual(2, settlement.score)
        self.service.withdraw_question(request_id="wd2", actor_id="op1",
                                       bank_id="b1", question_code="q3")
        backend = self.service.get_session_backend(actor_id="op1", session_id=session_id)
        self.assertEqual(2, backend["score"])
        self.assertEqual("finalized", backend["status"])

    # ------------------------------------------------------------------
    # 乱序同步、重放与断点恢复
    # ------------------------------------------------------------------

    def test_out_of_order_batches_merge_by_client_sequence(self):
        session_id = self.open_session()
        # 终端断网后按 3、1、2 的顺序补传；q3 属于尚未解锁的 L2。
        arrivals = [
            [self.answer(3, "q3", "参术苓草")],
            [self.answer(1, "q1", "大补元气")],
            [self.answer(2, "q2", "调和诸药")],
        ]
        for index, events in enumerate(arrivals):
            self.service.ingest_events(request_id=f"batch-{index}", actor_id="op1",
                                       session_id=session_id, batch_id=f"batch-{index}", events=events)
        effective = self.service.list_effective_events(actor_id="op1", session_id=session_id)
        self.assertEqual([1, 2, 3], [event["client_seq"] for event in effective])
        backend = self.service.get_session_backend(actor_id="op1", session_id=session_id)
        self.assertEqual(["L1", "L2"], backend["completed_levels"])
        self.assertEqual(3, backend["score"])

    def test_duplicate_reupload_reuses_first_result(self):
        session_id = self.open_session()
        events = [self.answer(1, "q1", "大补元气")]
        first = self.service.ingest_events(request_id="dup", actor_id="op1",
                                           session_id=session_id, batch_id="dup-batch", events=events)
        replay = self.service.ingest_events(request_id="dup", actor_id="op1",
                                            session_id=session_id, batch_id="dup-batch", events=events)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.results[0]["event_id"], replay.results[0]["event_id"])
        # 换一个批次但内容完全相同，仍沿用第一次处理结果。
        again = self.service.ingest_events(request_id="dup2", actor_id="op1",
                                           session_id=session_id, batch_id="dup-batch-2", events=events)
        self.assertEqual("replayed", again.results[0]["status"])
        self.assertEqual(first.results[0]["event_id"], again.results[0]["event_id"])
        stored = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM quiz_events WHERE session_id=? AND client_seq=1", (session_id,)
        ).fetchone()["c"]
        self.assertEqual(1, stored)

    def test_restart_resumes_and_idempotent_replay_does_not_double_count(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quiz.sqlite3"
            database = Database(path)
            service = QuizService(database)
            self._bootstrap_full(service)
            service.ingest_events(request_id="batch-1", actor_id="op1", session_id="fam1",
                                  batch_id="b1", events=[service_answer(1, "q1", "大补元气")])
            database.close()
            # 服务重启：终端没有收到回执，用同一 request_id 重发。
            database = Database(path)
            service = QuizService(database)
            replay = service.ingest_events(request_id="batch-1", actor_id="op1", session_id="fam1",
                                           batch_id="b1", events=[service_answer(1, "q1", "大补元气")])
            self.assertTrue(replay.replayed)
            stored = database.connection.execute(
                "SELECT COUNT(*) AS c FROM quiz_events WHERE session_id='fam1' AND client_seq=1"
            ).fetchone()["c"]
            self.assertEqual(1, stored)
            valid, count = service.verify_audit()
            self.assertTrue(valid)
            database.close()

    # ------------------------------------------------------------------
    # 同序号分叉冲突
    # ------------------------------------------------------------------

    def _create_open_conflict(self, session_id="fam1"):
        """造一个同序号分叉的待解释冲突，返回冲突详情。"""

        self.service.ingest_events(request_id="b1", actor_id="op1", session_id=session_id,
                                   batch_id="b1", events=[self.answer(1, "q1", "大补元气")])
        diverged = self.service.ingest_events(
            request_id="b2", actor_id="op1", session_id=session_id, batch_id="b2",
            events=[self.answer(1, "q1", "发汗解表")])
        self.assertEqual("conflicted", diverged.results[0]["status"])
        conflicts = self.service.list_conflicts(actor_id="op1", session_id=session_id)
        self.assertEqual(1, len(conflicts))
        return conflicts[0]

    def test_same_sequence_divergence_keeps_both_and_blocks_settlement(self):
        session_id = self.open_session()
        conflict = self._create_open_conflict(session_id)
        self.assertEqual("open", conflict["status"])
        self.assertEqual(2, len(conflict["variants"]))
        answers = {variant["payload"]["answer"] for variant in conflict["variants"]}
        self.assertEqual({"大补元气", "发汗解表"}, answers)
        # 冲突未解释前，两份内容都不进入有效事件，也不能结算。
        effective = self.service.list_effective_events(actor_id="op1", session_id=session_id)
        self.assertEqual([], effective)
        with self.assertRaises(ConflictError):
            self.service.finalize_session(request_id="fin", actor_id="op1", session_id=session_id)

    def test_operator_explicitly_resolves_conflict_before_finalize(self):
        session_id = self.open_session()
        conflict = self._create_open_conflict(session_id)
        chosen = next(variant["event_id"] for variant in conflict["variants"]
                      if variant["payload"]["answer"] == "大补元气")
        self.service.resolve_conflict(request_id="res", actor_id="op1",
                                      conflict_id=conflict["conflict_id"],
                                      chosen_event_id=chosen, note="终端A时间戳更早，采用A")
        effective = self.service.list_effective_events(actor_id="op1", session_id=session_id)
        self.assertEqual(1, len(effective))
        self.assertTrue(effective[0]["chosen_by_resolution"])
        settlement = self.service.finalize_session(request_id="fin", actor_id="op1",
                                                   session_id=session_id)
        self.assertEqual(1, settlement.score)
        # 已裁决冲突不能改判。
        with self.assertRaises(ConflictError):
            self.service.resolve_conflict(request_id="res2", actor_id="op1",
                                          conflict_id=conflict["conflict_id"],
                                          chosen_event_id=conflict["variants"][0]["event_id"],
                                          note="想改判")

    def test_third_variant_after_conflict_is_also_preserved(self):
        session_id = self.open_session()
        self._create_open_conflict(session_id)
        third = self.service.ingest_events(
            request_id="b3", actor_id="op1", session_id=session_id, batch_id="b3",
            events=[self.answer(1, "q1", "大补元气")])  # 与第一份内容相同
        self.assertEqual("replayed", third.results[0]["status"])
        another = self.service.ingest_events(
            request_id="b4", actor_id="op1", session_id=session_id, batch_id="b4",
            events=[self.answer(1, "q1", "另一个答案")])
        self.assertEqual("conflicted", another.results[0]["status"])
        conflicts = self.service.list_conflicts(actor_id="op1", session_id=session_id)
        self.assertEqual(3, len(conflicts[0]["variants"]))

    # ------------------------------------------------------------------
    # 关卡解锁
    # ------------------------------------------------------------------

    def test_locked_level_events_wait_for_prerequisites(self):
        session_id = self.open_session()
        # 先补传 L2 的 q3 与 L1 的 q1；L1 未完成，q3 暂时不进入有效集。
        self.service.ingest_events(request_id="b1", actor_id="op1", session_id=session_id,
                                   batch_id="b1",
                                   events=[self.answer(1, "q3", "参术苓草"),
                                           self.answer(2, "q1", "大补元气")])
        backend = self.service.get_session_backend(actor_id="op1", session_id=session_id)
        self.assertEqual([], backend["completed_levels"])
        self.assertEqual(["L1"], backend["unlocked_levels"])
        # 跳过 q2 后 L1 完成，L2 解锁，早先到达的 q3 自动并入有效集。
        self.service.ingest_events(request_id="b2", actor_id="op1", session_id=session_id,
                                   batch_id="b2",
                                   events=[{"client_seq": 3, "event_kind": "skip",
                                            "question_code": "q2", "member_alias": "山爸"}])
        backend = self.service.get_session_backend(actor_id="op1", session_id=session_id)
        self.assertEqual(["L1", "L2"], backend["completed_levels"])
        self.assertEqual(2, backend["score"])

    # ------------------------------------------------------------------
    # 有效事件列表与成绩复核
    # ------------------------------------------------------------------

    def test_settlement_hash_matches_listed_effective_events(self):
        from night_market_foundation.audit import digest

        session_id = self.open_session()
        events = [self.answer(3, "q3", "参术苓草"),
                  self.answer(1, "q1", "大补元气"),
                  {"client_seq": 2, "event_kind": "skip", "question_code": "q2",
                   "member_alias": "山爸"}]
        self.service.ingest_events(request_id="batch", actor_id="op1", session_id=session_id,
                                   batch_id="batch", events=events)
        settlement = self.service.finalize_session(request_id="fin", actor_id="op1",
                                                   session_id=session_id)
        listed = self.service.list_effective_events(actor_id="op1", session_id=session_id)
        recomputed = digest([
            {"event_id": event["event_id"], "client_seq": event["client_seq"],
             "event_kind": event["event_kind"], "question_code": event["question_code"],
             "correctness": event["correctness"]}
            for event in listed
        ])
        self.assertEqual(settlement.effective_hash, recomputed)
        self.assertEqual(settlement.effective_event_count, len(listed))
        self.assertEqual([1, 2, 3], [event["client_seq"] for event in listed])

    # ------------------------------------------------------------------
    # 并发结算
    # ------------------------------------------------------------------

    def test_concurrent_finalize_produces_single_immutable_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent.sqlite3"
            database = Database(path)
            service = QuizService(database)
            self._bootstrap_full(service)
            service.ingest_events(request_id="ev", actor_id="op1", session_id="fam1",
                                  batch_id="bev", events=[service_answer(1, "q1", "大补元气")])
            results: list = []
            errors: list = []

            def worker(index: int) -> None:
                try:
                    outcome = service.finalize_session(
                        request_id=f"finalize-{index}", actor_id="op1", session_id="fam1")
                    results.append(outcome)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual([], errors)
            self.assertEqual(8, len(results))
            self.assertTrue(all(result.score == 1 for result in results))
            hashes = {result.effective_hash for result in results}
            self.assertEqual(1, len(hashes))
            count = database.connection.execute("SELECT COUNT(*) AS c FROM quiz_settlements").fetchone()["c"]
            self.assertEqual(1, count)
            # 结算后不能再补传或改判。
            from night_market_foundation.errors import ConflictError
            with self.assertRaises(ConflictError):
                service.ingest_events(request_id="late", actor_id="op1", session_id="fam1",
                                      batch_id="late", events=[service_answer(2, "q2", "调和诸药")])
            valid, _ = service.verify_audit()
            self.assertTrue(valid)
            database.close()

    def test_concurrent_identical_upload_deduplicates_to_one_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent2.sqlite3"
            database = Database(path)
            service = QuizService(database)
            self._bootstrap_full(service)
            outcomes: list = []

            def worker(index: int) -> None:
                result = service.ingest_events(
                    request_id=f"up-{index}", actor_id="op1", session_id="fam1",
                    batch_id=f"up-{index}", events=[service_answer(1, "q1", "大补元气")])
                outcomes.append(result.results[0])

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            statuses = {item["status"] for item in outcomes}
            self.assertTrue(statuses <= {"accepted", "replayed"})
            event_ids = {item["event_id"] for item in outcomes}
            self.assertEqual(1, len(event_ids))
            database.close()

    # ------------------------------------------------------------------
    # 隐私视图
    # ------------------------------------------------------------------

    def test_child_age_restricted_hint_is_rejected(self):
        session_id = self.open_session()
        result = self.service.ingest_events(
            request_id="hint", actor_id="op1", session_id=session_id, batch_id="hint",
            events=[{"client_seq": 1, "event_kind": "hint", "question_code": "q2",
                     "member_alias": "小豆"}])
        self.assertEqual("rejected", result.results[0]["status"])
        self.assertEqual("age_restricted", result.results[0]["reason"])
        # 成人查看同一提示不受限。
        ok = self.service.ingest_events(
            request_id="hint2", actor_id="op1", session_id=session_id, batch_id="hint2",
            events=[{"client_seq": 2, "event_kind": "hint", "question_code": "q2",
                     "member_alias": "山爸"}])
        self.assertEqual("accepted", ok.results[0]["status"])

    def test_child_view_only_shows_age_appropriate_unlocked_content(self):
        session_id = self.open_session()
        view = self.service.child_view(session_id=session_id, member_alias="小豆")
        codes = {question["code"] for question in view["questions"]}
        self.assertEqual({"q1"}, codes)  # q2 超龄，q3 关卡未解锁
        with self.assertRaises(PermissionDenied):
            self.service.child_view(session_id=session_id, member_alias="山爸")

    def test_family_view_only_shows_consented_adult_progress(self):
        session_id = self.open_session()
        self.service.ingest_events(request_id="e1", actor_id="op1", session_id=session_id,
                                   batch_id="e1",
                                   events=[self.answer(1, "q1", "大补元气", "山爸"),
                                           self.answer(2, "q1", "大补元气", "小豆")])
        # 默认未同意公开：家庭视图里没有任何共享进度。
        view = self.service.family_view(session_id=session_id, member_alias="山爸")
        self.assertEqual([], view["shared_progress"])
        self.service.set_share_consent(request_id="c1", actor_id="op1", session_id=session_id,
                                       member_alias="山爸", consent=True)
        view = self.service.family_view(session_id=session_id, member_alias="小豆")
        aliases = {item["member_alias"] for item in view["shared_progress"]}
        self.assertEqual({"山爸"}, aliases)  # 儿童进度即便有事件也绝不共享明细

    def test_public_leaderboard_excludes_children_and_non_consented_families(self):
        # 纯儿童会话：完成后也不进公开排行。
        child_only = "fam-child"
        self.open_session(child_only, [{"member_alias": "童童", "is_child": True,
                                        "age": 9, "share_consent": True}])
        self.service.ingest_events(request_id="ce", actor_id="op1", session_id=child_only,
                                   batch_id="ce", events=[self.answer(1, "q1", "大补元气", "童童")])
        self.service.finalize_session(request_id="cf", actor_id="op1", session_id=child_only)
        # 成人未同意公开：同样排除。
        self.open_session("fam-quiet")
        self.service.ingest_events(request_id="qe", actor_id="op1", session_id="fam-quiet",
                                   batch_id="qe", events=[self.answer(1, "q1", "大补元气")])
        self.service.finalize_session(request_id="qf", actor_id="op1", session_id="fam-quiet")
        # 成人明确同意：进入排行，且只暴露化名与分数。
        loud = "fam-loud"
        self.open_session(loud, [{"member_alias": "兰姨", "is_child": False,
                                  "age": 36, "share_consent": True},
                                 {"member_alias": "小苗", "is_child": True, "age": 7}])
        self.service.ingest_events(request_id="le", actor_id="op1", session_id=loud,
                                   batch_id="le", events=[self.answer(1, "q1", "大补元气", "兰姨")])
        self.service.finalize_session(request_id="lf", actor_id="op1", session_id=loud)
        board = self.service.public_leaderboard(site_id="s1")
        self.assertEqual(1, len(board))
        self.assertEqual({"family_alias", "score"}, set(board[0]))
        self.assertEqual("山药一家", board[0]["family_alias"])

    def test_effective_event_views_hide_fields_by_audience(self):
        session_id = self.open_session()
        self.service.ingest_events(request_id="ev", actor_id="op1", session_id=session_id,
                                   batch_id="bev", events=[self.answer(1, "q1", "大补元气")])
        operator_view = self.service.list_effective_events(actor_id="op1", session_id=session_id)
        self.assertIn("event_id", operator_view[0])
        self.assertIn("member_alias", operator_view[0])
        public_view = self.service.list_effective_events(actor_id="op1", session_id=session_id,
                                                         view="public")
        self.assertNotIn("member_alias", public_view[0])
        self.assertNotIn("event_id", public_view[0])
        family_view = self.service.list_effective_events(actor_id="op1", session_id=session_id,
                                                         view="family")
        self.assertIn("member_alias", family_view[0])
        self.assertNotIn("event_id", family_view[0])
        with self.assertRaises(ValidationError):
            self.service.list_effective_events(actor_id="op1", session_id=session_id, view="other")

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _bootstrap_full(self, service: QuizService) -> None:
        service.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="o1", name="夜市机构")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                               display_name="管理员", role="admin", organization_id="o1")
        service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                               display_name="运营", role="operator", organization_id="o1")
        service.register_site(request_id="site", actor_id="op1", site_id="s1",
                              organization_id="o1", name="文化展示区", timezone_name="Asia/Shanghai")
        service.create_bank(request_id="bank", actor_id="op1", bank_id="b1",
                            site_id="s1", name="本草闯关题库")
        service.create_bank_version(request_id="ver", actor_id="op1", bank_id="b1")
        service.replace_version_content(request_id="content", actor_id="op1",
                                        version_id="b1-v1", levels=LEVELS,
                                        questions=QUESTIONS)
        service.freeze_bank_version(request_id="freeze", actor_id="op1", version_id="b1-v1")
        service.open_session(request_id="sess-fam1", actor_id="op1", session_id="fam1",
                             site_id="s1", bank_id="b1", family_alias="山药一家",
                             members=[{"member_alias": "山爸", "is_child": False,
                                       "age": 38, "share_consent": True}])


def service_answer(seq, question, answer, alias="山爸"):
    return {"client_seq": seq, "event_kind": "answer", "question_code": question,
            "answer": answer, "member_alias": alias}


if __name__ == "__main__":
    unittest.main()
