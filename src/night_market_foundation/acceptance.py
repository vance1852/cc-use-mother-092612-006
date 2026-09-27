"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .quiz import QuizService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = QuizService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范活动机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="活动负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号活动站点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="organizer_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="organizer_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        quiz = _run_quiz_chain(service)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed, **quiz}
        database.close()
        return result


def _run_quiz_chain(service: QuizService) -> dict[str, object]:
    """执行题库冻结、会话、乱序补传、重传与结算的验收链路。"""

    service.create_bank(request_id="req-bank", actor_id="operator-001", site_id="site-001",
                        bank_id="bank-001", title="药材知识闯关题库")
    service.add_question(request_id="req-q1", actor_id="operator-001", bank_id="bank-001",
                         question_id="q1", level=1, position=1,
                         prompt="清热解毒、花色由白转黄的药材？", answer_key="金银花",
                         points=10, min_age=0, knowledge_source="《本草纲目·草部》",
                         hints=[{"hint_id": "h1", "text": "忍冬科植物", "min_age": 0}])
    service.add_question(request_id="req-q2", actor_id="operator-001", bank_id="bank-001",
                         question_id="q2", level=1, position=2,
                         prompt="滋补肝肾、益精明目的红色小果？", answer_key="枸杞",
                         points=10, min_age=0, knowledge_source="《神农本草经》",
                         hints=[{"hint_id": "h1", "text": "宁夏道地药材", "min_age": 0}])
    service.freeze_bank(request_id="req-freeze", actor_id="operator-001", bank_id="bank-001")
    service.register_participant(request_id="req-participant", actor_id="operator-001",
                                 site_id="site-001", alias="杏林小队", age=34,
                                 consent_public=True, participant_id="participant-001")
    service.start_session(request_id="req-session", actor_id="operator-001",
                          participant_id="participant-001", bank_id="bank-001",
                          session_id="session-001")
    events = [
        {"client_seq": 2, "kind": "answer", "question_id": "q2", "payload": {"answer": "枸杞"}},
        {"client_seq": 1, "kind": "answer", "question_id": "q1", "payload": {"answer": "金银花"}},
    ]
    service.submit_events(actor_id="operator-001", session_id="session-001", events=events)
    resend = service.submit_events(actor_id="operator-001", session_id="session-001",
                                   events=list(reversed(events)))
    score = service.session_score("session-001")
    settlement = service.settle_session(actor_id="operator-001", session_id="session-001")
    settled_again = service.settle_session(actor_id="operator-001", session_id="session-001")
    return {"quiz_score": score["score"],
            "quiz_effective_events": len(score["effective_events"]),
            "quiz_resend_all_replayed": all(item["status"] == "replayed"
                                            for item in resend["results"]),
            "quiz_settled": not settlement["replayed"],
            "quiz_settle_replayed": settled_again["replayed"]}


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
