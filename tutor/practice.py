import ast
import hashlib
import math
import random
from decimal import Decimal
from pathlib import Path
from typing import Literal

import sympy
import yaml
from pint import UnitRegistry
from pint.errors import DimensionalityError, UndefinedUnitError
from pydantic import BaseModel, Field, model_validator


class TemplateError(ValueError):
    pass


UNIT_ALIASES = {
    "米": "m",
    "秒": "s",
    "千克": "kg",
    "牛": "N",
    "牛顿": "N",
    "米每秒": "m/s",
    "米每二次方秒": "m/s^2",
}
DIRECTION_ALIASES = {
    "向上": "up",
    "向下": "down",
    "向左": "left",
    "向右": "right",
    "正方向": "positive",
    "负方向": "negative",
    "与合力同向": "same_as_force",
    "与合力反向": "opposite_force",
}
UREG = UnitRegistry()


def _symbols(names: set[str], aliases: dict[str, str] | None = None) -> dict[str, sympy.Symbol]:
    canonical = {name: sympy.Symbol(name) for name in names}
    result = dict(canonical)
    for alias, name in (aliases or {}).items():
        if name in canonical:
            result[alias] = canonical[name]
    return result


def _convert_ast(node: ast.AST, symbols: dict[str, sympy.Symbol]):
    if isinstance(node, ast.Expression):
        return _convert_ast(node.body, symbols)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return sympy.Rational(str(node.value))
    if isinstance(node, ast.Name) and node.id in symbols:
        return symbols[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _convert_ast(node.operand, symbols)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
        left = _convert_ast(node.left, symbols)
        right = _convert_ast(node.comparators[0], symbols)
        operator = node.ops[0]
        if isinstance(operator, ast.Lt):
            return sympy.Lt(left, right)
        if isinstance(operator, ast.LtE):
            return sympy.Le(left, right)
        if isinstance(operator, ast.Gt):
            return sympy.Gt(left, right)
        if isinstance(operator, ast.GtE):
            return sympy.Ge(left, right)
        if isinstance(operator, ast.Eq):
            return sympy.Eq(left, right)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
        left = _convert_ast(node.left, symbols)
        right = _convert_ast(node.right, symbols)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if not right.is_number or abs(float(right)) > 6:
            raise TemplateError("指数必须是绝对值不超过 6 的常数")
        return left**right
    raise TemplateError("表达式包含不允许的语法")


def safe_expression(
    expression: str,
    names: set[str],
    aliases: dict[str, str] | None = None,
):
    if not 1 <= len(expression) <= 200:
        raise TemplateError("表达式长度不合法")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise TemplateError("表达式语法错误") from exc
    if sum(1 for _ in ast.walk(tree)) > 80:
        raise TemplateError("表达式过于复杂")
    return _convert_ast(tree, _symbols(names, aliases))


def _equation(
    expression: str,
    names: set[str],
    aliases: dict[str, str] | None = None,
) -> tuple[sympy.Expr, sympy.Expr]:
    if expression.count("=") != 1:
        raise TemplateError("等式必须包含一个等号")
    left, right = expression.split("=", 1)
    return safe_expression(left.strip(), names, aliases), safe_expression(right.strip(), names, aliases)


def _equivalent_equation(
    student: str,
    expected: str,
    names: set[str],
    aliases: dict[str, str],
    params: dict[str, Decimal] | None = None,
) -> bool:
    student_left, student_right = _equation(student, names, aliases)
    expected_left, expected_right = _equation(expected, names)
    substitutions = {sympy.Symbol(key): sympy.Rational(str(value)) for key, value in (params or {}).items()}
    student_eq = sympy.Eq(student_left.subs(substitutions), student_right.subs(substitutions))
    expected_eq = sympy.Eq(expected_left.subs(substitutions), expected_right.subs(substitutions))
    for name in names:
        symbol = sympy.Symbol(name)
        try:
            student_solutions = sympy.solve(student_eq, symbol)
            expected_solutions = sympy.solve(expected_eq, symbol)
        except (NotImplementedError, ValueError, TypeError):
            continue
        if len(student_solutions) == len(expected_solutions) == 1:
            if sympy.simplify(student_solutions[0] - expected_solutions[0]) == 0:
                return True
    return sympy.simplify((student_left - student_right) - (expected_left - expected_right)) == 0


class ParameterSpec(BaseModel):
    unit: str
    values: list[Decimal] | None = None
    min: Decimal | None = None
    max: Decimal | None = None
    step: Decimal | None = None

    @model_validator(mode="after")
    def validate_source(self):
        if self.values:
            return self
        if self.min is None or self.max is None or self.step is None or self.step <= 0 or self.min > self.max:
            raise ValueError("参数必须提供 values 或合法的 min/max/step")
        return self


class ToleranceSpec(BaseModel):
    relative: float = Field(default=0.01, ge=0, le=0.05)
    absolute: float = Field(default=0.001, ge=0)


class AnswerSpec(BaseModel):
    expression: str
    unit: str
    direction: str | None = None
    tolerance: ToleranceSpec = Field(default_factory=ToleranceSpec)


class RubricItem(BaseModel):
    id: str
    kind: Literal["equation", "substitution", "final_value", "final_unit", "direction"]
    points: int = Field(gt=0, le=100)
    expected: str | None = None


class GoldenCase(BaseModel):
    params: dict[str, Decimal]
    value: Decimal
    unit: str


class TemplateSpec(BaseModel):
    schema_version: Literal[1]
    id: str
    version: int = Field(gt=0)
    kp_ids: list[str] = Field(min_length=1)
    difficulty: int = Field(ge=1, le=5)
    type: Literal["numeric", "multi_step_numeric", "choice"]
    parameters: dict[str, ParameterSpec]
    constraints: list[str] = Field(default_factory=list)
    stem: str
    answer: AnswerSpec
    symbols: dict[str, list[str]] = Field(default_factory=dict)
    rubric: list[RubricItem]
    golden_cases: list[GoldenCase] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_contract(self):
        if sum(item.points for item in self.rubric) != 100:
            raise ValueError("rubric 分值之和必须为 100")
        if len({item.id for item in self.rubric}) != len(self.rubric):
            raise ValueError("rubric id 不能重复")
        parameter_names = set(self.parameters)
        equation_names = parameter_names | set(self.symbols)
        safe_expression(self.answer.expression, parameter_names)
        for constraint in self.constraints:
            safe_expression(constraint, parameter_names)
        for item in self.rubric:
            if item.kind in {"equation", "substitution"}:
                if not item.expected:
                    raise ValueError(f"rubric {item.id} 缺少 expected")
                _equation(item.expected, equation_names)
        if len(self.golden_cases) < 3:
            raise ValueError("每个模板至少需要 3 个 golden case")
        UREG.parse_units(self.answer.unit)
        for case in self.golden_cases:
            if set(case.params) != parameter_names:
                raise ValueError("golden case 参数必须与模板参数一致")
            substitutions = {
                sympy.Symbol(name): sympy.Rational(str(value)) for name, value in case.params.items()
            }
            actual = safe_expression(self.answer.expression, parameter_names).subs(substitutions)
            expected = (float(case.value) * UREG.parse_units(case.unit)).to(self.answer.unit).magnitude
            if not math.isclose(float(actual), expected, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("golden case 与答案表达式不一致")
        return self


class QuestionInstance(BaseModel):
    id: str
    template_id: str | None = None
    template_version: int | None = None
    kp_ids: list[str]
    type: str
    difficulty: int
    stem: str
    params: dict[str, dict]
    answer_spec: dict
    solution: list[str]
    rubric: list[dict]
    symbols: dict[str, list[str]]
    seed: int | None = None


class FinalAnswer(BaseModel):
    value: str = Field(min_length=1, max_length=80)
    unit: str = Field(default="", max_length=40)
    direction: str | None = Field(default=None, max_length=40)


class AnswerStep(BaseModel):
    kind: Literal["formula", "substitution"]
    expression: str = Field(min_length=1, max_length=200)


class CalculationAnswer(BaseModel):
    kind: Literal["calculation"] = "calculation"
    question_id: str
    final: FinalAnswer
    steps: list[AnswerStep] = Field(default_factory=list, max_length=8)


class RubricResult(BaseModel):
    id: str
    earned: int
    max: int
    evidence: str = ""


class GradeResult(BaseModel):
    question_id: str
    attempt_no: int
    correct: bool
    score: int
    final_value_ok: bool
    unit_ok: bool
    direction_ok: bool
    rubric_results: list[RubricResult]
    error_types: list[str]
    hint: str | None = None
    solution_revealed: bool = False
    solution: list[str] = Field(default_factory=list)


def stable_seed(*parts) -> int:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=False) & ((1 << 63) - 1)


def _parameter_values(spec: ParameterSpec) -> list[Decimal]:
    if spec.values:
        return spec.values
    count = int((spec.max - spec.min) / spec.step)
    return [spec.min + spec.step * index for index in range(count + 1)]


def _plain_number(value) -> str:
    simplified = sympy.simplify(value)
    if simplified.is_Integer:
        return str(int(simplified))
    if simplified.is_Rational and simplified.q <= 100:
        decimal = Decimal(int(simplified.p)) / Decimal(int(simplified.q))
        return format(decimal.normalize(), "f")
    return format(float(sympy.N(simplified, 12)), ".10g")


def instantiate_template(template: TemplateSpec, seed: int) -> QuestionInstance:
    rng = random.Random(seed)
    names = set(template.parameters)
    selected: dict[str, Decimal] | None = None
    for _ in range(50):
        candidate = {name: rng.choice(_parameter_values(spec)) for name, spec in template.parameters.items()}
        substitutions = {sympy.Symbol(name): sympy.Rational(str(value)) for name, value in candidate.items()}
        if all(bool(safe_expression(rule, names).subs(substitutions)) for rule in template.constraints):
            selected = candidate
            break
    if selected is None:
        raise TemplateError(f"模板 {template.id} 在 50 次内无法满足约束")

    substitutions = {sympy.Symbol(name): sympy.Rational(str(value)) for name, value in selected.items()}
    value = safe_expression(template.answer.expression, names).subs(substitutions)
    rendered = {name: format(number.normalize(), "f") for name, number in selected.items()}
    stem = template.stem.format(**rendered)
    question_id = "q_" + hashlib.sha256(
        f"{template.id}:{template.version}:{seed}".encode("utf-8")
    ).hexdigest()[:24]
    params = {
        name: {"value": rendered[name], "unit": template.parameters[name].unit}
        for name in selected
    }
    answer_spec = {
        "value": _plain_number(value),
        "unit": template.answer.unit,
        "direction": template.answer.direction,
        "tolerance": template.answer.tolerance.model_dump(),
    }
    solution = [
        f"使用关系式：{template.answer.expression}",
        "代入：" + "，".join(f"{name}={rendered[name]} {template.parameters[name].unit}" for name in selected),
        f"结果：{answer_spec['value']} {answer_spec['unit']}",
    ]
    return QuestionInstance(
        id=question_id,
        template_id=template.id,
        template_version=template.version,
        kp_ids=template.kp_ids,
        type=template.type,
        difficulty=template.difficulty,
        stem=stem,
        params=params,
        answer_spec=answer_spec,
        solution=solution,
        rubric=[item.model_dump() for item in template.rubric],
        symbols=template.symbols,
        seed=seed,
    )


def load_templates(directory: Path) -> list[TemplateSpec]:
    templates: list[TemplateSpec] = []
    seen: set[tuple[str, int]] = set()
    for path in sorted(directory.rglob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        items = raw.get("templates", []) if isinstance(raw, dict) else raw
        for item in items:
            template = TemplateSpec.model_validate(item)
            key = (template.id, template.version)
            if key in seen:
                raise TemplateError(f"模板版本重复: {template.id} v{template.version}")
            seen.add(key)
            templates.append(template)
    return templates


def _normalize_unit(unit: str) -> str:
    stripped = unit.strip()
    return UNIT_ALIASES.get(stripped, stripped)


def _compare_final(question: QuestionInstance, answer: CalculationAnswer) -> tuple[bool, bool, bool]:
    expected = question.answer_spec
    tolerance = expected["tolerance"]
    try:
        user_value = float(Decimal(answer.final.value.strip()))
        expected_value = float(Decimal(expected["value"]))
    except Exception:
        return False, False, False

    unit_ok = False
    value_ok = math.isclose(
        user_value,
        expected_value,
        rel_tol=tolerance["relative"],
        abs_tol=tolerance["absolute"],
    )
    try:
        user_unit = _normalize_unit(answer.final.unit)
        if user_unit:
            converted = (user_value * UREG.parse_units(user_unit)).to(expected["unit"])
            unit_ok = True
            value_ok = math.isclose(
                converted.magnitude,
                expected_value,
                rel_tol=tolerance["relative"],
                abs_tol=tolerance["absolute"],
            )
    except (DimensionalityError, UndefinedUnitError, ValueError):
        unit_ok = False

    required_direction = expected.get("direction")
    provided_direction = DIRECTION_ALIASES.get(answer.final.direction or "", answer.final.direction)
    direction_ok = required_direction is None or provided_direction == required_direction
    return value_ok, unit_ok, direction_ok


def grade_question(question: QuestionInstance, answer: CalculationAnswer, attempt_no: int) -> GradeResult:
    value_ok, unit_ok, direction_ok = _compare_final(question, answer)
    names = set(question.params) | set(question.symbols)
    aliases = {alias: canonical for canonical, values in question.symbols.items() for alias in values}
    params = {name: Decimal(item["value"]) for name, item in question.params.items()}
    results: list[RubricResult] = []
    malformed = False

    for rubric in question.rubric:
        earned = 0
        evidence = ""
        kind = rubric["kind"]
        if kind == "final_value" and value_ok:
            earned = rubric["points"]
            evidence = answer.final.value
        elif kind == "final_unit" and unit_ok:
            earned = rubric["points"]
            evidence = answer.final.unit
        elif kind == "direction" and direction_ok:
            earned = rubric["points"]
            evidence = answer.final.direction or ""
        elif kind in {"equation", "substitution"}:
            step_kind = "formula" if kind == "equation" else "substitution"
            for step in (item for item in answer.steps if item.kind == step_kind):
                try:
                    if _equivalent_equation(
                        step.expression,
                        rubric["expected"],
                        names,
                        aliases,
                        params if kind == "substitution" else None,
                    ):
                        earned = rubric["points"]
                        evidence = step.expression
                        break
                except TemplateError:
                    malformed = True
        results.append(RubricResult(id=rubric["id"], earned=earned, max=rubric["points"], evidence=evidence))

    error_types: list[str] = []
    if not value_ok:
        error_types.append("calculation")
    if not unit_ok:
        error_types.append("unit")
    if not direction_ok:
        error_types.append("direction")
    if any(item.id == "formula" and item.earned == 0 for item in results):
        error_types.append("formula")
    if malformed:
        error_types.append("malformed")

    correct = value_ok and unit_ok and direction_ok
    first_failed = next((item.id for item in results if item.earned < item.max), None)
    hints = {
        "formula": "先明确研究对象并写出连接已知量与所求量的物理关系式。",
        "substitute": "检查代入的数值是否与关系式中的物理量一一对应。",
        "magnitude": "检查计算过程和有效数字。",
        "unit": "数值接近正确，请检查单位及其量纲。",
        "direction": "结合正方向和受力方向重新判断结果方向。",
    }
    reveal = not correct and attempt_no >= 2
    return GradeResult(
        question_id=question.id,
        attempt_no=attempt_no,
        correct=correct,
        score=sum(item.earned for item in results),
        final_value_ok=value_ok,
        unit_ok=unit_ok,
        direction_ok=direction_ok,
        rubric_results=results,
        error_types=list(dict.fromkeys(error_types)),
        hint=hints.get(first_failed) if not correct and not reveal else None,
        solution_revealed=reveal,
        solution=question.solution if reveal or correct else [],
    )
