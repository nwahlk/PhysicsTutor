import asyncio
import json

import httpx


def call(app, method, path, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def chat(app, message, student="s1", thread="t1", **extra):
    return call(app, "POST", "/chat", json={"student_id": student, "thread_id": thread, "message": message, **extra})


def test_index_serves_student_ui(api_app):
    response = call(api_app, "GET", "/")
    assert response.status_code == 200
    assert "引力场" in response.text and "知识坐标" in response.text


def test_health_and_readiness(api_app):
    assert call(api_app, "GET", "/healthz").json() == {"status": "ok"}
    assert call(api_app, "GET", "/readyz").status_code == 200
    api_app.state.store.healthy = False
    assert call(api_app, "GET", "/readyz").status_code == 503


def test_catalog_endpoint(api_app):
    data = call(api_app, "GET", "/catalog").json()
    assert data["subject"] == "physics"
    assert len(data["chapters"]) == 4
    first = data["chapters"][0]["sections"][0]["points"][0]
    assert first == {"id": "b1.1.1.1", "name": "质点", "prereqs": [], "available": False}


def test_chat_validates_input(api_app):
    assert chat(api_app, "hi", kp_id="nope").status_code == 422
    assert chat(api_app, "hi", mode="dance").status_code == 422
    assert chat(api_app, "hi", student="bad id!").status_code == 422
    assert chat(api_app, "").status_code == 422


def test_explain_turn_streams_reply(api_app):
    response = chat(api_app, "讲讲", kp_id="b1.4.3.1", mode="explain")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    kinds = [event["type"] for event in events(response)]
    assert kinds == ["token", "done"]
    assert "b1.4.3.1" in events(response)[0]["content"]


def test_free_text_choice_rejects_unknown_candidate(api_app):
    from tutor.models import RouteDecision

    api_app.state.models.router.decision = RouteDecision(
        mode="explain", candidate_kp_ids=["b1.4.3.1", "b1.4.1.2"]
    )
    first = events(chat(api_app, "牛顿定律怎么理解"))
    assert first[0]["type"] == "interrupt"
    assert first[0]["payload"]["type"] == "kp_choice"

    invalid = call(
        api_app,
        "POST",
        "/answer",
        json={"student_id": "s1", "thread_id": "t1", "answer": "b1.2.4.1"},
    )
    assert invalid.status_code == 422

    selected = call(
        api_app,
        "POST",
        "/answer",
        json={"student_id": "s1", "thread_id": "t1", "answer": "b1.4.3.1"},
    )
    assert events(selected)[0]["type"] == "token"
    assert "b1.4.3.1" in events(selected)[0]["content"]


def test_practice_interrupt_then_structured_answer(api_app):
    first = events(chat(api_app, "来一道", kp_id="b1.2.4.1", mode="practice"))
    assert [event["type"] for event in first] == ["interrupt", "done"]
    payload = first[0]["payload"]
    assert "b1.2.4.1" in payload["question"]["kp_ids"]
    assert "answer_spec" not in payload["question"] and "solution" not in payload["question"]
    question_id = payload["question"]["id"]

    assert chat(api_app, "插话").status_code == 409

    history = call(api_app, "GET", "/history", params={"student_id": "s1", "thread_id": "t1"}).json()
    assert history["focus_kp_id"] == "b1.2.4.1"
    assert history["pending"][0]["payload"]["type"] == "question"

    answer = {
        "kind": "calculation",
        "question_id": question_id,
        "final": {"value": "30", "unit": "m/s", "direction": "down"},
        "steps": [{"kind": "formula", "expression": "v = g * t"}],
    }
    resumed = call(
        api_app,
        "POST",
        "/answer",
        json={"student_id": "s1", "thread_id": "t1", "answer": answer},
    )
    resumed_events = events(resumed)
    assert [event["type"] for event in resumed_events] == ["grade", "token", "done"]
    assert resumed_events[0]["result"]["correct"] is True
    assert "最终答案正确" in resumed_events[1]["content"]

    again = call(api_app, "POST", "/answer", json={"student_id": "s1", "thread_id": "t1", "answer": "x"})
    assert again.status_code == 409


def test_practice_wrong_answer_streams_hint_and_second_solution(api_app):
    first = events(chat(api_app, "来一道", thread="retry1", kp_id="b1.2.4.1", mode="practice"))
    question_id = first[0]["payload"]["question"]["id"]
    wrong = {
        "kind": "calculation",
        "question_id": question_id,
        "final": {"value": "1", "unit": "N", "direction": "up"},
        "steps": [],
    }
    first_grade = events(
        call(
            api_app,
            "POST",
            "/answer",
            json={"student_id": "s1", "thread_id": "retry1", "answer": wrong},
        )
    )
    assert [event["type"] for event in first_grade] == ["grade", "hint", "interrupt", "done"]
    assert first_grade[0]["result"]["solution_revealed"] is False
    assert first_grade[2]["payload"]["attempt_no"] == 2

    second_grade = events(
        call(
            api_app,
            "POST",
            "/answer",
            json={"student_id": "s1", "thread_id": "retry1", "answer": wrong},
        )
    )
    assert [event["type"] for event in second_grade] == ["grade", "token", "done"]
    assert second_grade[0]["result"]["solution_revealed"] is True
    assert second_grade[0]["result"]["solution"]


def test_practice_rejects_text_and_stale_question_id(api_app):
    first = events(chat(api_app, "来一道", thread="validation1", kp_id="b1.2.4.1", mode="practice"))
    question_id = first[0]["payload"]["question"]["id"]
    text = call(
        api_app,
        "POST",
        "/answer",
        json={"student_id": "s1", "thread_id": "validation1", "answer": "30 m/s"},
    )
    assert text.status_code == 422

    stale = {
        "kind": "calculation",
        "question_id": f"{question_id}-stale",
        "final": {"value": "30", "unit": "m/s", "direction": "down"},
        "steps": [],
    }
    response = call(
        api_app,
        "POST",
        "/answer",
        json={"student_id": "s1", "thread_id": "validation1", "answer": stale},
    )
    assert response.status_code == 422


def test_answer_without_session_is_409(api_app):
    response = call(api_app, "POST", "/answer", json={"student_id": "s1", "thread_id": "nope", "answer": "x"})
    assert response.status_code == 409


def test_other_student_is_forbidden(api_app):
    chat(api_app, "讲讲", kp_id="b1.1.1.1")
    assert chat(api_app, "我也来", student="s2").status_code == 403
    params = {"student_id": "s2", "thread_id": "t1"}
    assert call(api_app, "GET", "/history", params=params).status_code == 403
    answer = call(api_app, "POST", "/answer", json={"student_id": "s2", "thread_id": "t1", "answer": "x"})
    assert answer.status_code == 403


def test_history_of_unknown_thread_is_empty(api_app):
    data = call(api_app, "GET", "/history", params={"student_id": "s1", "thread_id": "fresh"}).json()
    assert data["messages"] == [] and data["pending"] == []
