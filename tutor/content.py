import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from tutor.catalog import Catalog, load_catalog
from tutor.practice import QuestionInstance, TemplateSpec, load_templates

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "data" / "templates" / "physics_b1"
QUESTION_DIR = ROOT / "data" / "questions" / "physics_b1"


class ContentError(ValueError):
    pass


def normalized_stem_sha256(stem: str) -> str:
    normalized = " ".join(stem.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def template_spec_sha256(template: TemplateSpec) -> str:
    encoded = json.dumps(
        template.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CuratedQuestion(BaseModel):
    id: str = Field(pattern=r"^bank_b1_[a-z0-9_]+$")
    kp_ids: list[str] = Field(min_length=1)
    type: Literal["numeric", "multi_step_numeric", "choice"]
    difficulty: int = Field(ge=1, le=5)
    stem: str = Field(min_length=10)
    params: dict[str, dict] = Field(default_factory=dict)
    answer_spec: dict
    solution_spec: list[str] = Field(min_length=1)
    rubric: list[dict] = Field(min_length=1)
    symbols: dict[str, list[str]] = Field(default_factory=dict)
    source_url: str = Field(pattern=r"^https?://")
    source_title: str = Field(min_length=1)
    accessed_at: date
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adaptation_note: str = Field(min_length=1)
    rights_note: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    status: Literal["draft", "reviewed", "rejected"]
    serving_enabled: bool = True

    @model_validator(mode="after")
    def validate_contract(self):
        if self.content_sha256 != normalized_stem_sha256(self.stem):
            raise ValueError("content_sha256 与规范化题干不一致")
        if sum(int(item["points"]) for item in self.rubric) != 100:
            raise ValueError("rubric 分值之和必须为 100")
        if not self.answer_spec.get("unit"):
            raise ValueError("answer_spec 必须包含单位")
        return self

    def to_question(self) -> QuestionInstance:
        return QuestionInstance(
            id=self.id,
            kp_ids=self.kp_ids,
            type=self.type,
            difficulty=self.difficulty,
            stem=self.stem,
            params=self.params,
            answer_spec=self.answer_spec,
            solution=self.solution_spec,
            rubric=self.rubric,
            symbols=self.symbols,
        )


def validate_kp_ids(kp_ids: list[str], catalog: Catalog, content_id: str) -> None:
    unknown = [kp_id for kp_id in kp_ids if catalog.get(kp_id) is None]
    if unknown:
        raise ContentError(f"{content_id} 引用了未知知识点: {', '.join(unknown)}")


def load_verified_templates(
    directory: Path = TEMPLATE_DIR,
    catalog: Catalog | None = None,
) -> list[TemplateSpec]:
    catalog = catalog or load_catalog()
    templates = load_templates(directory)
    for template in templates:
        validate_kp_ids(template.kp_ids, catalog, template.id)
    return templates


def load_curated_questions(
    directory: Path = QUESTION_DIR,
    catalog: Catalog | None = None,
) -> list[CuratedQuestion]:
    catalog = catalog or load_catalog()
    questions: list[CuratedQuestion] = []
    ids: set[str] = set()
    hashes: set[str] = set()
    for path in sorted(directory.rglob("*.jsonl")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                question = CuratedQuestion.model_validate_json(line)
            except ValueError as exc:
                raise ContentError(f"{path.name}:{line_no}: {exc}") from exc
            if question.id in ids:
                raise ContentError(f"精选题 ID 重复: {question.id}")
            if question.content_sha256 in hashes:
                raise ContentError(f"精选题题干重复: {question.id}")
            validate_kp_ids(question.kp_ids, catalog, question.id)
            ids.add(question.id)
            hashes.add(question.content_sha256)
            if question.status == "reviewed" and question.serving_enabled:
                questions.append(question)
    return questions


async def seed_content(conn) -> tuple[int, int]:
    templates = load_verified_templates()
    questions = load_curated_questions()
    for template in templates:
        digest = template_spec_sha256(template)
        await conn.execute(
            "INSERT INTO question_templates (id, version, kp_ids, difficulty, type, spec, "
            "spec_sha256, verified) VALUES (%s, %s, %s, %s, %s, %s, %s, true) "
            "ON CONFLICT (id, version) DO NOTHING",
            (
                template.id,
                template.version,
                template.kp_ids,
                template.difficulty,
                template.type,
                Jsonb(template.model_dump(mode="json")),
                digest,
            ),
        )
        cursor = await conn.execute(
            "SELECT spec_sha256 FROM question_templates WHERE id = %s AND version = %s",
            (template.id, template.version),
        )
        row = await cursor.fetchone()
        if row is None or row[0] != digest:
            raise ContentError(f"模板不可变版本冲突: {template.id} v{template.version}")

    for item in questions:
        question = item.to_question()
        await conn.execute(
            "INSERT INTO questions (id, kp_ids, type, difficulty, stem, params, answer_spec, "
            "solution_spec, rubric, symbols, source, source_url, content_sha256, verified) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'bank', %s, %s, true) "
            "ON CONFLICT (id) DO NOTHING",
            (
                question.id,
                question.kp_ids,
                question.type,
                question.difficulty,
                question.stem,
                Jsonb(question.params),
                Jsonb(question.answer_spec),
                Jsonb(question.solution),
                Jsonb(question.rubric),
                Jsonb(question.symbols),
                item.source_url,
                item.content_sha256,
            ),
        )
        cursor = await conn.execute(
            "SELECT content_sha256 FROM questions WHERE id = %s", (question.id,)
        )
        row = await cursor.fetchone()
        if row is None or row[0] != item.content_sha256:
            raise ContentError(f"精选题不可变快照冲突: {question.id}")
    return len(templates), len(questions)
