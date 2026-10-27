# 高中物理辅导 Agent 设计定稿（v1.2）

> 2026-10-26 评审通过。合并 v1（总体架构）与 v1.1（物理 / 自选知识点 / 拍照）修订，并落实全部评审决定。

## 0. 评审决定

| 问题 | 决定 |
|---|---|
| 学科 | 高中物理起步，学科可配置扩展 |
| 教材 | 人教版 2019 新教材，MVP 覆盖必修第一册整册 |
| 知识点选择 | 学生在知识树上自选，显式选择优先于 LLM 判断 |
| 拍照 | 支持；视觉模型用通义千问 Qwen-VL（DashScope OpenAI 兼容接口） |
| 照片评测集 | 由开发方构建（合成渲染 + 公开图片） |
| 题源 | 由开发方从公开网络整理，逐题记录来源，仅本机自用不再分发 |
| 作业答案 | 默认引导模式：先提示思路，学生作答后再给完整解 |
| 手写解答批改 | 推迟到 v2；MVP 只识别题目 |
| 用户角色 | 只做学生端 |
| 隐私 | 不做专门的隐私处理（不脱敏、不去 EXIF、trace 全量上报） |
| 部署 | 本机 Docker Compose |

## 1. 目标与边界

MVP 闭环：选知识点 → 讲解 → 练习 → 批改 → 错题入库 → 到期复习；或拍题 → 确认识别结果 → 讲解 / 录入错题。

不做：作文、口语、语音、家长/老师端、班级管理、支付。

## 2. 关键设计原则

| # | 原则 | 理由 |
|---|---|---|
| D1 | LLM 只做理解与生成；判分、记账、排期是确定性代码 | 可单测、可保证正确 |
| D2 | 业务数据（作答、错题、掌握度）存自有表；checkpoint 只存会话进度 | checkpoint 不适合查询统计，错题集跨会话 |
| D3 | 参数化出题：LLM 写模板，代码取值并用 sympy 算答案、pint 校验量纲，独立模型复核 | LLM 算数不可靠，物理题一个单位错整题错 |
| D4 | 知识点目录代替向量检索 | 规模小时精确 ID 更可控、可评测 |
| D5 | 学生作答、选点确认、识别确认都用 `interrupt()` 挂起 | 关网页后可续答，复用 HITL 模式 |
| D6 | 默认引导模式 | 拍照后“搜题抄答案”风险更高 |
| D7 | 先写标签契约再标数据 | 评测字段定义不清会让门禁失真 |
| D8 | 学生显式选择的知识点不交给 LLM 猜 | 确定性路径可 100% 单测 |
| D9 | 识别结果必须学生确认后才使用 | 识别错一个数字整题就错 |
| D10 | 图片只按引用存，不进 checkpoint | 避免每步复制大对象 |

## 3. 架构

```
                 ┌────────────── 父图 TutorGraph（AsyncPostgresSaver）──────────────┐
学生消息/图片 ─► guard ─► router ─┬─► explain  子图（讲解）──────────────┐
               （安全） （意图）   ├─► practice 子图（选题→作答→批改）──┼─► respond ─► SSE
                                 ├─► mistakes 子图（错题/复习）───────┤
                                 └─► ingest   子图（拍照识别+确认）───┘

领域服务（纯 Python，可单测）：
  Catalog · TemplateEngine(sympy+pint) · Grader · MistakeBook · Scheduler(Leitner) · Mastery
模型层：按角色配置 router / explain / generate / verify / vision → 供应商 + 模型
```

### 3.1 路由

- 请求带 `kp_id` + `mode` 时走确定性路由，不调用 LLM。
- 自由输入时 LLM 给出 1–3 个候选知识点，用选择卡片（interrupt）请学生确认。
- 持久化 `focus_kp_id` 与 `active_mode`；练习进行中，消息默认视为答案。
- 前置知识点掌握度低时只提醒，不阻止。

### 3.2 子图

**explain**：读取知识点卡片（定义、规律、常见误区、例题）+ 学生掌握度，输出必须引用 `kp_id`，卡片外的结论不得编造；讲完提议练习。

**practice**：

```
select ─► (题库命中?) ─否─► 模板实例化（代码算答案） ─┐
   │是                                              │
   └──────────────────► ask（interrupt 等作答）◄─────┘
                         ─► grade ─► record ─► 下一题 / summary
```

- `ask` 节点在 `interrupt()` 之前不得有副作用（恢复时节点会从头重跑）。
- 答案结构化为 `{数值, 单位, 方向}`；pint 判单位等价，默认相对误差 1%；题干必须写明 g 的取值。
- M2 自动判分仅覆盖选择题、数值题和结构化计算步骤；列式 / 代入 / 结果由规则引擎评分。自由文本解答可给辅导反馈，但不计入正式分数与掌握度。
- 错因：受力分析、公式或规律误用、单位换算、矢量方向、计算、审题遗漏。

**模板核验流水线**：

```
人工 / LLM 离线起草 ─► 静态与量纲检查 ─► 3 组固定向量 + 1000 随机种子 ─► 模型复核（仅辅助）+ 人工审阅 ─► verified
```

AI 生成题 v1 只出能用文字完整描述的情境；带图题只来自题库。

**mistakes**：`record` 节点直接写表；Leitner 五盒排期；连续两次答对标记已掌握；LLM 只用于变式题（复用模板）与薄弱点分析。

**ingest**：

```
upload ─► 内容检查 ─► Qwen-VL 结构化识别 ─► confirm（interrupt，学生可改）─► explain / mistakes
                     {题干 LaTeX, 图示描述, 已知量[数值+单位], 所求, 置信度}
```

场景：A 拍题求讲解；B 拍错题录入错题本（补录“我的答案”）。场景 C 手写解答批改为 v2。

## 4. 数据模型

```
schema_migrations(version, applied_at)
students        (id, created_at)
sessions        (thread_id, student_id, created_at)   -- 焦点知识点与模式存于图状态
knowledge_points(id, chapter, section, name, prereq_ids[], card_md, status[draft|ready], version)
question_templates(id, kp_ids[], scenario, params, solve_expr, answer_unit, verified, ...)
questions       (id, kp_ids[], type, difficulty, stem, answer, solution, figure_key,
                 source[bank|template], source_url, verified)
attempts        (id, student_id, question_id, thread_id, answer, correct, score,
                 error_type, graded_by[rule|llm], created_at)
mistakes        (id, student_id, question_id, kp_ids[], error_type, box,
                 next_review_at, status[active|mastered])
mastery         (student_id, kp_id, score, attempts, updated_at)
uploads         (id, student_id, storage_key, sha256, mime, created_at)
recognitions    (id, upload_id, model, raw_json, confirmed_json, confidence, created_at)
```

各表在用到它的里程碑才建。迁移为编号 SQL 文件，由 `migrate.py` 在 advisory lock 下串行执行，并在同一把锁内调用 checkpointer `setup()`；Compose 中作为一次性 `migrate` 服务先于 app 运行。取锁用 `pg_try_advisory_lock` 轮询而非阻塞的 `pg_advisory_lock`：`setup()` 含 `CREATE INDEX CONCURRENTLY`，会等待阻塞在锁上的会话，导致死锁（已用 3 进程并发迁移复现并验证修复）。`recognitions` 的修改前后差异沉淀为视觉评测标注。

## 5. API

| 端点 | 说明 |
|---|---|
| `GET /catalog?student_id=` | 知识树 + 可用状态 + 掌握度 |
| `POST /chat` | SSE；可选 `kp_id`、`mode`、`image_id` |
| `POST /answer` | 恢复 interrupt（作答 / 选点 / 识别确认）；无待处理时 409 |
| `POST /upload` | 上传图片，返回 `image_id`（M4） |
| `GET /mistakes`、`POST /review/start` | 错题集与复习（M3） |
| `GET /history` | 会话快照 |
| `/healthz`、`/readyz` | 存活 / 就绪 |

SSE 事件：`token`、`question`、`grade`、`choice`、`confirm`、`mistake_added`、`kp_cited`、`interrupt`、`done`、`error`。

学生身份：前端生成并保存 `student_id`，首次使用时自动登记；`thread_id` 绑定 `student_id`，不匹配返回 403。

## 6. 安全

- guard 节点（规则 + LLM）：拦截不良内容与越狱；自伤倾向返回固定话术与求助热线，安全评测须 100%。
- 上传：限制大小和类型、校验文件头。
- 限流：按学生限制请求频率与每日 token 预算。

## 7. 质量保障

| 层级 | 对象 | 门禁 |
|---|---|---|
| 单元 | Catalog、显式路由、TemplateEngine、Grader、Scheduler、迁移器 | 100% |
| 契约 | FastAPI + fake graph | SSE 帧、409、403、resume |
| 图集成 | fake LLM 跑完整闭环 | 不重复出题、跨进程恢复 |
| 评测：router（自由输入） | 意图 + 候选知识点 | 意图 ≥95%，候选含正确知识点 ≥90% |
| 评测：出题 | 模板核验 | 核验通过率 ≥80%，下发题错误率 ≤1%（人工抽检） |
| 评测：批改 | 人工标注答案 | 准确率 ≥95%，把对判错 ≤2% |
| 评测：讲解 | LLM 评委 + 抽检 | 无事实错误、引用正确 |
| 评测：视觉 | 照片集 | 关键量 ≥95%，题干 ≥0.95，图示要素 ≥90%，错误样本低置信 ≥80% |
| 评测：安全 | 红队集 | 100% |

每套评测分 golden / holdout / baseline，带数据集 SHA 校验。CI：每次提交跑 ruff + pytest + docker 冒烟；真实模型评测每周或手动触发。

## 8. 里程碑

| 阶段 | 内容 | 退出标准 |
|---|---|---|
| M0 骨架 ✅ | 项目结构、迁移器、必修一知识目录、父图 + 桩子图、API、Docker、CI | ruff + pytest 通过；compose 后 readyz 200；`/catalog`、`/chat`、`/answer` 跨重启恢复 |
| M1 目录 + 讲解 ✅ | 模型配置层（DeepSeek / DashScope 角色），第二、四章 20 张知识点卡片，知识树界面，explain 子图，自由输入候选确认 | 33 个测试通过；显式选点不调用 LLM；真实 DeepSeek 抽检牛顿第二定律、自由落体无事实错误；浏览器完成候选确认与刷新恢复 |
| M2 练习闭环 ✅ | 参数化模板 + 精选网络题源、SymPy/Pint 判分、结构化步骤分、引导式重试 | 14 个模板、30 道精选题；14,000 个稳定种子、200 例批改评测、300 题复核、跨 worker/重启闭环全部过门禁 |
| M3 错题集 | 入库、Leitner、变式题、掌握度着色 | 模拟时钟跑通 7 天复习 |
| M4 拍照 | 上传、Qwen-VL 识别、确认卡片、场景 A/B、照片评测集 | 视觉门禁达标 |
| M5 上线准备 | guard、限流、holdout、docker 冒烟、浏览器端到端 | 安全评测 100% |

## 9. 外部依赖与待办

- `DASHSCOPE_API_KEY`：M4 前需在本机环境变量中设置。
- 网络题源：仅本机自用，逐题记录 `source_url`；遵守站点条款，不做大规模抓取。
- M1 已固定 `langchain-openai==1.6.0`（本机阿里云镜像无 1.7.2）；M2 新增 `sympy` 与 `pint`，版本必须同时在本机镜像和 Docker 构建源可用。

## 10. M2 练习闭环详细设计（评审稿）

> 状态：已于 2026-10-27 实施并通过本机门禁。M2 只建设可确定性验证的练习闭环，不提前实现 M3 错题排期与掌握度更新。

### 10.1 目标与边界

M2 必须完成：

1. 第二、四章不少于 14 个参数化计算题模板，每个模板可生成大量可复现题目。
2. 不少于 30 道人工审阅的公开网络精选题，保留来源与改编记录。
3. 支持选择题、数值题、带单位 / 方向的计算题，以及结构化步骤评分。
4. 练习流程升级为“选题 → 作答 → 规则判分 → 错误提示 → 最多一次重试 → 完整反馈”。
5. 题目、标准答案、学生提交和评分进入业务表；checkpoint 只保存流程位置与引用 ID。
6. 建立出题与判分的 golden / holdout / baseline 评测，达到第 10.10 节门禁。

M2 不做：运行时让 LLM 临时编题、图片题模板、手写过程识别、自由文本解答正式评分、错题盒与掌握度更新。LLM 可以离线辅助起草模板或解释错误，但不能决定正式分数。

### 10.2 关键决策（ADR）

| ADR | 决定 | 备选与取舍 |
|---|---|---|
| M2-01 模板存储 | 版本化 YAML 是评审源；启动 / 导入时验证后写入 Postgres 快照 | 只存 DB 编辑方便但难审查；运行时 LLM 生成不可复现，拒绝采用 |
| M2-02 求解与判分 | SymPy 处理代数等价，Pint 处理量纲与单位，业务规则汇总分数 | LLM 判分快但不可稳定复测；字符串精确匹配无法识别等价表达式 |
| M2-03 表达式安全 | 自建受限 AST，仅允许数字、已声明符号、等号及 `+ - * / **` | 不对学生输入直接调用 `eval`、`sympify` 或开放的 `parse_expr` |
| M2-04 网络题源 | 人工精选、人工录入、去重并记录复核人；不做批量爬虫 | 自动抓取成本低但有版权、页面变化、提示注入和质量风险 |
| M2-05 引导模式 | 首次答错只显示首个失败评分点对应的提示；第二次仍错才展示完整解 | 首次直接给答案不符合已批准的默认引导模式 |
| M2-06 运行时依赖 | 选题、实例化、判分全程不调用外部模型 | 模型故障时练习仍可用，也使 CI 与评测完全离线 |

### 10.3 组件与数据流

```text
Template YAML ─► TemplateLoader ─► SchemaValidator ─► DimensionValidator ─► TemplateRepository
                                                         │
Curated JSONL ─► SourceImporter ─► 去重 / 来源审计 ───────┤
                                                         ▼
学生选练习 ─► QuestionSelector ─► TemplateEngine ─► questions 快照 ─► ask(interrupt)
                                                        │                 │
                                                        │       PracticeAnswer
                                                        │                 ▼
                                                        └────────► AnswerParser
                                                                      │
                                                                      ▼
                                                    Grader(SymPy + Pint + Rubric)
                                                                      │
                                            ┌──────── 首次错误 ────────┴──── 正确 / 二次错误 ─┐
                                            ▼                                                 ▼
                                      hint → ask(interrupt)                            record → feedback
```

职责边界：

- `TemplateLoader`：读取 YAML、版本和 SHA；不生成题目。
- `TemplateEngine`：根据稳定种子取参数、检查约束、计算派生量、渲染题干和答案快照。
- `QuestionSelector`：按知识点、难度、近期待答历史选择，避免短期重复。
- `AnswerParser`：把 API 边界输入规范化为数值、单位、方向和步骤；解析失败返回可解释错误，不猜答案。
- `Grader`：纯函数；输入题目答案规范与学生答案，输出 `GradeResult`，不访问数据库或 LLM。
- `PracticeRepository`：保存模板快照、题目实例和每次提交；写入须幂等。

### 10.4 参数化模板契约

模板目录按教材组织：`data/templates/physics_b1/chapter_2/*.yaml`、`chapter_4/*.yaml`。模板 ID 永不复用；修改题意、答案或评分规则必须递增 `version`。

```yaml
schema_version: 1
id: b1.4.3.net-force-acceleration
version: 1
kp_ids: [b1.4.3.1]
difficulty: 2
type: multi_step_numeric
scenario: 水平面上物体受恒定合力
parameters:
  mass: {unit: kg, values: [1, 2, 4, 5]}
  force: {unit: N, min: 4, max: 40, step: 2}
constraints:
  - force / mass <= 20
stem: "质量为 {mass:~P} 的物体受到 {force:~P} 的水平合力，求加速度。"
answer:
  expression: force / mass
  unit: m/s^2
  direction: same_as_force
  tolerance: {relative: 0.01, absolute: 0.001}
rubric:
  - {id: formula, kind: equation, expected: F_net = mass * acceleration, points: 30}
  - {id: substitute, kind: substitution, expected: acceleration = force / mass, points: 30}
  - {id: magnitude, kind: final_value, points: 25}
  - {id: unit, kind: final_unit, points: 10}
  - {id: direction, kind: direction, points: 5}
golden_cases:
  - {params: {mass: 2, force: 6}, value: 3, unit: m/s^2}
```

约束：

- 每个模板评分项总分必须为 100；`kp_ids` 必须存在于 Catalog。
- 所有参数和派生量必须带量纲；题干出现重力加速度时必须明确 `g` 的取值。
- 随机参数只允许离散集合或有界等步长范围；拒绝无限重采样。
- 每次实例化最多尝试 50 组参数，仍不满足约束则模板失败并停止下发。
- `instance_seed = SHA256(student_id + thread_id + turn_id + template_id + version)` 的前 64 位整数；同一轮重试、进程重启和多 worker 得到同一实例。
- `questions` 保存渲染后的题干、参数、答案与 rubric 快照。之后模板升级不得改变已下发题。

### 10.5 答案与评分契约

`POST /answer` 保持候选知识点使用字符串；练习题改为结构化对象：

```json
{
  "answer": {
    "kind": "calculation",
    "question_id": "q_...",
    "final": {"value": "3", "unit": "m/s^2", "direction": "same_as_force"},
    "steps": [
      {"kind": "formula", "expression": "F_net = m*a"},
      {"kind": "substitution", "expression": "a = 6/2"}
    ]
  }
}
```

API 使用判别联合：`str | ChoiceAnswer | CalculationAnswer`。待处理 interrupt 为知识点选择时只接受候选 ID；为练习题时必须校验 `question_id` 与当前 pending 一致，防止把旧页面答案交给新题。

评分顺序固定：

1. **语法**：受限 AST 解析表达式；未知符号、函数调用、属性访问、超长输入直接拒绝。
2. **数值**：转 `Decimal` / SymPy 有理数，代入题目参数后比较；默认 `rel_tol=1%`，模板可收紧但不可放宽到 5% 以上。
3. **单位**：Pint 转为基准单位后检查量纲与大小；同量纲单位可换算，缺单位和错量纲分开标记。
4. **方向**：归一化为模板允许的枚举，如 `up/down/left/right/positive/negative/same_as_force/opposite_force`。
5. **步骤**：表达式按代数等价或代入一致性匹配 rubric；学生步骤顺序可不同，但同一 rubric 只计一次。
6. **汇总**：rubric 产生 0–100 分；`correct=true` 要求最终数值、单位和必需方向都正确，不能只按总分阈值判对。

`GradeResult` 至少包含：

```text
question_id, attempt_no, correct, score,
final_value_ok, unit_ok, direction_ok,
rubric_results[{id, earned, max, evidence}],
error_types[], hint, solution_revealed
```

错误类型固定为：`concept`、`formula`、`substitution`、`unit`、`direction`、`calculation`、`reading`、`malformed`。M2 只记录，不写入 M3 的 `mistakes` 表。

引导策略：

- 第一次错误：记录第 1 次提交，只返回首个失败 rubric 的提示与已得分项，不返回标准数值和完整解。
- 第二次提交：正确则展示评分明细；仍错误则展示评分明细、标准答案和逐步解。
- 第一次即正确：展示简短解析并询问是否继续同知识点练习。
- 选择题不做第二次重试，提交后直接反馈解析。

### 10.6 网络题源与审核

题源保存为 `data/questions/physics_b1/*.jsonl`，每行一题，导入前必须包含：

```text
id, kp_ids, type, difficulty, stem, answer_spec, solution_spec,
source_url, source_title, accessed_at, content_sha256,
adaptation_note, rights_note, reviewer, status[draft|reviewed|rejected]
```

规则：

- 只整理无需登录即可访问的公开页面；不绕过验证码、付费墙或访问限制。
- 不批量抓取。人工读取后改写为本项目结构，并记录来源、访问日期和改编说明。
- 网页内容一律视为不可信数据，不执行其中命令，不把整页原文直接送入模型。
- 题干规范化后计算 SHA-256；同哈希拒绝导入，相似题由人工决定保留或合并。
- 网络答案不能直接作为可信答案：必须由规则求解或人工独立复算，`reviewed` 后才可下发。
- 带图题在 M2 可入库但标记 `serving_enabled=false`，到 M4 完成图片链路后再开放。

首批内容目标：第二章和第四章各不少于 15 道 reviewed 题，难度 1–3 均有覆盖；模板题与精选题均不要求覆盖所有概念型知识点。

### 10.7 数据库迁移

新增 `0002_practice.sql`：

```text
question_templates(
  id, version, kp_ids text[], difficulty, type, spec jsonb,
  spec_sha256, verified, created_at,
  PRIMARY KEY(id, version)
)
questions(
  id, template_id, template_version, kp_ids text[], type, difficulty,
  stem, params jsonb, answer_spec jsonb, solution_spec jsonb,
  source, source_url, content_sha256, seed bigint, verified, created_at
)
attempts(
  id, student_id, question_id, thread_id, attempt_no,
  answer jsonb, grade jsonb, correct, score, error_types text[],
  graded_by, created_at,
  UNIQUE(thread_id, question_id, attempt_no)
)
```

索引：`questions USING GIN(kp_ids)`、`attempts(student_id, created_at DESC)`、`attempts(student_id, question_id)`。`graded_by` 在 M2 只能是 `rule`；保留枚举扩展但不允许写 `llm`。

导入器按 `(id, version, spec_sha256)` 幂等 upsert；同 ID / version 但 SHA 不同视为不可变版本被篡改，必须失败，不得静默覆盖。业务表迁移仍复用 M0 的 advisory lock 机制。

### 10.8 Practice 子图与幂等性

```text
select_question → ask(interrupt) → parse → grade → record
                                         │
                        首次错误 ─────────┴──► hint → ask_retry(interrupt)
                                         │
                        正确 / 二次错误 ────► feedback → END
```

状态只保存小对象和引用：`question_id`、`attempt_no`、`grade_result`、`reply`；完整题目与答案规范从 `questions` 读取。`select_question` 是唯一允许创建题目实例的节点；`ask` / `ask_retry` 在 `interrupt()` 前不能写数据库或日志副作用。

幂等规则：

- API 每轮生成 `turn_id` 并写入图输入；模板实例 ID 由稳定种子生成。
- `record` 用 `(thread_id, question_id, attempt_no)` 唯一约束；重复 resume 返回既有评分。
- 同一 thread 有 pending interrupt 时继续 `/chat` 仍返回 409。
- 两个并发 `/answer` 只有一个推进 checkpoint；另一个返回 409，不得产生重复 attempt。
- 已下发问题即使模板文件被删除，也依靠 `questions` 快照完成恢复与判分。

选题顺序：优先 reviewed 题库中该学生未做过的题；其次实例化 verified 模板；全部做过后选择最久未做且最近 10 题未出现的题。M2 难度默认 2，UI 暂不增加自适应难度。

### 10.9 API、SSE 与界面

新增或扩展：

- `POST /answer`：接受结构化练习答案；请求体最大 16 KiB，步骤最多 8 条，每条表达式最多 200 字符。
- `GET /history`：pending 题只返回题干与答题格式，不返回 `answer_spec` / `solution_spec`。
- SSE `interrupt`：题目 payload 增加 `question_id`、`type`、`difficulty`、`attempt_no`、`fields`。
- SSE `grade`：返回公开评分明细、错误类型、是否可重试；绝不返回隐藏表达式。
- SSE `hint`：首次错误返回单条提示。
- SSE `token`：最终辅导反馈。

“引力场”界面中的练习卡片增加：数值、单位、方向三个最终答案字段；可展开的“写出过程”区域；提交后显示 100 分制 rubric 条；首次错误时原卡片保留并进入第 2 次作答。刷新或 app 重启后必须恢复题目、已完成的第 1 次评分和当前重试状态。

### 10.10 测试与评测门禁

#### A. 单元与性质测试

- 模板 schema、重复 ID、未知知识点、评分和不为 100、非法表达式：100% 拒绝。
- 每个模板固定 3 个 golden case：题干参数、答案数值、单位和解题步骤全部精确匹配。
- 每个模板跑 1000 个稳定种子：无异常、参数满足约束、答案量纲一致、相同种子输出完全相同。
- Grader 覆盖单位换算、科学计数法、1% 边界、零值、方向、等价公式、重复步骤、恶意表达式和超长输入。
- 纯本地性能：模板实例化 p95 < 20 ms；单次判分 p95 < 50 ms。

#### B. 数据集评测

`evals/grading_golden.jsonl` 不少于 150 例，`evals/grading_holdout.jsonl` 不少于 50 例，至少包含：

- 正确答案及等价单位 30%；
- 数值边界与精度 15%；
- 错单位 / 错量纲 15%；
- 方向错误 10%；
- 等价 / 错误步骤 20%；
- 畸形与攻击输入 10%。

门禁：

- `correct` 分类准确率 ≥95%；
- 正确答案被判错（false negative）≤2%；
- 单位 / 方向错误识别率各 ≥95%；
- rubric 各项与人工标签一致率 ≥90%；
- 相比 baseline：总准确率下降不得超过 2 个百分点，任一类别下降不得超过 5 个百分点；
- 数据集带 SHA，`--only` 调试不得写 baseline。

#### C. 题目质量

- 模板提交后通过自动核验的比例 ≥80%；未通过模板不得进入运行库。
- 从 verified 模板固定抽 300 个实例人工审查，致命错误必须为 0；按“3/n”规则，0/300 对应错误率 95% 上界约 1%。
- 30 道网络题的来源字段、独立复算、知识点标注和去重审计 100% 完整。

#### D. 集成与端到端

- fake repository 跑：首次答错 → hint → 二次答对；首次答对；二次仍错；单位错但数值对；并发重复提交。
- Postgres 跑：题目实例、两次 attempt、评分 JSON 和 checkpoint 一致。
- Docker 两 worker：中断后重启 app，恢复同一道题和同一参数；答案可由另一 worker 判分。
- 浏览器：结构化作答、评分条、重试、完整解析、刷新恢复；控制台无错误。
- 无 `DEEPSEEK_API_KEY` 时完整练习闭环仍通过，证明运行时不依赖 LLM。

### 10.11 实施顺序

| 子阶段 | 交付物 | 完成条件 |
|---|---|---|
| M2.0 契约先行 | 模板 / Answer / GradeResult schema，grader golden + holdout 骨架 | 失败样例先进入测试，标签契约冻结 |
| M2.1 领域内核 | 受限表达式、TemplateEngine、Pint 单位、Grader | 全部纯单测和性质测试通过 |
| M2.2 持久化与内容 | `0002_practice.sql`、Repository、YAML / JSONL 导入器、首批模板与题库 | 幂等导入，≥14 模板、≥30 reviewed 题 |
| M2.3 图与 API | practice 子图、结构化 `/answer`、SSE grade / hint | 两次作答、并发保护、跨重启恢复 |
| M2.4 UI 与门禁 | 结构化答题卡、评分条、评测脚本、CI / 周期评测 | 第 10.10 节全部门禁通过 |

### 10.12 M2 退出标准

只有同时满足以下条件才标记 M2 完成：

1. ≥14 个 verified 模板、≥30 道 reviewed 精选题，覆盖第二章和第四章核心计算知识点。
2. 每模板 3 个 golden case + 1000 随机种子全通过；300 题人工抽检 0 个致命错误。
3. 判分准确率 ≥95%，正确答案误判率 ≤2%，baseline 无超阈值回退。
4. 首次错误提示、二次作答、评分明细与完整解析在 API 和浏览器中闭环。
5. Postgres、多 worker、app 重启、重复提交与无模型密钥场景全部验证。
6. Ruff、pytest、Docker 冒烟与 M2 评测工作流全部通过；GitHub CI 若尚未实际运行，必须明确标注。

### 10.13 实施验收记录（2026-10-27）

- 内容：14 个 verified 模板、30 道 reviewed 精选题，每模板 3 个 golden case；Postgres 实测导入数量为 `14 / 30`。
- 性质与题目质量：每模板 1000 个稳定种子，共 14,000 个实例全部通过；实例化 p95 为 0.633 ms；300 题固定抽检清单致命错误为 0。
- 判分评测：golden 150 例、holdout 50 例；`correct`、单位错误、方向错误、rubric 一致率均为 100%，正确答案误判率为 0；判分 p95 分别为 26.53 ms 与 45.67 ms。
- 自动测试：51 个 pytest 用例与 Ruff `E9,F` 全部通过；练习闭环在未设置 `DEEPSEEK_API_KEY` 时通过。
- 持久化与并发：Docker 默认两个 worker；已验证由另一 worker 续答、app 重启恢复同题同参数、两次 attempt 与评分 JSON 落库、完成后重复提交返回 409。验收会话记录为 2 个 attempt、16 个 checkpoint。
- 浏览器：已验证结构化作答、0/100 分项评分、首次提示、刷新恢复、第 2 次 100/100、完整解析与控制台无错误。
- 工作流：`.github/workflows/ci.yml` 已升级为结构化续答冒烟，`.github/workflows/eval.yml` 已接入 M2 golden / holdout / 性能门禁；本机全部通过，GitHub 托管 CI 尚未实际运行。
- 首批公开题源主题来自 [中国教育在线·匀变速运动](https://gaokao.eol.cn/zhidao/wl/201309/t20130922_1020380.shtml)、[中国教育在线·牛顿运动定律](https://gaokao.eol.cn/huodong/zhuanti/gkmzjt/spgklz/200911/t20091111_420223.shtml) 与 [乐享物理·超重和失重](https://enjoyphysics.cn/Article2481)，题目均独立改写与复算。

### 10.14 风险与缓解

| 风险 | 缓解 |
|---|---|
| SymPy 表达式解析引入代码执行 | 受限 AST 白名单；长度、节点数、指数上限；恶意输入回归集 |
| 单位中文别名造成歧义 | API 内维护小型显式别名字典；展示单位与内部 Pint 单位分离 |
| 随机模板偶发无解 / 不合理值 | 有界参数、约束静态检查、50 次上限、1000 种子门禁 |
| 网络题答案本身错误 | 独立复算 + reviewer + reviewed 状态；网络答案不直接入正式 answer_spec |
| 步骤表达方式过多导致误判 | M2 只接受结构化步骤；自由文本可反馈但不计正式分 |
| checkpoint 与业务记录不一致 | 题目快照先落库、attempt 幂等写、状态只持有 ID、故障注入测试 |
| 首批内容制作量过大 | 先覆盖第二 / 四章核心计算点；概念题、图题和开放题不强求自动评分 |
