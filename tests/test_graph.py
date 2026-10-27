import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from tutor.catalog import load_catalog
from tutor.graph import build_graph, resolve_focus
from tutor.models import ModelBundle, RouteDecision, build_model_bundle
from tutor.store import MemoryStore


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("k1", "practice", None, None), ("k1", "practice", "practice")),
        (("k2", None, "k1", "practice"), ("k2", "practice", "practice")),
        ((None, None, "k1", "practice"), ("k1", "practice", "practice")),
        ((None, "explain", "k1", "practice"), ("k1", "explain", "explain")),
        ((None, None, None, None), (None, "explain", "clarify")),
        ((None, "practice", None, None), (None, "practice", "clarify")),
        ((None, "mistakes", None, None), (None, "mistakes", "mistakes")),
    ],
)
def test_resolve_focus(args, expected):
    assert resolve_focus(*args) == expected


def turn(message, kp_id=None, mode=None):
    return {
        "messages": [HumanMessage(content=message)],
        "student_id": "test-student",
        "thread_id": "test-thread",
        "turn_id": f"turn-{message}",
        "turn_kp_id": kp_id,
        "turn_mode": mode,
    }


class FakeRouter:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return self.decision


class FakeExplain:
    async def ainvoke(self, messages):
        assert "定义：" in messages[0].content and "规律：" in messages[0].content
        return AIMessage(content="合力产生加速度；质量一定时，合力越大，加速度越大。")


def fake_models(decision=None):
    return ModelBundle(
        router=FakeRouter(decision or RouteDecision(mode="explain", candidate_kp_ids=[])),
        explain=FakeExplain(),
    )


def test_model_bundle_without_key_fails_only_when_invoked(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    models = build_model_bundle()
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        asyncio.run(models.router.ainvoke([]))


def test_practice_closes_without_model_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    async def scenario():
        store = MemoryStore()
        graph = build_graph(
            InMemorySaver(),
            catalog=load_catalog(),
            models=build_model_bundle(),
            store=store,
        )
        config = {"configurable": {"thread_id": "offline-practice"}}
        first = await graph.ainvoke(
            turn("来一道", "b1.2.4.1", "practice"),
            config,
        )
        question_id = first["__interrupt__"][0].value["question"]["id"]
        question = await store.get_question(question_id)
        answer = {
            "kind": "calculation",
            "question_id": question_id,
            "final": {
                "value": question.answer_spec["value"],
                "unit": question.answer_spec["unit"],
                "direction": question.answer_spec.get("direction"),
            },
            "steps": [],
        }
        result = await graph.ainvoke(Command(resume=answer), config)
        assert result["grade_result"]["correct"] is True

    asyncio.run(scenario())


def test_explicit_selection_skips_router_and_explain_cites_card():
    async def scenario():
        models = fake_models()
        graph = build_graph(InMemorySaver(), catalog=load_catalog(), models=models)
        result = await graph.ainvoke(turn("讲讲", "b1.4.3.1", "explain"), {"configurable": {"thread_id": "e1"}})
        reply = result["messages"][-1].content
        assert "b1.4.3.1" in reply and "牛顿第二定律" in reply
        assert models.router.calls == 0

    asyncio.run(scenario())


def test_free_text_router_requires_candidate_confirmation():
    async def scenario():
        models = fake_models(RouteDecision(mode="explain", candidate_kp_ids=["b1.4.3.1", "b1.4.1.2"]))
        graph = build_graph(InMemorySaver(), catalog=load_catalog(), models=models)
        config = {"configurable": {"thread_id": "route1"}}
        first = await graph.ainvoke(turn("牛顿定律怎么理解"), config)
        choice = first["__interrupt__"][0].value
        assert choice["type"] == "kp_choice"
        assert [item["id"] for item in choice["candidates"]] == ["b1.4.3.1", "b1.4.1.2"]

        result = await graph.ainvoke(Command(resume="b1.4.3.1"), config)
        assert result["focus_kp_id"] == "b1.4.3.1"
        assert "b1.4.3.1" in result["messages"][-1].content

    asyncio.run(scenario())


def test_practice_interrupt_guided_retry_and_focus_persists():
    async def scenario():
        store = MemoryStore()
        graph = build_graph(InMemorySaver(), catalog=load_catalog(), models=fake_models(), store=store)
        config = {"configurable": {"thread_id": "t1"}}

        await graph.ainvoke(turn("讲讲", "b1.2.4.1", "explain"), config)
        state = await graph.aget_state(config)
        assert state.values["focus_kp_id"] == "b1.2.4.1"
        assert "b1.2.4.1" in state.values["messages"][-1].content

        first = await graph.ainvoke(turn("来一道", mode="practice"), config)
        payload = first["__interrupt__"][0].value
        assert payload["type"] == "question"
        assert "b1.2.4.1" in payload["question"]["kp_ids"]
        question_id = payload["question"]["id"]

        wrong = {
            "kind": "calculation",
            "question_id": question_id,
            "final": {"value": "1", "unit": "N", "direction": "up"},
            "steps": [],
        }
        retry = await graph.ainvoke(Command(resume=wrong), config)
        retry_payload = retry["__interrupt__"][0].value
        assert retry_payload["attempt_no"] == 2
        assert retry_payload["previous_grade"]["solution_revealed"] is False

        correct = {
            "kind": "calculation",
            "question_id": question_id,
            "final": {"value": "30", "unit": "m/s", "direction": "down"},
            "steps": [],
        }
        await graph.ainvoke(Command(resume=correct), config)
        state = await graph.aget_state(config)
        assert not state.interrupts
        assert "最终答案正确" in state.values["messages"][-1].content
        assert len(store.attempts) == 2
        assert "question_id" not in state.values
        assert "answer" not in state.values

    asyncio.run(scenario())


def test_without_focus_asks_to_choose():
    async def scenario():
        graph = build_graph(InMemorySaver(), catalog=load_catalog(), models=fake_models())
        config = {"configurable": {"thread_id": "t2"}}
        result = await graph.ainvoke(turn("你好"), config)
        assert "知识树" in result["messages"][-1].content

    asyncio.run(scenario())
