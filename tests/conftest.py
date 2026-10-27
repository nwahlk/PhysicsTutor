import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tutor.api import CATALOG, app  # noqa: E402
from tutor.graph import build_graph  # noqa: E402
from tutor.models import ModelBundle, RouteDecision  # noqa: E402
from tutor.store import MemoryStore  # noqa: E402


class FakeRouter:
    def __init__(self):
        self.decision = RouteDecision(mode="explain", candidate_kp_ids=[])

    async def ainvoke(self, _messages):
        return self.decision


class FakeExplain:
    async def ainvoke(self, _messages):
        return AIMessage(content="这是基于知识卡片生成的讲解。")


@pytest.fixture
def api_app():
    # httpx ASGITransport 不触发 lifespan：直接注入内存版依赖
    models = ModelBundle(router=FakeRouter(), explain=FakeExplain())
    store = MemoryStore()
    app.state.models = models
    app.state.graph = build_graph(InMemorySaver(), catalog=CATALOG, models=models, store=store)
    app.state.store = store
    yield app
    del app.state.graph
    del app.state.store
    del app.state.models
