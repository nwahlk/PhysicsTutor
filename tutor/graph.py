from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from tutor.catalog import Catalog, KnowledgePoint
from tutor.content import load_verified_templates
from tutor.models import ModelBundle
from tutor.practice import CalculationAnswer, GradeResult, grade_question, instantiate_template, stable_seed
from tutor.store import MemoryStore, Store

MODES: tuple[str, ...] = ("explain", "practice", "mistakes")
DEFAULT_MODE = "explain"


class TutorState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    student_id: str
    thread_id: str
    turn_id: str
    turn_kp_id: str | None
    turn_mode: str | None
    focus_kp_id: str | None
    mode: str | None
    pending_mode: str
    candidate_kp_ids: list[str]
    route: str
    reply: str
    grade_result: dict


def resolve_focus(
    turn_kp_id: str | None,
    turn_mode: str | None,
    focus_kp_id: str | None,
    mode: str | None,
) -> tuple[str | None, str, str]:
    kp_id = turn_kp_id or focus_kp_id
    resolved_mode = turn_mode or mode or DEFAULT_MODE
    if resolved_mode == "mistakes":
        return kp_id, resolved_mode, "mistakes"
    if not kp_id:
        return None, resolved_mode, "clarify"
    return kp_id, resolved_mode, resolved_mode


def latest_student_message(state: TutorState) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def router_prompt(catalog: Catalog, state: TutorState) -> list[AnyMessage]:
    available = "\n".join(
        f"- {kp.id}: {kp.name}" for kp in catalog.points.values() if kp.card is not None
    )
    current = state.get("focus_kp_id") or "无"
    return [
        SystemMessage(
            content=(
                "你是高中物理辅导路由器。判断学生要讲解、练习还是查看错题集。"
                "若消息明确提到知识点，从清单中返回 1-3 个最相关 ID；不得输出清单外 ID。"
                "若只是围绕当前知识点继续交流，候选可为空。不要回答物理问题。\n"
                f"当前知识点：{current}\n可用知识点：\n{available}"
            )
        ),
        HumanMessage(content=latest_student_message(state)),
    ]


def make_router(catalog: Catalog, models: ModelBundle):
    async def router(state: TutorState) -> dict:
        if state.get("turn_kp_id") is not None or state.get("turn_mode") is not None:
            kp_id, mode, route = resolve_focus(
                state.get("turn_kp_id"),
                state.get("turn_mode"),
                state.get("focus_kp_id"),
                state.get("mode"),
            )
            return {"focus_kp_id": kp_id, "mode": mode, "route": route, "candidate_kp_ids": []}

        decision = await models.router.ainvoke(router_prompt(catalog, state))
        if decision.mode == "mistakes":
            return {"mode": "mistakes", "route": "mistakes", "candidate_kp_ids": []}
        candidates = [
            kp_id
            for kp_id in decision.candidate_kp_ids
            if (point := catalog.get(kp_id)) is not None and point.card is not None
        ][:3]
        if candidates:
            return {
                "pending_mode": decision.mode,
                "candidate_kp_ids": candidates,
                "route": "choose",
            }
        kp_id, mode, route = resolve_focus(
            None,
            decision.mode,
            state.get("focus_kp_id"),
            state.get("mode"),
        )
        return {"focus_kp_id": kp_id, "mode": mode, "route": route, "candidate_kp_ids": []}

    return router


def make_choose_focus(catalog: Catalog):
    def choose_focus(state: TutorState) -> dict:
        candidates = [catalog.get(kp_id) for kp_id in state["candidate_kp_ids"]]
        selected = interrupt(
            {
                "type": "kp_choice",
                "prompt": "请选择你想学习的知识点",
                "candidates": [{"id": kp.id, "name": kp.name} for kp in candidates if kp],
            }
        )
        mode = state["pending_mode"]
        return {
            "focus_kp_id": str(selected),
            "mode": mode,
            "route": mode,
            "candidate_kp_ids": [],
        }

    return choose_focus


def clarify(state: TutorState) -> dict:
    return {"reply": "先在左侧知识树里选一个想学的知识点，或者直接告诉我你想学什么。"}


class ExplainInput(TypedDict):
    messages: list[AnyMessage]
    focus_kp_id: str


class ExplainOutput(TypedDict):
    reply: str


class ExplainState(ExplainInput, ExplainOutput, total=False):
    pass


def explain_prompt(point: KnowledgePoint, question: str) -> list[AnyMessage]:
    card = point.card
    return [
        SystemMessage(
            content=(
                "你是严谨、耐心的高中物理老师。只能依据下面的知识卡片讲解；"
                "结构依次为直观理解、核心规律、易错提醒、微型例子，最后邀请学生做一道练习。"
                "使用纯中文文本和 Unicode 公式，不要输出 Markdown 或 LaTeX。"
                "不要虚构卡片外结论，不要代替学生完成未提供的作业题。\n"
                f"知识点：{point.id} {point.name}\n"
                f"定义：{card.definition}\n规律：{card.law}\n"
                f"易错：{'；'.join(card.misconceptions)}\n例子：{card.example}"
            )
        ),
        HumanMessage(content=question),
    ]


def build_explain_graph(catalog: Catalog, model):
    async def explain(state: ExplainState) -> dict:
        point = catalog.get(state["focus_kp_id"])
        if point is None or point.card is None:
            return {"reply": f"知识点 {state['focus_kp_id']} 的讲解卡片尚未开放。"}
        question = "请讲解这个知识点"
        for message in reversed(state.get("messages", [])):
            if isinstance(message, HumanMessage):
                question = str(message.content)
                break
        response = await model.ainvoke(explain_prompt(point, question))
        return {"reply": f"【知识点 {point.id} · {point.name}】\n{response.content}"}

    graph = StateGraph(ExplainState, input_schema=ExplainInput, output_schema=ExplainOutput)
    graph.add_node("explain", explain)
    graph.add_edge(START, "explain")
    graph.add_edge("explain", END)
    return graph.compile()


def mistakes_stub(state: TutorState) -> dict:
    return {"reply": "【M0 桩】错题集将在 M3 接入。"}


class PracticeInput(TypedDict):
    student_id: str
    thread_id: str
    turn_id: str
    focus_kp_id: str


class PracticeOutput(TypedDict, total=False):
    reply: str
    grade_result: dict


class PracticeState(PracticeInput, PracticeOutput, total=False):
    question_id: str
    attempt_no: int
    answer: dict
    grade_result: dict
    hint: str


def public_question(
    question,
    attempt_no: int,
    previous_grade: dict | None = None,
    hint: str | None = None,
) -> dict:
    payload = {
        "type": "question",
        "question": {
            "id": question.id,
            "kp_ids": question.kp_ids,
            "type": question.type,
            "difficulty": question.difficulty,
            "stem": question.stem,
        },
        "attempt_no": attempt_no,
        "fields": ["value", "unit", "direction", "steps"],
    }
    if previous_grade is not None:
        payload["previous_grade"] = previous_grade
    if hint is not None:
        payload["hint"] = hint
    return payload


def build_practice_graph(store: Store):
    templates = load_verified_templates()

    async def select_question(state: PracticeState) -> dict:
        question = await store.select_bank_question(state["student_id"], state["focus_kp_id"])
        if question is None:
            matching = [item for item in templates if state["focus_kp_id"] in item.kp_ids]
            if not matching:
                return {"reply": "这个知识点暂时没有可自动批改的练习题，请先选择其他知识点。"}
            selector = stable_seed(state["student_id"], state["thread_id"], state["turn_id"])
            template = matching[selector % len(matching)]
            seed = stable_seed(
                state["student_id"],
                state["thread_id"],
                state["turn_id"],
                template.id,
                template.version,
            )
            question = instantiate_template(template, seed)
            question = await store.save_question(question, source="template")
        return {"question_id": question.id, "attempt_no": 1}

    async def ask(state: PracticeState) -> dict:
        question = await store.get_question(state["question_id"])
        if question is None:
            raise RuntimeError("待作答题目不存在")
        answer = interrupt(public_question(question, state["attempt_no"]))
        return {"answer": answer}

    async def grade(state: PracticeState) -> dict:
        question = await store.get_question(state["question_id"])
        if question is None:
            raise RuntimeError("待批改题目不存在")
        answer = CalculationAnswer.model_validate(state["answer"])
        if answer.question_id != question.id:
            raise ValueError("答案 question_id 与待作答题目不一致")
        result = grade_question(question, answer, state["attempt_no"])
        return {"answer": answer.model_dump(mode="json"), "grade_result": result.model_dump(mode="json")}

    async def record(state: PracticeState) -> dict:
        answer = CalculationAnswer.model_validate(state["answer"])
        grade_result = GradeResult.model_validate(state["grade_result"])
        stored = await store.record_attempt(
            student_id=state["student_id"],
            thread_id=state["thread_id"],
            question_id=state["question_id"],
            attempt_no=state["attempt_no"],
            answer=answer,
            grade=grade_result,
        )
        return {"grade_result": stored.model_dump(mode="json")}

    def after_record(state: PracticeState) -> str:
        result = GradeResult.model_validate(state["grade_result"])
        return "retry" if not result.correct and state["attempt_no"] == 1 else "feedback"

    def hint(state: PracticeState) -> dict:
        result = GradeResult.model_validate(state["grade_result"])
        return {"attempt_no": 2, "hint": result.hint or "检查你的列式、数值、单位和方向。"}

    async def ask_retry(state: PracticeState) -> dict:
        question = await store.get_question(state["question_id"])
        if question is None:
            raise RuntimeError("待重试题目不存在")
        answer = interrupt(public_question(question, 2, state["grade_result"], state["hint"]))
        return {"answer": answer}

    def feedback(state: PracticeState) -> dict:
        result = GradeResult.model_validate(state["grade_result"])
        if result.correct:
            solution = "\n".join(result.solution)
            return {"reply": f"本题得分 {result.score}/100，最终答案正确。\n{solution}"}
        solution = "\n".join(result.solution)
        return {"reply": f"第二次作答得分 {result.score}/100。下面是完整解析：\n{solution}"}

    graph = StateGraph(PracticeState, input_schema=PracticeInput, output_schema=PracticeOutput)
    graph.add_node("select_question", select_question)
    graph.add_node("ask", ask)
    graph.add_node("grade", grade)
    graph.add_node("record", record)
    graph.add_node("hint", hint)
    graph.add_node("ask_retry", ask_retry)
    graph.add_node("feedback", feedback)
    graph.add_edge(START, "select_question")
    graph.add_conditional_edges(
        "select_question",
        lambda state: "ask" if state.get("question_id") else "end",
        {"ask": "ask", "end": END},
    )
    graph.add_edge("ask", "grade")
    graph.add_edge("ask_retry", "grade")
    graph.add_edge("grade", "record")
    graph.add_conditional_edges("record", after_record, {"retry": "hint", "feedback": "feedback"})
    graph.add_edge("hint", "ask_retry")
    graph.add_edge("feedback", END)
    return graph.compile()


def respond(state: TutorState) -> dict:
    return {"messages": [AIMessage(content=state["reply"])]}


def build_graph(checkpointer, catalog: Catalog, models: ModelBundle, store: Store | None = None):
    store = store or MemoryStore()
    graph = StateGraph(TutorState)
    graph.add_node("router", make_router(catalog, models))
    graph.add_node("choose", make_choose_focus(catalog))
    graph.add_node("clarify", clarify)
    graph.add_node("explain", build_explain_graph(catalog, models.explain))
    graph.add_node("practice", build_practice_graph(store))
    graph.add_node("mistakes", mistakes_stub)
    graph.add_node("respond", respond)
    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        lambda state: state["route"],
        ["choose", "clarify", "explain", "practice", "mistakes"],
    )
    graph.add_conditional_edges(
        "choose",
        lambda state: state["route"],
        ["explain", "practice", "mistakes"],
    )
    for node in ("clarify", "explain", "practice", "mistakes"):
        graph.add_edge(node, "respond")
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=checkpointer)
