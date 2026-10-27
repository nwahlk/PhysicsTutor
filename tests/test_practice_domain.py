from pathlib import Path

import pytest

from tutor.practice import (
    AnswerStep,
    CalculationAnswer,
    FinalAnswer,
    TemplateError,
    TemplateSpec,
    grade_question,
    instantiate_template,
    load_templates,
    safe_expression,
    stable_seed,
)


@pytest.fixture
def newton_template():
    return TemplateSpec.model_validate(
        {
            "schema_version": 1,
            "id": "b1.4.3.net-force-acceleration",
            "version": 1,
            "kp_ids": ["b1.4.3.1"],
            "difficulty": 2,
            "type": "multi_step_numeric",
            "parameters": {
                "mass": {"unit": "kg", "values": [2]},
                "force": {"unit": "N", "values": [6]},
            },
            "constraints": ["force / mass <= 20"],
            "stem": "质量为 {mass} kg 的物体受到 {force} N 的水平合力，求加速度。",
            "answer": {
                "expression": "force / mass",
                "unit": "m/s^2",
                "direction": "same_as_force",
                "tolerance": {"relative": 0.01, "absolute": 0.001},
            },
            "symbols": {"mass": ["m"], "force": ["F", "F_net"], "acceleration": ["a"]},
            "rubric": [
                {
                    "id": "formula",
                    "kind": "equation",
                    "expected": "force = mass * acceleration",
                    "points": 30,
                },
                {
                    "id": "substitute",
                    "kind": "substitution",
                    "expected": "acceleration = force / mass",
                    "points": 30,
                },
                {"id": "magnitude", "kind": "final_value", "points": 25},
                {"id": "unit", "kind": "final_unit", "points": 10},
                {"id": "direction", "kind": "direction", "points": 5},
            ],
            "golden_cases": [
                {"params": {"mass": 2, "force": 6}, "value": 3, "unit": "m/s^2"},
                {"params": {"mass": 2, "force": 6}, "value": 0.003, "unit": "km/s^2"},
                {"params": {"mass": 2, "force": 6}, "value": 300, "unit": "cm/s^2"},
            ],
        }
    )


def answer(value="3", unit="m/s^2", direction="same_as_force", steps=None):
    return CalculationAnswer(
        question_id="ignored",
        final=FinalAnswer(value=value, unit=unit, direction=direction),
        steps=steps or [],
    )


def test_safe_expression_allows_only_declared_arithmetic():
    symbols = {"force", "mass"}
    assert safe_expression("force / mass", symbols).subs({"force": 6, "mass": 2}) == 3
    for expression in ["__import__('os')", "force.real", "unknown + 1", "force ** 99", "force ** mass"]:
        with pytest.raises(TemplateError):
            safe_expression(expression, symbols)


def test_template_is_deterministic_and_validates_score(newton_template):
    seed = stable_seed("student", "thread", "turn", newton_template.id, newton_template.version)
    first = instantiate_template(newton_template, seed)
    second = instantiate_template(newton_template, seed)
    assert first == second
    assert 0 <= seed <= 2**63 - 1
    assert first.stem.startswith("质量为 2")
    assert first.answer_spec["value"] == "3"
    assert sum(item.points for item in newton_template.rubric) == 100

    bad = newton_template.model_dump()
    bad["rubric"][0]["points"] = 20
    with pytest.raises(ValueError, match="100"):
        TemplateSpec.model_validate(bad)


def test_grader_accepts_equivalent_formula_units_and_tolerance(newton_template):
    question = instantiate_template(newton_template, 1)
    response = answer(
        value="0.003",
        unit="km/s^2",
        steps=[
            AnswerStep(kind="formula", expression="a = F_net / m"),
            AnswerStep(kind="substitution", expression="a = 6 / 2"),
        ],
    )
    result = grade_question(question, response, attempt_no=1)
    assert result.correct is True
    assert result.score == 100
    assert result.error_types == []

    boundary = grade_question(question, answer(value="3.02"), attempt_no=1)
    assert boundary.correct is True


def test_grader_reports_unit_direction_and_guided_retry(newton_template):
    question = instantiate_template(newton_template, 1)
    first = grade_question(question, answer(value="3", unit="N", direction="opposite_force"), attempt_no=1)
    assert first.correct is False
    assert {"unit", "direction"} <= set(first.error_types)
    assert first.hint and first.solution_revealed is False

    second = grade_question(question, answer(value="3", unit="N", direction="opposite_force"), attempt_no=2)
    assert second.solution_revealed is True
    assert second.solution


def test_malformed_step_never_executes(newton_template):
    question = instantiate_template(newton_template, 1)
    response = answer(steps=[AnswerStep(kind="formula", expression="__import__('os').system('whoami')")])
    result = grade_question(question, response, attempt_no=1)
    assert result.correct is True
    assert "malformed" in result.error_types
    assert next(item for item in result.rubric_results if item.id == "formula").earned == 0


def test_real_template_catalog_has_m2_minimum():
    directory = Path(__file__).resolve().parent.parent / "data" / "templates" / "physics_b1"
    templates = load_templates(directory)
    assert len(templates) >= 14
    assert {"b1.2", "b1.4"} <= {kp.rsplit(".", 2)[0] for item in templates for kp in item.kp_ids}
    for template in templates:
        for seed in range(20):
            question = instantiate_template(template, seed)
            assert question.answer_spec["unit"]
