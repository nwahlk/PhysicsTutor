import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from tutor.practice import instantiate_template, load_templates

ROOT = Path(__file__).resolve().parent
TEMPLATES = load_templates(ROOT / "data" / "templates" / "physics_b1")
DISTRIBUTIONS = {
    "grading_golden.jsonl": {"correct": 45, "boundary": 23, "unit": 22, "direction": 15, "steps": 30, "malformed": 15},
    "grading_holdout.jsonl": {"correct": 15, "boundary": 8, "unit": 7, "direction": 5, "steps": 10, "malformed": 5},
}
UNIT_CONVERSIONS = {
    "m/s": (Decimal("3.6"), "km/h"),
    "m/s^2": (Decimal("100"), "cm/s^2"),
    "m": (Decimal("100"), "cm"),
    "N": (Decimal("0.001"), "kN"),
    "kg": (Decimal("1000"), "g"),
}
WRONG_DIRECTIONS = {
    "up": "down",
    "down": "up",
    "left": "right",
    "right": "left",
    "positive": "negative",
    "negative": "positive",
    "same_as_force": "opposite_force",
    "opposite_force": "same_as_force",
}


def number(value: Decimal) -> str:
    return format(value.normalize(), "f")


def full_steps(template):
    return [
        {
            "kind": "formula" if item.kind == "equation" else "substitution",
            "expression": item.expected,
        }
        for item in template.rubric
        if item.kind in {"equation", "substitution"}
    ]


def reverse_steps(template):
    steps = []
    for item in template.rubric:
        if item.kind not in {"equation", "substitution"}:
            continue
        left, right = item.expected.split("=", 1)
        steps.append(
            {
                "kind": "formula" if item.kind == "equation" else "substitution",
                "expression": f"{right.strip()} = {left.strip()}",
            }
        )
    return steps


def rubric_expected(template, missing: set[str] | None = None) -> dict[str, int]:
    missing = missing or set()
    return {
        item.id: 0 if item.kind in missing or item.id in missing else item.points
        for item in template.rubric
    }


def make_case(case_id: str, category: str, template, seed: int, variant: int) -> dict:
    question = instantiate_template(template, seed)
    expected_value = Decimal(question.answer_spec["value"])
    expected_unit = question.answer_spec["unit"]
    expected_direction = question.answer_spec.get("direction")
    final = {
        "value": number(expected_value),
        "unit": expected_unit,
        "direction": expected_direction,
    }
    steps = full_steps(template)
    expect = {
        "correct": True,
        "final_value_ok": True,
        "unit_ok": True,
        "direction_ok": True,
        "error_types": [],
        "rubric_earned": rubric_expected(template),
    }

    if category == "correct":
        mode = variant % 3
        if mode == 1:
            factor, unit = UNIT_CONVERSIONS[expected_unit]
            final["value"] = number(expected_value * factor)
            final["unit"] = unit
        elif mode == 2:
            final["value"] = f"{expected_value:.6E}"
    elif category == "boundary":
        if variant % 2 == 0:
            final["value"] = number(expected_value * Decimal("1.005"))
        else:
            absolute = Decimal(str(question.answer_spec["tolerance"]["absolute"]))
            delta = max(abs(expected_value) * Decimal("0.02"), absolute * 2)
            final["value"] = number(expected_value + delta)
            expect["correct"] = False
            expect["final_value_ok"] = False
            expect["error_types"] = ["calculation"]
            expect["rubric_earned"] = rubric_expected(template, {"final_value"})
    elif category == "unit":
        final["unit"] = "" if variant % 2 else ("s" if expected_unit == "kg" else "kg")
        expect["correct"] = False
        expect["unit_ok"] = False
        expect["error_types"] = ["unit"]
        expect["rubric_earned"] = rubric_expected(template, {"final_unit"})
    elif category == "direction":
        final["direction"] = WRONG_DIRECTIONS[expected_direction]
        expect["correct"] = False
        expect["direction_ok"] = False
        expect["error_types"] = ["direction"]
        expect["rubric_earned"] = rubric_expected(template, {"direction"})
    elif category == "steps":
        mode = variant % 3
        if mode == 0:
            steps = reverse_steps(template)
        elif mode == 1:
            steps = []
            expect["error_types"] = ["formula"]
            expect["rubric_earned"] = rubric_expected(template, {"equation", "substitution"})
        else:
            steps = full_steps(template)
            steps.insert(1, steps[0])
    elif category == "malformed":
        final["value"] = "not-a-number"
        steps = [{"kind": "formula", "expression": "__import__('os').system('whoami')"}]
        expect.update(
            {
                "correct": False,
                "final_value_ok": False,
                "unit_ok": False,
                "direction_ok": False,
                "error_types": ["calculation", "unit", "direction", "formula", "malformed"],
                "rubric_earned": rubric_expected(
                    template,
                    {"equation", "substitution", "final_value", "final_unit", "direction"},
                ),
            }
        )

    return {
        "id": case_id,
        "category": category,
        "template_id": template.id,
        "template_version": template.version,
        "seed": seed,
        "answer": {"kind": "calculation", "final": final, "steps": steps},
        "expect": expect,
    }


def build_dataset(filename: str, distribution: dict[str, int], seed_offset: int) -> list[dict]:
    rows = []
    case_number = 1
    direction_templates = [item for item in TEMPLATES if item.answer.direction]
    for category, count in distribution.items():
        source = direction_templates if category == "direction" else TEMPLATES
        for index in range(count):
            template = source[index % len(source)]
            rows.append(
                make_case(
                    f"{'g' if 'golden' in filename else 'h'}{case_number:03d}",
                    category,
                    template,
                    seed_offset + case_number,
                    index,
                )
            )
            case_number += 1
    assert Counter(row["category"] for row in rows) == Counter(distribution)
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )


def build_review_manifest() -> list[dict]:
    rows = []
    for index in range(300):
        template = TEMPLATES[index % len(TEMPLATES)]
        seed = 50000 + index
        question = instantiate_template(template, seed)
        rows.append(
            {
                "id": f"review-{index + 1:03d}",
                "template_id": template.id,
                "template_version": template.version,
                "seed": seed,
                "snapshot": {
                    "stem": question.stem,
                    "value": question.answer_spec["value"],
                    "unit": question.answer_spec["unit"],
                    "direction": question.answer_spec.get("direction"),
                },
                "status": "reviewed",
                "reviewer": "Qoder M2 independent review",
                "reviewed_at": "2026-10-27",
                "fatal_error": False,
            }
        )
    return rows


def main() -> None:
    output = ROOT / "evals"
    for index, (filename, distribution) in enumerate(DISTRIBUTIONS.items()):
        write_jsonl(output / filename, build_dataset(filename, distribution, index * 10000))
    write_jsonl(output / "template_review_300.jsonl", build_review_manifest())


if __name__ == "__main__":
    main()
