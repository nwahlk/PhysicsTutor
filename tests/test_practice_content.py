from pathlib import Path

from tutor.content import (
    ContentError,
    load_curated_questions,
    load_verified_templates,
    normalized_stem_sha256,
)
from tutor.practice import AnswerStep, CalculationAnswer, FinalAnswer, grade_question


def test_verified_template_catalog_meets_m2_contract():
    templates = load_verified_templates()
    assert len(templates) >= 14
    assert all(len(item.golden_cases) >= 3 for item in templates)
    assert {kp.rsplit(".", 2)[0] for item in templates for kp in item.kp_ids} >= {"b1.2", "b1.4"}


def test_reviewed_question_bank_has_traceable_balanced_content():
    questions = load_curated_questions()
    assert len(questions) >= 30
    assert sum(any(kp.startswith("b1.2.") for kp in item.kp_ids) for item in questions) >= 15
    assert sum(any(kp.startswith("b1.4.") for kp in item.kp_ids) for item in questions) >= 15
    assert {item.difficulty for item in questions} >= {1, 2, 3}
    for item in questions:
        assert item.status == "reviewed"
        assert item.content_sha256 == normalized_stem_sha256(item.stem)
        assert item.source_url.startswith("http")
        assert item.adaptation_note and item.rights_note and item.reviewer


def test_all_reviewed_answers_and_formula_rubrics_regrade_to_full_score():
    for item in load_curated_questions():
        question = item.to_question()
        formula = next(part for part in item.rubric if part["id"] == "formula")
        answer = CalculationAnswer(
            question_id=question.id,
            final=FinalAnswer(
                value=str(item.answer_spec["value"]),
                unit=item.answer_spec["unit"],
                direction=item.answer_spec.get("direction"),
            ),
            steps=[AnswerStep(kind="formula", expression=formula["expected"])],
        )
        result = grade_question(question, answer, attempt_no=1)
        assert result.correct, item.id
        assert result.score == 100, item.id


def test_loader_rejects_unknown_knowledge_point(tmp_path: Path):
    source = next(iter((Path(__file__).parent.parent / "data" / "questions" / "physics_b1").glob("*.jsonl")))
    line = source.read_text(encoding="utf-8").splitlines()[0].replace("b1.2.2.2", "b1.9.9.9")
    (tmp_path / "bad.jsonl").write_text(line + "\n", encoding="utf-8")
    try:
        load_curated_questions(tmp_path)
    except ContentError as exc:
        assert "未知知识点" in str(exc)
    else:
        raise AssertionError("未知知识点未被拒绝")
