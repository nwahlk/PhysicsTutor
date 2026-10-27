import json
from collections import Counter
from pathlib import Path

import eval_grading as evaluation

ROOT = Path(__file__).resolve().parent.parent


def test_grading_datasets_match_frozen_contract():
    golden = evaluation.load_cases(ROOT / "evals" / "grading_golden.jsonl")
    holdout = evaluation.load_cases(ROOT / "evals" / "grading_holdout.jsonl")

    assert len(golden) == 150
    assert len(holdout) == 50
    assert Counter(item["category"] for item in golden) == Counter(
        evaluation.EXPECTED_DISTRIBUTIONS[150]
    )
    assert Counter(item["category"] for item in holdout) == Counter(
        evaluation.EXPECTED_DISTRIBUTIONS[50]
    )


def test_grading_golden_and_holdout_pass_absolute_gates():
    for filename in ("grading_golden.jsonl", "grading_holdout.jsonl"):
        path = ROOT / "evals" / filename
        results, latencies = evaluation.evaluate(evaluation.load_cases(path))
        report = evaluation.score_results(results, path, latencies)
        assert evaluation.check_gate({**report, "p95_grading_ms": 0}) == []
        assert report["exact_accuracy"] == 1.0


def test_grading_baseline_matches_dataset_and_metrics():
    path = ROOT / "evals" / "grading_golden.jsonl"
    results, latencies = evaluation.evaluate(evaluation.load_cases(path))
    report = evaluation.score_results(results, path, latencies)
    baseline = json.loads((ROOT / "evals" / "grading_baseline.json").read_text(encoding="utf-8"))

    assert baseline["dataset_sha256"] == evaluation.dataset_sha256(path)
    assert evaluation.check_gate({**report, "p95_grading_ms": 0}, baseline) == []


def test_template_review_manifest_and_small_seed_sweep():
    assert evaluation.verify_review_manifest() == {
        "reviewed_count": 300,
        "fatal_error_count": 0,
    }
    result = evaluation.verify_templates(seed_count=10)
    assert result["template_count"] >= 14
    assert result["instance_count"] >= 140
    assert result["p95_instantiation_ms"] < 20
