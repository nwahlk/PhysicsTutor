import asyncio

from tutor.practice import CalculationAnswer, FinalAnswer, GradeResult
from tutor.store import MemoryStore


def test_duplicate_attempt_is_idempotent_under_concurrency():
    async def scenario():
        store = MemoryStore()
        answer = CalculationAnswer(
            question_id="q1",
            final=FinalAnswer(value="1", unit="m/s"),
        )
        first = GradeResult(
            question_id="q1",
            attempt_no=1,
            correct=True,
            score=100,
            final_value_ok=True,
            unit_ok=True,
            direction_ok=True,
            rubric_results=[],
            error_types=[],
        )
        conflicting = first.model_copy(update={"correct": False, "score": 0})

        saved = await asyncio.gather(
            store.record_attempt(
                student_id="s1",
                thread_id="t1",
                question_id="q1",
                attempt_no=1,
                answer=answer,
                grade=first,
            ),
            store.record_attempt(
                student_id="s1",
                thread_id="t1",
                question_id="q1",
                attempt_no=1,
                answer=answer,
                grade=conflicting,
            ),
        )

        assert saved == [first, first]
        assert len(store.attempts) == 1

    asyncio.run(scenario())
