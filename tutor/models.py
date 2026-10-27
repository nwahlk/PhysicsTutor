import os
from dataclasses import dataclass
from typing import Any, Literal

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, field_validator


class RouteDecision(BaseModel):
    mode: Literal["explain", "practice", "mistakes"] = "explain"
    candidate_kp_ids: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("candidate_kp_ids")
    @classmethod
    def unique_candidates(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


@dataclass(frozen=True)
class RoleConfig:
    provider: str
    model: str
    base_url: str
    api_key_env: str
    temperature: float


ROLE_CONFIGS = {
    "router": RoleConfig("deepseek", "deepseek-chat", "https://api.deepseek.com", "DEEPSEEK_API_KEY", 0),
    "explain": RoleConfig("deepseek", "deepseek-chat", "https://api.deepseek.com", "DEEPSEEK_API_KEY", 0.2),
    "generate": RoleConfig("deepseek", "deepseek-chat", "https://api.deepseek.com", "DEEPSEEK_API_KEY", 0.4),
    "verify": RoleConfig("deepseek", "deepseek-chat", "https://api.deepseek.com", "DEEPSEEK_API_KEY", 0),
    "vision": RoleConfig(
        "dashscope",
        "qwen-vl-max",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
        0,
    ),
}


@dataclass
class ModelBundle:
    router: Any
    explain: Any


class UnavailableModel:
    def __init__(self, role: str, api_key_env: str):
        self.role = role
        self.api_key_env = api_key_env

    async def ainvoke(self, _messages):
        raise RuntimeError(f"缺少 {self.api_key_env}，无法调用 {self.role} 模型")


def role_config(role: str) -> RoleConfig:
    try:
        return ROLE_CONFIGS[role]
    except KeyError as exc:
        raise ValueError(f"未知模型角色: {role}") from exc


def chat_model(role: str) -> ChatOpenAI:
    config = role_config(role)
    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        raise RuntimeError(f"缺少 {config.api_key_env}，无法创建 {role} 模型")
    return ChatOpenAI(
        model=config.model,
        base_url=config.base_url,
        api_key=api_key,
        temperature=config.temperature,
        timeout=30,
        max_retries=2,
    )


def build_model_bundle() -> ModelBundle:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        unavailable = UnavailableModel("router/explain", "DEEPSEEK_API_KEY")
        return ModelBundle(router=unavailable, explain=unavailable)
    router = chat_model("router").with_structured_output(RouteDecision, method="function_calling")
    return ModelBundle(router=router, explain=chat_model("explain"))
