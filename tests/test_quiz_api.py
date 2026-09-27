"""闯关模块 HTTP 路由的端到端测试。"""

import unittest

from night_market_foundation.api import route
from night_market_foundation.clock import FixedClock
from night_market_foundation.quiz import QuizService
from night_market_foundation.storage import Database

LEVELS = [{"code": "L1", "title": "初识本草", "prerequisites": []}]
QUESTIONS = [{
    "code": "q1", "level_code": "L1", "prompt": "人参的功效",
    "options": ["大补元气", "发汗解表"], "answer_key": "大补元气",
    "knowledge_source": "神农本草经", "min_age": 0,
}]


class QuizApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = QuizService(self.database)
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "机构"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"}, {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "s1", "organization_id": "o1",
               "name": "展区", "timezone_name": "Asia/Shanghai"}, {"X-Actor-Id": "a1"})

    def tearDown(self):
        self.database.close()

    def _freeze_bank(self):
        headers = {"X-Actor-Id": "a1"}
        route(self.service, "POST", "/quiz/banks",
              {"request_id": "bank", "bank_id": "b1", "site_id": "s1", "name": "题库"}, headers)
        route(self.service, "POST", "/quiz/bank-versions",
              {"request_id": "ver", "bank_id": "b1"}, headers)
        route(self.service, "POST", "/quiz/bank-versions/content",
              {"request_id": "content", "version_id": "b1-v1",
               "levels": LEVELS, "questions": QUESTIONS}, headers)
        status, payload = route(self.service, "POST", "/quiz/bank-versions/freeze",
                                {"request_id": "freeze", "version_id": "b1-v1"}, headers)
        self.assertEqual(201, status)
        status, version = route(self.service, "GET", "/quiz/bank-versions/b1-v1", None, headers)
        self.assertEqual(200, status)
        return version["content_hash"]

    def test_full_quiz_flow_over_http(self):
        headers = {"X-Actor-Id": "a1"}
        content_hash = self._freeze_bank()
        status, payload = route(self.service, "POST", "/quiz/sessions", {
            "request_id": "sess", "session_id": "fam1", "site_id": "s1", "bank_id": "b1",
            "family_alias": "山药一家",
            "members": [{"member_alias": "山爸", "is_child": False, "age": 36,
                         "share_consent": True}],
        }, headers)
        self.assertEqual(201, status)
        self.assertEqual("b1-v1", payload["bank_version_id"])
        # 乱序补传：同一批内事件顺序与客户端序号无关。
        status, payload = route(self.service, "POST", "/quiz/events", {
            "request_id": "batch1", "session_id": "fam1", "batch_id": "batch1",
            "events": [
                {"client_seq": 2, "event_kind": "answer", "question_code": "q1",
                 "answer": "大补元气", "member_alias": "山爸"},
            ],
        }, headers)
        self.assertEqual(202, status)
        self.assertEqual(1, payload["accepted"])
        # 重复补传沿用第一次结果。
        status, payload = route(self.service, "POST", "/quiz/events", {
            "request_id": "batch1", "session_id": "fam1", "batch_id": "batch1",
            "events": [
                {"client_seq": 2, "event_kind": "answer", "question_code": "q1",
                 "answer": "大补元气", "member_alias": "山爸"},
            ],
        }, headers)
        self.assertEqual(200, status)
        status, payload = route(self.service, "POST", "/quiz/sessions/finalize",
                                {"request_id": "final", "session_id": "fam1"}, headers)
        self.assertEqual(200, status)
        self.assertEqual(1, payload["score"])
        self.assertEqual(1, payload["effective_event_count"])
        self.assertEqual(64, len(payload["effective_hash"]))
        # 后台视图、有效事件、公开榜。
        status, backend = route(self.service, "GET", "/quiz/sessions/fam1/backend", None, headers)
        self.assertEqual(200, status)
        self.assertEqual("finalized", backend["status"])
        status, events = route(self.service, "GET",
                               "/quiz/sessions/fam1/effective-events?view=operator", None, headers)
        self.assertEqual(200, status)
        self.assertEqual(1, len(events["items"]))
        status, board = route(self.service, "GET", "/quiz/leaderboard?site_id=s1", None, headers)
        self.assertEqual(200, status)
        self.assertEqual(1, len(board["items"]))

    def test_divergence_requires_resolution_before_finalize(self):
        headers = {"X-Actor-Id": "a1"}
        self._freeze_bank()
        route(self.service, "POST", "/quiz/sessions", {
            "request_id": "sess", "session_id": "fam2", "site_id": "s1", "bank_id": "b1",
            "family_alias": "枸杞一家",
            "members": [{"member_alias": "爸", "is_child": False, "age": 40,
                         "share_consent": False}],
        }, headers)
        route(self.service, "POST", "/quiz/events", {
            "request_id": "b1", "session_id": "fam2", "batch_id": "b1",
            "events": [{"client_seq": 1, "event_kind": "answer", "question_code": "q1",
                        "answer": "大补元气", "member_alias": "爸"}],
        }, headers)
        status, payload = route(self.service, "POST", "/quiz/events", {
            "request_id": "b2", "session_id": "fam2", "batch_id": "b2",
            "events": [{"client_seq": 1, "event_kind": "answer", "question_code": "q1",
                        "answer": "发汗解表", "member_alias": "爸"}],
        }, headers)
        self.assertEqual(202, status)
        self.assertEqual("conflicted", payload["results"][0]["status"])
        status, payload = route(self.service, "POST", "/quiz/sessions/finalize",
                                {"request_id": "fin", "session_id": "fam2"}, headers)
        self.assertEqual(409, status)
        status, conflicts = route(self.service, "GET",
                                  "/quiz/conflicts?session_id=fam2", None, headers)
        self.assertEqual(200, status)
        self.assertEqual(2, len(conflicts["items"][0]["variants"]))
        chosen = next(v["event_id"] for v in conflicts["items"][0]["variants"]
                      if v["payload"]["answer"] == "大补元气")
        status, payload = route(self.service, "POST", "/quiz/conflicts/resolve", {
            "request_id": "res", "conflict_id": conflicts["items"][0]["conflict_id"],
            "chosen_event_id": chosen, "note": "以现场终端为准",
        }, headers)
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST", "/quiz/sessions/finalize",
                                {"request_id": "fin2", "session_id": "fam2"}, headers)
        self.assertEqual(200, status)
        self.assertEqual(1, payload["score"])


if __name__ == "__main__":
    unittest.main()
