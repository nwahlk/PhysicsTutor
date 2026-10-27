import argparse
import hashlib
import json
import math
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

import sympy
from pint.errors import DimensionalityError

from tutor.practice import (
    CalculationAnswer,
    TemplateSpec,
    UREG,
    grade_question,
    instantiate_template,
    load_templates,
    safe_expression,
)

ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "data" / "templates" / "physics_b1"
DEFAULT_CASES = ROOT / "evals" / "grading_golden.jsonl"
DEFAULT_HOLDOUT = ROOT / "evals" / "grading_holdout.jsonl"
DEFAULT_BASELINE = ROOT / "evals" / "grading_baseline.json"
DEFAULT_REVIEW = ROOT / "evals" / "template_review_300.jsonl"
DEFAULT_REPORT = ROOT / "evals" / "reports" / "grading-latest.json"
CATEGORIES = ("correct", "boundary", "unit", "direction", "steps", "malformed")
EXPECTED_DISTRIBUTIONS = {
    150: {"correct": 45, "boundary": 23, "unit": 22, "direction": 15, "steps": 30, "malformed": 15},
    50: {"correct": 15, "boundary": 8, "unit": 7, "direction": 5, "steps": 10, "malformed": 5},
}


def dataset_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * ratio
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno} 不是合法 JSON") from exc
        row_id = row.get("id")
        if not row_id or row_id in seen:
            raise ValueError(f"{path}:{lineno} id 缺失或重复")
        seen.add(row_id)
        rows.append(row)
    if not rows:
        raise ValueError(f"{path} 没有数据")
    return rows


def load_cases(path: Path) -> list[dict]:
    rows = load_jsonl(path)
    templates = {(item.id, item.version) for item in load_templates(TEMPLATE_DIR)}
    for row in rows:
        if row.get("category") not in CATEGORIES:
            raise ValueError(f"{row['id']} category 非法")
        if (row.get("template_id"), row.get("template_version")) not in templates:
            raise ValueError(f"{row['id']} 引用了未知模板")
        if not isinstance(row.get("seed"), int) or row["seed"] < 0:
            raise ValueError(f"{row['id']} seed 非法")
        CalculationAnswer.model_validate(
            {"question_id": "fixture", **row.get("answer", {})}
        )
        expect = row.get("expect") or {}
        required = {
            "correct",
            "final_value_ok",
            "unit_ok",
            "direction_ok",
            "error_types",
            "rubric_earned",
        }
        if set(expect) != required:
            raise ValueError(f"{row['id']} expect 字段不完整")
    expected = EXPECTED_DISTRIBUTIONS.get(len(rows))
    if expected and Counter(row["category"] for row in rows) != Counter(expected):
        raise ValueError(f"{path} 类别分布不符合冻结契约")
    return rows


def evaluate(cases: list[dict]) -> tuple[list[dict], list[float]]:
    templates = {(item.id, item.version): item for item in load_templates(TEMPLATE_DIR)}
    results: list[dict] = []
    latencies: list[float] = []
    for case in cases:
        template = templates[(case["template_id"], case["template_version"])]
        question = instantiate_template(template, case["seed"])
        answer = CalculationAnswer.model_validate(
            {"question_id": question.id, **case["answer"]}
        )
        started = time.perf_counter()
        grade = grade_question(question, answer, attempt_no=1)
        latency_ms = (time.perf_counter() - started) * 1000
        latencies.append(latency_ms)
        actual = {
            "correct": grade.correct,
            "final_value_ok": grade.final_value_ok,
            "unit_ok": grade.unit_ok,
            "direction_ok": grade.direction_ok,
            "error_types": sorted(grade.error_types),
            "rubric_earned": {item.id: item.earned for item in grade.rubric_results},
        }
        expected = dict(case["expect"])
        expected["error_types"] = sorted(expected["error_types"])
        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "expect": expected,
                "actual": actual,
                "latency_ms": round(latency_ms, 3),
            }
        )
    return results, latencies


def score_results(results: list[dict], path: Path, latencies: list[float]) -> dict:
    total = len(results)
    correct_labels = [item for item in results if item["expect"]["correct"]]
    false_negatives = sum(
        item["expect"]["correct"] and not item["actual"]["correct"]
        for item in results
    )
    unit_cases = [item for item in results if "unit" in item["expect"]["error_types"]]
    direction_cases = [item for item in results if "direction" in item["expect"]["error_types"]]
    rubric_total = sum(len(item["expect"]["rubric_earned"]) for item in results)
    rubric_matches = sum(
        actual == item["actual"]["rubric_earned"].get(rubric_id)
        for item in results
        for rubric_id, actual in item["expect"]["rubric_earned"].items()
    )
    per_category = {}
    for category in CATEGORIES:
        subset = [item for item in results if item["category"] == category]
        matches = sum(item["actual"] == item["expect"] for item in subset)
        per_category[category] = {
            "total": len(subset),
            "matches": matches,
            "accuracy": round(matches / len(subset), 4) if subset else 0.0,
        }
    failures = [
        {
            "id": item["id"],
            "category": item["category"],
            "expect": item["expect"],
            "actual": item["actual"],
        }
        for item in results
        if item["actual"] != item["expect"]
    ]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dataset": path.resolve().relative_to(ROOT).as_posix(),
        "dataset_sha256": dataset_sha256(path),
        "case_count": total,
        "exact_accuracy": round((total - len(failures)) / total, 4),
        "correct_accuracy": round(
            sum(item["actual"]["correct"] == item["expect"]["correct"] for item in results) / total,
            4,
        ),
        "false_negative_rate": round(false_negatives / len(correct_labels), 4) if correct_labels else 0.0,
        "unit_error_recall": round(
            sum("unit" in item["actual"]["error_types"] for item in unit_cases) / len(unit_cases),
            4,
        ) if unit_cases else 1.0,
        "direction_error_recall": round(
            sum("direction" in item["actual"]["error_types"] for item in direction_cases) / len(direction_cases),
            4,
        ) if direction_cases else 1.0,
        "rubric_consistency": round(rubric_matches / rubric_total, 4) if rubric_total else 0.0,
        "p95_grading_ms": round(percentile(latencies, 0.95), 3),
        "per_category": per_category,
        "failures": failures,
    }


def baseline_metrics(report: dict) -> dict:
    return {
        key: report[key]
        for key in (
            "generated_at",
            "dataset",
            "dataset_sha256",
            "case_count",
            "exact_accuracy",
            "correct_accuracy",
            "false_negative_rate",
            "unit_error_recall",
            "direction_error_recall",
            "rubric_consistency",
            "per_category",
        )
    }


def check_gate(report: dict, baseline: dict | None = None) -> list[str]:
    problems: list[str] = []
    thresholds = {
        "correct_accuracy": (0.95, "correct 分类准确率"),
        "unit_error_recall": (0.95, "单位错误识别率"),
        "direction_error_recall": (0.95, "方向错误识别率"),
        "rubric_consistency": (0.90, "rubric 一致率"),
    }
    for key, (minimum, label) in thresholds.items():
        if report[key] + 1e-9 < minimum:
            problems.append(f"{label} {report[key]:.1%} 低于 {minimum:.0%}")
    if report["false_negative_rate"] > 0.02 + 1e-9:
        problems.append(f"正确答案误判率 {report['false_negative_rate']:.1%} 高于 2%")
    if report["p95_grading_ms"] >= 50:
        problems.append(f"判分 p95 {report['p95_grading_ms']:.1f} ms 不低于 50 ms")
    if baseline:
        if baseline.get("dataset_sha256") != report["dataset_sha256"]:
            problems.append("数据集 SHA 已变化，baseline 不可比")
        elif baseline.get("correct_accuracy", 0) - report["correct_accuracy"] > 0.02 + 1e-9:
            problems.append("correct 分类准确率相比 baseline 下降超过 2 个百分点")
        for category, old in baseline.get("per_category", {}).items():
            new = report["per_category"].get(category)
            if new and old.get("accuracy", 0) - new["accuracy"] > 0.05 + 1e-9:
                problems.append(f"{category} 类别相比 baseline 下降超过 5 个百分点")
    return problems


def _validate_parameter(template: TemplateSpec, name: str, value: Decimal) -> None:
    spec = template.parameters[name]
    if spec.values is not None:
        if value not in spec.values:
            raise AssertionError(f"{template.id} 参数 {name} 不在离散集合")
        return
    if value < spec.min or value > spec.max or (value - spec.min) % spec.step != 0:
        raise AssertionError(f"{template.id} 参数 {name} 越界")


def verify_templates(seed_count: int = 1000) -> dict:
    templates = load_templates(TEMPLATE_DIR)
    timings: list[float] = []
    for template in templates:
        names = set(template.parameters)
        for seed in range(seed_count):
            started = time.perf_counter()
            question = instantiate_template(template, seed)
            timings.append((time.perf_counter() - started) * 1000)
            if question != instantiate_template(template, seed):
                raise AssertionError(f"{template.id} seed={seed} 不稳定")
            values = {
                name: Decimal(item["value"])
                for name, item in question.params.items()
            }
            for name, value in values.items():
                _validate_parameter(template, name, value)
            substitutions = {
                sympy.Symbol(name): sympy.Rational(str(value))
                for name, value in values.items()
            }
            if not all(
                bool(safe_expression(rule, names).subs(substitutions))
                for rule in template.constraints
            ):
                raise AssertionError(f"{template.id} seed={seed} 违反约束")
            try:
                unit = UREG.parse_units(question.answer_spec["unit"])
                UREG.Quantity(1, unit).to_base_units()
            except (DimensionalityError, AttributeError) as exc:
                raise AssertionError(f"{template.id} seed={seed} 单位非法") from exc
    p95 = percentile(timings, 0.95)
    if p95 >= 20:
        raise AssertionError(f"模板实例化 p95 {p95:.1f} ms 不低于 20 ms")
    return {
        "template_count": len(templates),
        "seed_count_per_template": seed_count,
        "instance_count": len(timings),
        "p95_instantiation_ms": round(p95, 3),
    }


def verify_review_manifest(path: Path = DEFAULT_REVIEW) -> dict:
    rows = load_jsonl(path)
    if len(rows) != 300:
        raise ValueError("模板人工抽检清单必须恰好包含 300 题")
    templates = {(item.id, item.version): item for item in load_templates(TEMPLATE_DIR)}
    fatal = 0
    for row in rows:
        template = templates[(row["template_id"], row["template_version"])]
        question = instantiate_template(template, row["seed"])
        snapshot = {
            "stem": question.stem,
            "value": question.answer_spec["value"],
            "unit": question.answer_spec["unit"],
            "direction": question.answer_spec.get("direction"),
        }
        if snapshot != row["snapshot"]:
            raise ValueError(f"{row['id']} 与当前固定实例不一致")
        if row.get("status") != "reviewed" or not row.get("reviewer"):
            raise ValueError(f"{row['id']} 缺少人工复核记录")
        fatal += bool(row.get("fatal_error"))
    if fatal:
        raise ValueError(f"300 题人工抽检发现 {fatal} 个致命错误")
    return {"reviewed_count": len(rows), "fatal_error_count": fatal}


def print_report(report: dict) -> None:
    print(
        f"用例 {report['case_count']} | exact {report['exact_accuracy']:.1%} | "
        f"correct {report['correct_accuracy']:.1%} | FN {report['false_negative_rate']:.1%}"
    )
    print(
        f"单位 {report['unit_error_recall']:.1%} | 方向 {report['direction_error_recall']:.1%} | "
        f"rubric {report['rubric_consistency']:.1%} | 判分 p95 {report['p95_grading_ms']:.2f} ms"
    )
    for category, metrics in report["per_category"].items():
        print(f"  {category:<10} {metrics['matches']}/{metrics['total']} = {metrics['accuracy']:.1%}")


def main() -> None:
    parser = argparse.ArgumentParser(description="M2 确定性判分离线评测")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--only", default="")
    parser.add_argument("--no-baseline", action="store_true")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--skip-properties", action="store_true")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        cases = [case for case in cases if case["id"] in wanted]
        if not cases:
            raise SystemExit("--only 没有匹配到用例")
    if args.write_baseline and args.only:
        raise SystemExit("--only 调试不得写 baseline")
    if args.no_baseline and args.write_baseline:
        raise SystemExit("--no-baseline 与 --write-baseline 不能同时使用")

    results, latencies = evaluate(cases)
    report = score_results(results, args.cases, latencies)
    if not args.skip_properties:
        report["template_verification"] = verify_templates()
        report["manual_review"] = verify_review_manifest()
    print_report(report)

    baseline = None
    if not args.no_baseline and args.baseline.exists() and not args.write_baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    problems = check_gate(report, baseline)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {args.report}")
    if problems:
        for problem in problems:
            print(f"- {problem}")
        raise SystemExit(1)
    if args.write_baseline:
        args.baseline.write_text(
            json.dumps(baseline_metrics(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"baseline 已写入 {args.baseline}")
    print("M2 判分评测门禁通过")


if __name__ == "__main__":
    main()
