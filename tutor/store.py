import hashlib
import json
from typing import Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from tutor.practice import CalculationAnswer, GradeResult, QuestionInstance


class Store(Protocol):
    async def ping(self) -> None: ...

    async def claim_thread(self, student_id: str, thread_id: str) -> bool: ...

    async def thread_owner(self, thread_id: str) -> str | None: ...

    async def save_question(
        self,
        question: QuestionInstance,
        *,
        source: str,
        source_url: str | None = None,
        content_sha256: str | None = None,
    ) -> QuestionInstance: ...

    async def get_question(self, question_id: str) -> QuestionInstance | None: ...

    async def select_bank_question(self, student_id: str, kp_id: str) -> QuestionInstance | None: ...

    async def record_attempt(
        self,
        *,
        student_id: str,
        thread_id: str,
        question_id: str,
        attempt_no: int,
        answer: CalculationAnswer,
        grade: GradeResult,
    ) -> GradeResult: ...


def question_content_sha256(question: QuestionInstance) -> str:
    payload = question.model_dump(
        mode="json",
        exclude={"id", "template_id", "template_version", "seed"},
    )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _question_from_row(row) -> QuestionInstance:
    return QuestionInstance(
        id=row[0],
        template_id=row[1],
        template_version=row[2],
        kp_ids=row[3],
        type=row[4],
        difficulty=row[5],
        stem=row[6],
        params=row[7],
        answer_spec=row[8],
        solution=row[9],
        rubric=row[10],
        symbols=row[11],
        seed=row[12],
    )


QUESTION_COLUMNS = (
    "id, template_id, template_version, kp_ids, type, difficulty, stem, params, "
    "answer_spec, solution_spec, rubric, symbols, seed"
)


class MemoryStore:
    def __init__(self, healthy: bool = True, with_content: bool = True):
        self.healthy = healthy
        self.sessions: dict[str, str] = {}
        self.questions: dict[str, QuestionInstance] = {}
        self.attempts: dict[tuple[str, str, int], tuple[str, GradeResult]] = {}
        if with_content:
            from tutor.content import load_curated_questions

            self.questions = {item.id: item.to_question() for item in load_curated_questions()}

    async def ping(self) -> None:
        if not self.healthy:
            raise ConnectionError("store unavailable")

    async def claim_thread(self, student_id: str, thread_id: str) -> bool:
        return self.sessions.setdefault(thread_id, student_id) == student_id

    async def thread_owner(self, thread_id: str) -> str | None:
        return self.sessions.get(thread_id)

    async def save_question(
        self,
        question: QuestionInstance,
        *,
        source: str,
        source_url: str | None = None,
        content_sha256: str | None = None,
    ) -> QuestionInstance:
        existing = self.questions.setdefault(question.id, question)
        if existing != question:
            raise ValueError(f"题目不可变快照冲突: {question.id}")
        return existing

    async def get_question(self, question_id: str) -> QuestionInstance | None:
        return self.questions.get(question_id)

    async def select_bank_question(self, student_id: str, kp_id: str) -> QuestionInstance | None:
        attempted = {
            question_id
            for (_, question_id, _), (owner, _) in self.attempts.items()
            if owner == student_id
        }
        candidates = sorted(
            (
                question
                for question in self.questions.values()
                if question.template_id is None and kp_id in question.kp_ids
            ),
            key=lambda item: (item.id in attempted, item.id),
        )
        return candidates[0] if candidates else None

    async def record_attempt(
        self,
        *,
        student_id: str,
        thread_id: str,
        question_id: str,
        attempt_no: int,
        answer: CalculationAnswer,
        grade: GradeResult,
    ) -> GradeResult:
        key = (thread_id, question_id, attempt_no)
        _, existing = self.attempts.setdefault(key, (student_id, grade))
        return existing


class PgStore:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def ping(self) -> None:
        async with self.pool.connection(timeout=2) as conn:
            await conn.execute("SELECT 1")

    async def claim_thread(self, student_id: str, thread_id: str) -> bool:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO students (id) VALUES (%s) ON CONFLICT DO NOTHING", (student_id,)
            )
            await conn.execute(
                "INSERT INTO sessions (thread_id, student_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (thread_id, student_id),
            )
            cursor = await conn.execute(
                "SELECT student_id FROM sessions WHERE thread_id = %s", (thread_id,)
            )
            row = await cursor.fetchone()
        return row is not None and row[0] == student_id

    async def thread_owner(self, thread_id: str) -> str | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT student_id FROM sessions WHERE thread_id = %s", (thread_id,)
            )
            row = await cursor.fetchone()
        return row[0] if row else None

    async def save_question(
        self,
        question: QuestionInstance,
        *,
        source: str,
        source_url: str | None = None,
        content_sha256: str | None = None,
    ) -> QuestionInstance:
        digest = content_sha256 or question_content_sha256(question)
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO questions ("
                "id, template_id, template_version, kp_ids, type, difficulty, stem, params, "
                "answer_spec, solution_spec, rubric, symbols, source, source_url, content_sha256, "
                "seed, verified) VALUES ("
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true) "
                "ON CONFLICT (id) DO NOTHING",
                (
                    question.id,
                    question.template_id,
                    question.template_version,
                    question.kp_ids,
                    question.type,
                    question.difficulty,
                    question.stem,
                    Jsonb(question.params),
                    Jsonb(question.answer_spec),
                    Jsonb(question.solution),
                    Jsonb(question.rubric),
                    Jsonb(question.symbols),
                    source,
                    source_url,
                    digest,
                    question.seed,
                ),
            )
            cursor = await conn.execute(
                f"SELECT {QUESTION_COLUMNS}, content_sha256 FROM questions WHERE id = %s",
                (question.id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("题目写入后不可见")
        if row[13] != digest:
            raise ValueError(f"题目不可变快照冲突: {question.id}")
        return _question_from_row(row)

    async def get_question(self, question_id: str) -> QuestionInstance | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {QUESTION_COLUMNS} FROM questions WHERE id = %s AND verified",
                (question_id,),
            )
            row = await cursor.fetchone()
        return _question_from_row(row) if row else None

    async def select_bank_question(self, student_id: str, kp_id: str) -> QuestionInstance | None:
        recent = await self._recent_question_ids(student_id)
        row = await self._select_bank_row(student_id, kp_id, recent)
        if row is None and recent:
            row = await self._select_bank_row(student_id, kp_id, [])
        return _question_from_row(row) if row else None

    async def _recent_question_ids(self, student_id: str) -> list[str]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT question_id FROM attempts WHERE student_id = %s "
                "GROUP BY question_id ORDER BY max(created_at) DESC LIMIT 10",
                (student_id,),
            )
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def _select_bank_row(self, student_id: str, kp_id: str, excluded: list[str]):
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {QUESTION_COLUMNS} FROM questions q "
                "WHERE q.source = 'bank' AND q.verified AND %s = ANY(q.kp_ids) "
                "AND NOT (q.id = ANY(%s::text[])) "
                "ORDER BY EXISTS (SELECT 1 FROM attempts a WHERE a.student_id = %s "
                "AND a.question_id = q.id), "
                "(SELECT max(a.created_at) FROM attempts a WHERE a.student_id = %s "
                "AND a.question_id = q.id) NULLS FIRST, q.id LIMIT 1",
                (kp_id, excluded, student_id, student_id),
            )
            return await cursor.fetchone()

    async def record_attempt(
        self,
        *,
        student_id: str,
        thread_id: str,
        question_id: str,
        attempt_no: int,
        answer: CalculationAnswer,
        grade: GradeResult,
    ) -> GradeResult:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "INSERT INTO attempts (student_id, question_id, thread_id, attempt_no, answer, "
                "grade, correct, score, error_types, graded_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'rule') "
                "ON CONFLICT (thread_id, question_id, attempt_no) DO NOTHING RETURNING grade",
                (
                    student_id,
                    question_id,
                    thread_id,
                    attempt_no,
                    Jsonb(answer.model_dump(mode="json")),
                    Jsonb(grade.model_dump(mode="json")),
                    grade.correct,
                    grade.score,
                    grade.error_types,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                cursor = await conn.execute(
                    "SELECT grade FROM attempts WHERE thread_id = %s AND question_id = %s "
                    "AND attempt_no = %s",
                    (thread_id, question_id, attempt_no),
                )
                row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("作答记录写入后不可见")
        return GradeResult.model_validate(row[0])
