import sys, os, re, json, random, datetime, argparse, shutil
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
from pathlib import Path
import requests
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ── 客户端 ─────────────────────────────────────────────────────────────────
def _make_advisor_client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("ADVISOR_API_KEY") or "placeholder",
        base_url=os.getenv("ADVISOR_BASE_URL", "http://localhost:11434/v1"),
    )

llm_advisor = _make_advisor_client()
ADVISOR_MODEL    = os.getenv("ADVISOR_MODEL", "Qwen/Qwen3.5-27B-FP8")
DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK", "")
DINGTALK_SECRET  = os.getenv("DINGTALK_SECRET", "")

PROMPTS_DIR        = Path("prompts")
VERSIONS_DIR       = PROMPTS_DIR / "versions"
PENDING_DIR        = PROMPTS_DIR / "pending"
CHANGELOG          = PROMPTS_DIR / "CHANGELOG.md"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system_prompt.md"
CASES_PATH                = Path("tests/cases.json")
REGRESSION_PATH           = Path("tests/regression_set.json")
REGRESSION_REAL_PATH      = Path("tests/regression_set_real.json")
REGRESSION_CANDIDATES_PATH = Path("tests/regression_candidates.md")
FEEDBACK_PATH      = Path("feedback/pending.md")
REPORTS_DIR        = Path("reports")
ADVISOR_LOG_DIR    = REPORTS_DIR / "advisor"

DIFY_VAR_PATTERN = re.compile(r"\{\{#[^#}]+#\}\}")


def _extract_dify_vars(text: str) -> set[str]:
    return set(DIFY_VAR_PATTERN.findall(text))

# ── 解析日报劣质对话区块 ────────────────────────────────────────────────────
_SECTION_RE = re.compile(
    r"### \[会话 ([a-f0-9]+)\].*?得分 [\d.]+.*?用户 `(\w+)`\n"
    r"\*\*问题\*\*：(.+?)\n"
    r"(?:\*\*顾客\*\*：(.+?)\n)?"   # 可选：旧格式日报无此行
    r"\*\*AI回复\*\*：(.+?)\n"
    r"\*\*建议\*\*：(.+?)(?=\n###|\Z)",
    re.DOTALL,
)

def _parse_bad_sections(report_text: str) -> list[dict]:
    results = []
    for m in _SECTION_RE.finditer(report_text):
        results.append({
            "conv_id":       m.group(1).strip(),
            "user_id":       m.group(2).strip(),
            "problems":      m.group(3).strip(),
            "customer_turn": (m.group(4) or "").strip(),
            "ai_reply":      m.group(5).strip(),
            "suggestion":    m.group(6).strip(),
        })
    return results


_KEYWORD_BLACKLIST = [
    "我们", "我帮", "我来", "案例", "知识库", "数据库",
    "保证", "绝对", "百分之百", "AI", "机器人", "智能客服", "助手",
]


def _extract_keyword_violations(text: str) -> list[str]:
    return [kw for kw in _KEYWORD_BLACKLIST if kw in text]


def _section_to_case(section: dict, source_date: str = "") -> dict:
    """旧 markdown 格式回退用。无完整对话历史。"""
    problems = section.get("problems", "")
    forbidden = _extract_keyword_violations(problems + " " + section.get("ai_reply", ""))

    case_id = f"tc_{section['conv_id']}"
    split = "holdout" if random.random() < 0.2 else "optimize"
    return {
        "id":                   case_id,
        "split":                split,
        "source":                f"{source_date}_{section['conv_id']}",
        "customer_input":       section.get("customer_turn", "")[:120],
        "must_not_contain":     forbidden,
        "must_not_violate_rules": [],
        "expected_behavior":    section.get("suggestion", ""),
        "dialogue_messages":    [],
    }


def _record_to_case(record: dict, source_date: str) -> dict:
    """从结构化 JSON 日报的一条 bad_conversation 生成测试用例。"""
    rules = [v.get("rule", "") for v in record.get("violations", []) if v.get("rule")]
    keyword_violations = _extract_keyword_violations(record.get("bad_turn", ""))

    case_id = f"tc_{record['conv_id']}"
    split = "holdout" if random.random() < 0.2 else "optimize"
    return {
        "id":                    case_id,
        "split":                 split,
        "source":                f"{source_date}_{record['conv_id']}",
        "customer_input":        record.get("customer_turn", "")[:120],
        "must_not_contain":      keyword_violations,
        "must_not_violate_rules": rules,
        "expected_behavior":     record.get("suggestion", ""),
        "dialogue_messages":     record.get("messages", [])[-30:],
        "converted":             bool(record.get("converted")),
    }


def extract_cases(report_path: Path) -> list[dict]:
    """优先从同名 .json 结构化日报抽取；不存在时退回 .md regex 解析。"""
    date_str = report_path.stem
    json_path = report_path.with_suffix(".json")

    new_records: list[dict] = []
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            for rec in payload.get("bad_conversations", []):
                new_records.append(("json", rec))
        except (json.JSONDecodeError, OSError):
            new_records = []

    if not new_records and report_path.exists():
        text = report_path.read_text(encoding="utf-8")
        for sec in _parse_bad_sections(text):
            new_records.append(("md", sec))

    if CASES_PATH.exists():
        try:
            existing = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    else:
        existing = []
    existing_sources = {c["source"] for c in existing}

    new_cases = []
    for kind, rec in new_records:
        case = _record_to_case(rec, date_str) if kind == "json" else _section_to_case(rec, date_str)
        if case["source"] not in existing_sources:
            new_cases.append(case)
            existing_sources.add(case["source"])

    if new_cases:
        all_cases = existing + new_cases
        CASES_PATH.parent.mkdir(exist_ok=True)
        CASES_PATH.write_text(json.dumps(all_cases, ensure_ascii=False, indent=2), encoding="utf-8")

    return new_cases


_BEHAVIOR_JUDGE_PROMPT = """\
以下是 AI 客服的一条回复，判断它是否满足要求："{behavior}"
只回答 JSON：{{"ok": true}} 或 {{"ok": false}}

AI回复：
{reply}"""


_VIOLATION_JUDGE_PROMPT = """\
判断以下 AI 客服回复是否违反规则：「{rule}」
{history_context}
只回答 JSON：{{"violates": true}} 或 {{"violates": false}}

AI回复：
{reply}"""


def _judge_behavior(reply: str, behavior: str) -> bool:
    r = llm_advisor.chat.completions.create(
        model=ADVISOR_MODEL,
        messages=[{"role": "user", "content": _BEHAVIOR_JUDGE_PROMPT.format(
            behavior=behavior, reply=reply
        )}],
        temperature=0.0,
        max_tokens=20,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    text = (r.choices[0].message.content or "").strip()
    try:
        return bool(json.loads(text).get("ok", False))
    except (json.JSONDecodeError, KeyError, TypeError):
        return False  # 保守策略：解析失败时拒绝，避免误放行


def _judge_violates(reply: str, rule: str, history: list[dict] | None = None) -> bool:
    history_context = ""
    if history:
        recent = history[-8:]
        rendered = "\n".join(
            f"[{'顾客' if m['role']=='user' else 'AI'}] {m['content']}" for m in recent
        )
        history_context = f"\n相关对话历史：\n{rendered}\n"
    r = llm_advisor.chat.completions.create(
        model=ADVISOR_MODEL,
        messages=[{"role": "user", "content": _VIOLATION_JUDGE_PROMPT.format(
            rule=rule, history_context=history_context, reply=reply
        )}],
        temperature=0.0,
        max_tokens=20,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    text = (r.choices[0].message.content or "").strip()
    try:
        return bool(json.loads(text).get("violates", False))
    except (json.JSONDecodeError, KeyError, TypeError):
        return False  # 保守策略：解析失败不判违规，避免主管循环卡死


def _build_eval_messages(candidate_prompt: str, case: dict) -> tuple[list[dict], list[dict]]:
    """返回 (发送给候选模型的 messages, 用于违规判定的 history)。"""
    history = case.get("dialogue_messages") or []
    if history:
        # 多轮：候选 prompt 作 system + 完整历史回放
        # history 应已以最后一条 user 消息结尾；若不是，截断到最后一条 user
        last_user_idx = -1
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx == -1:
            messages = [{"role": "system", "content": candidate_prompt}]
            messages.append({"role": "user", "content": case.get("customer_input", "")})
            return messages, []
        replay = history[: last_user_idx + 1]
        messages = [{"role": "system", "content": candidate_prompt}, *replay]
        return messages, replay[:-1]  # 判违规时不重复包含本轮 user
    # 单轮回退
    messages = [
        {"role": "system", "content": candidate_prompt},
        {"role": "user",   "content": case.get("customer_input", "")},
    ]
    return messages, []


def evaluate_candidate(candidate_prompt: str, cases: list[dict]) -> dict:
    """用候选提示词回放每条用例，返回 {passed, total, passed_count, failures}。"""
    failures = []
    for case in cases:
        messages, history = _build_eval_messages(candidate_prompt, case)

        resp = llm_advisor.chat.completions.create(
            model=ADVISOR_MODEL,
            messages=messages,
            temperature=0.1,
            max_tokens=256,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        reply = (resp.choices[0].message.content or "").strip()

        # 1. 关键词级违规
        hit = [w for w in case.get("must_not_contain", []) if w in reply]
        if hit:
            failures.append({
                "id": case["id"], "reason": f"含违禁词：{hit}", "reply": reply[:200]
            })
            continue

        # 2. 语义级违规（对照具体规则）
        violated_rules = []
        for rule in case.get("must_not_violate_rules", []):
            if rule and _judge_violates(reply, rule, history):
                violated_rules.append(rule)
        if violated_rules:
            failures.append({
                "id":     case["id"],
                "reason": f"违反规则：{violated_rules}",
                "reply":  reply[:200],
            })
            continue

        # 3. 期望行为
        if case.get("expected_behavior"):
            if not _judge_behavior(reply, case["expected_behavior"]):
                failures.append({
                    "id":     case["id"],
                    "reason": f"未满足：{case['expected_behavior']}",
                    "reply":  reply[:200],
                })

    total = len(cases)
    passed_count = total - len(failures)
    return {
        "passed":       len(failures) == 0,
        "total":        total,
        "passed_count": passed_count,
        "failures":     failures,
    }


# ── 版本管理 ──────────────────────────────────────────────────────────────────
def get_next_version() -> str:
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(VERSIONS_DIR.glob("v*.md"))
    if not existing:
        return "v001"
    last = existing[-1].stem.split("_")[0]  # "v003"
    try:
        n = int(last[1:]) + 1
    except ValueError:
        raise ValueError(f"无法解析版本号：{existing[-1].name}，请检查 {VERSIONS_DIR} 目录")
    return f"v{n:03d}"


def _validate_dify_vars(candidate: str) -> None:
    """对比候选与当前 system_prompt 的 Dify 变量集合，集合不一致直接拒绝发布。"""
    if not SYSTEM_PROMPT_PATH.exists():
        return
    current = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    current_vars = _extract_dify_vars(current)
    candidate_vars = _extract_dify_vars(candidate)
    if current_vars == candidate_vars:
        return
    missing = current_vars - candidate_vars
    extra = candidate_vars - current_vars
    raise ValueError(
        f"Dify 变量校验失败，拒绝发布。"
        f"丢失：{sorted(missing) or '无'}；新增：{sorted(extra) or '无'}"
    )


def publish_version(candidate: str, version: str, change_info: dict) -> None:
    """归档当前提示词为 version_date.md，写入候选版本，追加 CHANGELOG。

    归档语义：vNNN_date.md 存的是该版本的候选内容（非发布前的旧版本）。
    rollback vNNN = 恢复到 vNNN 发布时的提示词内容。
    首次发布时额外保存 v000 备份以保留原始状态。
    发布前校验 Dify 变量必须与当前完全一致。
    """
    _validate_dify_vars(candidate)

    today = datetime.date.today().isoformat()
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # 第一次发布时，将当前 system_prompt.md 存为 v000 备份
    if version == "v001" and SYSTEM_PROMPT_PATH.exists():
        v000_files = list(VERSIONS_DIR.glob("v000_*.md"))
        if not v000_files:
            shutil.copy(SYSTEM_PROMPT_PATH, VERSIONS_DIR / f"v000_{today}.md")

    # 将候选版本存为 version_date.md
    archive = VERSIONS_DIR / f"{version}_{today}.md"
    archive.write_text(candidate, encoding="utf-8")

    # 更新 system_prompt.md
    SYSTEM_PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYSTEM_PROMPT_PATH.write_text(candidate, encoding="utf-8")

    # 追加 CHANGELOG
    entry = (
        f"\n## {version} — {today}\n"
        f"**改动模块**：{change_info.get('module', '')}\n"
        f"**原因**：{change_info.get('reason', '')}\n"
        f"**测试**：优化集 {change_info.get('opt_result', '')}，"
        f"验证集 {change_info.get('hold_result', '')}\n"
    )
    CHANGELOG.parent.mkdir(parents=True, exist_ok=True)
    with CHANGELOG.open("a", encoding="utf-8") as f:
        f.write(entry)


def stage_pending(candidate: str, version: str, change_info: dict) -> Path:
    """候选通过测试后写入 pending_vXXX_date.md，等待人工审核。
    同时保存元数据 .json 给 approve_pending 用。Dify 变量在此阶段就预校验。
    """
    _validate_dify_vars(candidate)

    today = datetime.date.today().isoformat()
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    md_path   = PENDING_DIR / f"pending_{version}_{today}.md"
    meta_path = PENDING_DIR / f"pending_{version}_{today}.json"
    md_path.write_text(candidate, encoding="utf-8")
    meta_path.write_text(
        json.dumps({"version": version, "date": today, **change_info},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return md_path


def approve_pending(version: str, push_to_production: bool = True) -> dict:
    """人工审核通过 → 写本地 system_prompt.md → 推送到 Dify 生产 → 清理 pending。
    push 失败自动回滚本地，保证本地与 Dify 始终一致。
    push_to_production=False 时只写本地不推 Dify（用于离线测试）。
    """
    md_files = sorted(PENDING_DIR.glob(f"pending_{version}_*.md"))
    if not md_files:
        raise FileNotFoundError(f"未找到待审核版本：pending_{version}_*.md")
    md_path = md_files[-1]
    meta_path = md_path.with_suffix(".json")
    if not meta_path.exists():
        raise FileNotFoundError(f"待审核版本缺少元数据：{meta_path.name}")

    candidate = md_path.read_text(encoding="utf-8")
    change_info = json.loads(meta_path.read_text(encoding="utf-8"))

    # 1. 先记录回滚锚点：发布前最近一个版本
    prev_versions = sorted(VERSIONS_DIR.glob("v*.md"))
    rollback_anchor = prev_versions[-1].stem.split("_", 1)[0] if prev_versions else None

    # 2. 写本地（含 Dify 变量校验）
    publish_version(candidate=candidate, version=version, change_info=change_info)

    # 3. 推 Dify 生产
    if push_to_production:
        try:
            from dify_push import push_prompt, DifyPushError
            push_result = push_prompt(candidate, dry_run=False)
            change_info["dify_pushed"] = True
            change_info["dify_llm_node"] = push_result.get("llm_node", "")
        except Exception as e:
            # 推送失败：回滚本地到上一版本，避免本地/生产不一致
            print(f"  ✗ Dify 推送失败：{e}")
            print(f"  → 自动回滚本地到 {rollback_anchor or 'v000'}")
            try:
                if rollback_anchor:
                    rollback_version(rollback_anchor, versions_dir=VERSIONS_DIR, target=SYSTEM_PROMPT_PATH)
                # 把 v00X 归档文件也删掉，保持 vNNN 单调
                today = datetime.date.today().isoformat()
                for v_archive in VERSIONS_DIR.glob(f"{version}_*.md"):
                    v_archive.unlink()
            except Exception as rb_err:
                print(f"  ⚠ 回滚也失败了：{rb_err} —— 需要人工介入恢复 system_prompt.md")
            raise RuntimeError(f"Dify 推送失败已回滚：{e}") from e

    md_path.unlink()
    meta_path.unlink()
    return {"version": version, "module": change_info.get("module", ""),
            "dify_pushed": change_info.get("dify_pushed", False)}


def push_current_to_dify() -> dict:
    """把当前 system_prompt.md 一次性推到 Dify（用于补推/迁移）。"""
    if not SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(SYSTEM_PROMPT_PATH)
    from dify_push import push_prompt
    prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return push_prompt(prompt, dry_run=False)


def rollback_version(
    version: str,
    versions_dir: Path = VERSIONS_DIR,
    target: Path = SYSTEM_PROMPT_PATH,
) -> None:
    """将 target 恢复为指定版本的内容（rollback vXXX = 使用 vXXX 时发布的候选提示词）。"""
    candidates = list(versions_dir.glob(f"{version}_*.md"))
    if not candidates:
        raise FileNotFoundError(f"版本 {version} 不存在于 {versions_dir}")
    src = sorted(candidates)[-1]
    shutil.copy(src, target)
    print(f"已回滚到 {src.name}")


# ── 主管智能体 ────────────────────────────────────────────────────────────────
_SUPERVISOR_PROMPT = """\
你是资深客服主管。唯一核心指标是留资率——顾客留下微信号或电话的比例。

你的工作：分析劣质对话（顾客未留资或体验差），找出 AI 违反系统提示词哪条规则导致顾客不愿留资或直接离开，然后精准加强那条规则。

【系统提示词模块结构】
身份规则 / 回复风格 / 项目与价格 / 收到个人信息 / 微信引导时机 / 知识库使用

【当前系统提示词】
{current_prompt}

【今日日报摘要（劣质对话均为未留资或体验差的对话）】
{report_text}

【人工反馈】
{feedback_text}

【优化集测试用例（共 {n_cases} 条）】
{cases_text}

{failure_section}

【分析步骤】
1. 找违规：这批对话中，AI 最频繁违反的是系统提示词哪个模块的哪条规则？
2. 找因果：这条违规如何直接导致顾客不留资（顾客反感/顾客问题未被解答/顾客失去信任）？
3. 找根因：规则为何被违反？是表述模糊、缺少优先级、缺少反例，还是条件判断不清晰？
4. 精准加强：只改那一个模块，让规则更清晰、边界更明确，使 AI 不容易误判。

【硬性约束】
- 禁止全文重写；禁止同时修改多个模块
- 禁止把顾客原话抄进提示词（不允许针对具体对话打补丁）
- 只加强已有规则的表述，不引入提示词中没有的新规则
- Dify 变量（如 {{{{#context#}}}}、{{{{#1777359257394.persona#}}}} 等）必须原样保留，一字不差

输出 JSON（只输出 JSON，不要其他文字）：
{{
  "module": "被加强的模块名称",
  "violated_rule": "被违反的具体规则原文（从当前系统提示词中摘录）",
  "conversion_impact": "这条违规如何导致顾客不留资",
  "reason": "规则为何频繁被违反，引用了哪些数据",
  "candidate_prompt": "完整的新系统提示词文本"
}}"""


def generate_candidate(
    report_text: str,
    feedback_text: str,
    current_prompt: str,
    optimize_cases: list[dict],
    failures: list[dict] | None = None,
) -> dict:
    cases_text = "\n".join(
        f"- [{c['id']}] 输入：{c['customer_input'][:80]}  期望：{c['expected_behavior']}"
        for c in optimize_cases[:20]
    )
    failure_section = ""
    if failures:
        lines = ["【上次测试失败原因，本次必须修复】"]
        for f in failures[:10]:
            lines.append(f"- {f['id']}: {f['reason']}")
        failure_section = "\n".join(lines)

    prompt = _SUPERVISOR_PROMPT.format(
        current_prompt=current_prompt,
        report_text=report_text[:3000],
        feedback_text=feedback_text[:1000] if feedback_text else "（无）",
        n_cases=len(optimize_cases),
        cases_text=cases_text,
        failure_section=failure_section,
    )
    r = llm_advisor.chat.completions.create(
        model=ADVISOR_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=4096,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    text = (r.choices[0].message.content or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "module": "未知",
            "reason": "解析失败",
            "expected_effect": "",
            "candidate_prompt": current_prompt,
        }


# ── 主循环 ────────────────────────────────────────────────────────────────────
MAX_RETRIES = 5
HOLDOUT_PASS_THRESHOLD = 0.75


def _load_cases() -> tuple[list[dict], list[dict]]:
    if CASES_PATH.exists():
        try:
            all_cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            all_cases = []
    else:
        all_cases = []
    optimize = [c for c in all_cases if c.get("split") == "optimize"]
    holdout  = [c for c in all_cases if c.get("split") == "holdout"]
    return optimize, holdout


def _read_regression_file(path: Path, universal_default: list[str]) -> tuple[list[dict], float, list[str]]:
    """读单个 regression 文件，返回 (cases, threshold, universal_blacklist)。"""
    if not path.exists():
        return [], 0.95, universal_default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], 0.95, universal_default

    universal = data.get("universal_must_not_contain", universal_default) or universal_default
    threshold = data.get("_meta", {}).get("publish_threshold", 0.95)

    cases = []
    for c in data.get("cases", []):
        per_case = c.get("must_not_contain") or []
        merged = list({*universal, *per_case})

        case = {
            "id":                    c["id"],
            "split":                 "regression",
            "source":                f"regression_{c['id']}",
            "category":              c.get("category", ""),
            "customer_input":        "",
            "must_not_contain":      merged,
            "must_not_violate_rules": c.get("must_not_violate_rules", []),
            "expected_behavior":     c.get("expected_behavior", ""),
            "dialogue_messages":     c.get("dialogue_messages", []),
        }
        for m in reversed(case["dialogue_messages"]):
            if m.get("role") == "user":
                case["customer_input"] = m["content"]
                break
        cases.append(case)
    return cases, threshold, universal


def _load_regression_cases() -> tuple[list[dict], float]:
    """合并人工维护的合成 regression_set 与真实挖掘的 regression_set_real。
    阈值取较严的（最大值）。
    """
    syn_cases, syn_thr, universal = _read_regression_file(REGRESSION_PATH, [])
    real_cases, real_thr, _ = _read_regression_file(REGRESSION_REAL_PATH, universal)
    threshold = max(syn_thr, real_thr) if (syn_cases or real_cases) else 0.95
    seen = set()
    merged = []
    for c in [*syn_cases, *real_cases]:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        merged.append(c)
    return merged, threshold


def _gather_real_violations(top_per_rule: int = 5) -> list[dict]:
    """扫 reports/<date>.json，按 violations[].rule 聚类，返回每条规则下分数最低的 Top N 候选。
    每个候选对象含完整对话、违规证据、来源元数据。"""
    if not REPORTS_DIR.exists():
        return []
    by_rule: dict[str, list[dict]] = {}
    for p in sorted(REPORTS_DIR.glob("????-??-??.json")):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        date_str = payload.get("date", p.stem)
        for record in payload.get("bad_conversations", []):
            for v in record.get("violations", []):
                rule = (v.get("rule") or "").strip()
                if not rule:
                    continue
                by_rule.setdefault(rule, []).append({
                    "rule":       rule,
                    "evidence":   v.get("evidence", ""),
                    "impact":     v.get("impact", ""),
                    "score":      record.get("score", 1.0),
                    "conv_id":    record.get("conv_id", ""),
                    "user_id":    record.get("user_id", ""),
                    "date":       date_str,
                    "messages":   record.get("messages", []),
                    "suggestion": record.get("suggestion", ""),
                    "customer_turn": record.get("customer_turn", ""),
                    "bad_turn":   record.get("bad_turn", ""),
                })
    # 排序：分数低的优先（违规越严重）
    out = []
    for rule, items in by_rule.items():
        items.sort(key=lambda x: x.get("score", 1.0))
        out.append({"rule": rule, "candidates": items[:top_per_rule], "total": len(items)})
    out.sort(key=lambda r: -r["total"])
    return out


def _read_existing_real_sources() -> set[str]:
    """已晋升的真实 case 来源标识（避免重复挖掘相同对话）。"""
    if not REGRESSION_REAL_PATH.exists():
        return set()
    try:
        data = json.loads(REGRESSION_REAL_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return {c.get("source", "") for c in data.get("cases", []) if c.get("source")}


def mine_regression_candidates(top_per_rule: int = 5) -> Path:
    """扫历史日报输出 regression_candidates.md，含 [ ] 复选框给人工勾选。"""
    grouped = _gather_real_violations(top_per_rule=top_per_rule)
    seen = _read_existing_real_sources()

    lines: list[str] = [
        f"# 回归测试集候选 — {datetime.date.today().isoformat()}",
        "",
        "> 把要晋升的候选 `[ ]` 改为 `[x]`，保存后执行：",
        "> `python advisor.py --promote-regression`",
        "",
        f"扫描 {len(grouped)} 类违规，{sum(g['total'] for g in grouped)} 条历史违规，",
        f"已挑选 Top {top_per_rule}/类作候选（按分数升序，越严重越靠前）。",
        f"已晋升过的来源 {len(seen)} 条会被跳过。",
        "",
        "---",
        "",
    ]
    cnt = 0
    for grp in grouped:
        rule = grp["rule"]
        lines.append(f"## {rule}（共 {grp['total']} 条历史命中）")
        lines.append("")
        for c in grp["candidates"]:
            source = f"{c['date']}_{c['conv_id']}"
            if source in seen:
                continue
            cnt += 1
            lines.append(f"### [ ] 候选 {cnt}: `{source}`  得分 {c['score']:.2f}  用户 `{c['user_id']}`")
            lines.append("")
            lines.append("**对话历史**：")
            lines.append("```")
            for m in c["messages"][-10:]:
                role = "顾客" if m.get("role") == "user" else "AI"
                content = (m.get("content") or "").replace("\n", " ")[:200]
                lines.append(f"[{role}] {content}")
            lines.append("```")
            lines.append("")
            lines.append(f"**违规证据**：{c['evidence']}")
            lines.append(f"**留资影响**：{c['impact']}")
            lines.append(f"**触发顾客原话**：{c['customer_turn']}")
            lines.append(f"**违规 AI 原话**：{c['bad_turn']}")
            lines.append(f"**建议回法**：{c['suggestion']}")
            lines.append("")
            lines.append("---")
            lines.append("")

    REGRESSION_CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGRESSION_CANDIDATES_PATH.write_text("\n".join(lines), encoding="utf-8")
    return REGRESSION_CANDIDATES_PATH


_PROMOTE_HEADER_RE = re.compile(
    r"### \[x\] 候选 \d+: `(?P<source>[^`]+)`\s+得分 (?P<score>[\d.]+)\s+用户 `(?P<user>[^`]*)`"
)


def promote_regression() -> tuple[int, Path]:
    """读 regression_candidates.md 中已勾选的候选，写入 regression_set_real.json。
    返回 (新增条数, 文件路径)。
    """
    if not REGRESSION_CANDIDATES_PATH.exists():
        raise FileNotFoundError(f"先跑 --mine-regression 生成 {REGRESSION_CANDIDATES_PATH}")

    text = REGRESSION_CANDIDATES_PATH.read_text(encoding="utf-8")
    grouped = _gather_real_violations(top_per_rule=999)
    by_source: dict[str, dict] = {}
    for grp in grouped:
        for c in grp["candidates"]:
            by_source[f"{c['date']}_{c['conv_id']}"] = c

    # 找 [x] 的 source
    selected_sources = []
    current_rule = None
    for line in text.split("\n"):
        if line.startswith("## "):
            current_rule = line[3:].split("（")[0].strip()
        m = _PROMOTE_HEADER_RE.match(line)
        if m:
            selected_sources.append((m.group("source"), current_rule))

    if not selected_sources:
        return 0, REGRESSION_REAL_PATH

    # 加载已有
    if REGRESSION_REAL_PATH.exists():
        existing = json.loads(REGRESSION_REAL_PATH.read_text(encoding="utf-8"))
    else:
        # 沿用合成集的 universal_must_not_contain
        syn_universal = []
        if REGRESSION_PATH.exists():
            try:
                syn_data = json.loads(REGRESSION_PATH.read_text(encoding="utf-8"))
                syn_universal = syn_data.get("universal_must_not_contain", [])
            except (json.JSONDecodeError, OSError):
                pass
        existing = {
            "_meta": {
                "purpose": "从真实生产对话挖掘的稳定回归集（人工筛过）",
                "publish_threshold": 0.95,
                "last_updated": datetime.date.today().isoformat(),
            },
            "universal_must_not_contain": syn_universal,
            "cases": [],
        }

    seen_sources = {c.get("source", "") for c in existing["cases"]}
    added = 0
    for source, rule in selected_sources:
        if source in seen_sources:
            continue
        cand = by_source.get(source)
        if not cand:
            continue
        case = {
            "id":                     f"rg_real_{source.replace('-', '').replace('_', '_')[:24]}",
            "category":               "real-mined",
            "source":                 source,
            "description":            f"挖自 {source}：{rule[:40]}",
            "dialogue_messages":      cand["messages"][-30:],
            "must_not_contain":       [],
            "must_not_violate_rules": [rule] if rule else [],
            "expected_behavior":      cand.get("suggestion", ""),
        }
        existing["cases"].append(case)
        seen_sources.add(source)
        added += 1

    existing["_meta"]["last_updated"] = datetime.date.today().isoformat()

    REGRESSION_REAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGRESSION_REAL_PATH.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return added, REGRESSION_REAL_PATH


def _record_advisor_log(date: str, payload: dict) -> Path:
    """每天追加一条 advisor 运行记录（即便没发布也要留痕）。"""
    ADVISOR_LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = ADVISOR_LOG_DIR / f"{date}.json"
    if out.exists():
        try:
            history = json.loads(out.read_text(encoding="utf-8"))
            if not isinstance(history, list):
                history = [history]
        except (json.JSONDecodeError, OSError):
            history = []
    else:
        history = []
    history.append({"timestamp": datetime.datetime.now().isoformat(timespec="seconds"), **payload})
    out.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def run_advisor(
    report_path: Path,
    extract_only: bool = False,
    rollback: str = "",
    auto_publish: bool = False,
) -> dict:
    """飞轮主循环：验证集驱动迭代，回归测试集守住稳定性底线。
    候选必须依次通过：optimize 集 → holdout 子集 → 回归测试集，全过才进入 pending。
    任一环节失败都把失败 case 喂回下一轮 generate_candidate，迭代直到全过或超出 MAX_RETRIES。
    auto_publish=True 时跳过人工审核直接发布。
    """
    date_str = datetime.date.today().isoformat()

    if rollback:
        rollback_version(rollback)
        _record_advisor_log(date_str, {"action": "rolled_back", "version": rollback})
        return {"action": "rolled_back", "version": rollback}

    new_cases = extract_cases(report_path)
    print(f"  → 新增测试用例 {len(new_cases)} 条")

    if extract_only:
        _record_advisor_log(date_str, {"action": "extracted", "new_cases": len(new_cases)})
        return {"action": "extracted", "new_cases": len(new_cases)}

    report_text    = report_path.read_text(encoding="utf-8")
    current_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    feedback_text  = FEEDBACK_PATH.read_text(encoding="utf-8") if FEEDBACK_PATH.exists() else ""
    optimize_cases, holdout_cases    = _load_cases()
    regression_cases, regression_thr = _load_regression_cases()

    if not optimize_cases and not regression_cases:
        print("  → 优化集与回归集均为空，跳过本轮")
        _record_advisor_log(date_str, {"action": "skipped", "reason": "no_cases"})
        return {"action": "skipped", "reason": "no_cases"}

    print(f"  → 验证集：optimize {len(optimize_cases)}  |  holdout {len(holdout_cases)}")
    print(f"  → 测试集：regression {len(regression_cases)}（阈值 {regression_thr:.0%}）")

    failures: list[dict] | None = None
    iter_log: list[dict] = []

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"\n  [迭代 {attempt}/{MAX_RETRIES}] 生成候选提示词...")
        result = generate_candidate(
            report_text, feedback_text, current_prompt, optimize_cases, failures
        )
        candidate = result.get("candidate_prompt", current_prompt)

        # 1. 验证集（cases.json[optimize]）— 驱动迭代
        if optimize_cases:
            print(f"  → 评估 optimize 集（{len(optimize_cases)}）...")
            opt_eval = evaluate_candidate(candidate, optimize_cases)
            print(f"     {opt_eval['passed_count']}/{opt_eval['total']}")
            if not opt_eval["passed"]:
                failures = opt_eval["failures"]
                iter_log.append({"attempt": attempt, "stage": "optimize", "failures": len(failures)})
                print(f"     ✗ optimize 失败 {len(failures)} 条，回喂下一轮")
                continue
            opt_result = f"{opt_eval['passed_count']}/{opt_eval['total']}"
        else:
            opt_result = "N/A"

        # 2. 验证子集（cases.json[holdout]）— 防过拟合
        if holdout_cases:
            print(f"  → 评估 holdout 集（{len(holdout_cases)}）...")
            hold_eval = evaluate_candidate(candidate, holdout_cases)
            hold_rate = hold_eval["passed_count"] / hold_eval["total"]
            print(f"     {hold_eval['passed_count']}/{hold_eval['total']}（{hold_rate:.0%}）")
            if hold_rate < HOLDOUT_PASS_THRESHOLD:
                failures = hold_eval["failures"]
                iter_log.append({"attempt": attempt, "stage": "holdout", "failures": len(failures)})
                print(f"     ✗ holdout {hold_rate:.0%} < {HOLDOUT_PASS_THRESHOLD:.0%}，回喂下一轮")
                continue
            hold_result = f"{hold_eval['passed_count']}/{hold_eval['total']}"
        else:
            hold_result = "N/A"

        # 3. 回归测试集 — 稳定性底线（人工维护，不得退化）
        if regression_cases:
            print(f"  → 评估 regression 集（{len(regression_cases)}）...")
            reg_eval = evaluate_candidate(candidate, regression_cases)
            reg_rate = reg_eval["passed_count"] / reg_eval["total"]
            print(f"     {reg_eval['passed_count']}/{reg_eval['total']}（{reg_rate:.0%}）")
            if reg_rate < regression_thr:
                # 关键：回归失败也回喂为新验证案例，驱动下一轮修复
                failures = reg_eval["failures"]
                iter_log.append({"attempt": attempt, "stage": "regression", "failures": len(failures)})
                print(f"     ✗ regression {reg_rate:.0%} < {regression_thr:.0%}，回归失败回喂下一轮")
                continue
            reg_result = f"{reg_eval['passed_count']}/{reg_eval['total']}"
        else:
            reg_result = "N/A"

        # 全部通过 → 入闸
        version = get_next_version()
        change_info = {
            "module":            result.get("module", ""),
            "violated_rule":     result.get("violated_rule", ""),
            "conversion_impact": result.get("conversion_impact", ""),
            "reason":            result.get("reason", ""),
            "opt_result":        opt_result,
            "hold_result":       hold_result,
            "regression_result": reg_result,
        }

        if auto_publish:
            try:
                publish_version(candidate=candidate, version=version, change_info=change_info)
            except ValueError as e:
                print(f"  ✗ 发布被拒（Dify 变量校验失败）：{e}")
                failures = [{"id": "_dify_var_check", "reason": str(e)}]
                iter_log.append({"attempt": attempt, "stage": "dify_var", "failures": 1})
                continue
            print(f"  → 自动发布 {version} 成功！模块：{result.get('module')}")
            action = "published"
        else:
            try:
                pending_path = stage_pending(candidate=candidate, version=version, change_info=change_info)
            except ValueError as e:
                print(f"  ✗ 候选被拒：{e}")
                failures = [{"id": "_dify_var_check", "reason": str(e)}]
                iter_log.append({"attempt": attempt, "stage": "dify_var", "failures": 1})
                continue
            print(f"  → 候选 {version} 已生成，等待人工审核：{pending_path}")
            action = "pending"

        if FEEDBACK_PATH.exists() and feedback_text.strip():
            FEEDBACK_PATH.write_text("", encoding="utf-8")

        run_payload = {
            "action":            action,
            "version":           version,
            "module":            result.get("module"),
            "reason":            result.get("reason"),
            "opt_result":        opt_result,
            "hold_result":       hold_result,
            "regression_result": reg_result,
            "iterations":        iter_log,
        }
        _record_advisor_log(date_str, run_payload)
        return run_payload

    # 全部重试用尽未通过：候选不发布，把当前失败留档供明天接力
    print(f"  → {MAX_RETRIES} 轮迭代未收敛，本轮不发布")
    failed_payload = {
        "action":     "failed",
        "attempts":   MAX_RETRIES,
        "failures":   failures or [],
        "iterations": iter_log,
    }
    _record_advisor_log(date_str, failed_payload)
    return failed_payload


def collect_status() -> dict:
    """聚合操作员关心的状态：当前版本、用例集大小、待审 pending、近 7 日运营摘要。"""
    today = datetime.date.today()

    # 当前 prompt
    if SYSTEM_PROMPT_PATH.exists():
        sp = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        sp_chars = len(sp)
        sp_vars = sorted(_extract_dify_vars(sp))
    else:
        sp_chars, sp_vars = 0, []

    # 已发布版本（按字典序末尾即最新）；vNNN_<date>.md 取 vNNN 部分
    versions = []
    if VERSIONS_DIR.exists():
        for p in sorted(VERSIONS_DIR.glob("v*.md")):
            versions.append(p.stem.split("_", 1)[0])
    latest_version = versions[-1] if versions else "（未发布）"

    # cases.json
    optimize_cases, holdout_cases = _load_cases()
    regression_cases, regression_thr = _load_regression_cases()

    # 待审 pending
    pending_files = []
    if PENDING_DIR.exists():
        for p in sorted(PENDING_DIR.glob("pending_v*.md")):
            try:
                mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime)
            except OSError:
                mtime = None
            pending_files.append({"path": str(p), "version": p.stem.split("_")[1], "mtime": mtime})

    # 近 7 日 monitor 留资率
    stats = []
    stats_path = REPORTS_DIR / "stats.json"
    if stats_path.exists():
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            stats = []
    cutoff = (today - datetime.timedelta(days=6)).isoformat()
    recent_stats = [s for s in stats if s.get("date", "") >= cutoff]

    # 近 7 日 advisor 行动
    recent_advisor: dict[str, list[dict]] = {}
    if ADVISOR_LOG_DIR.exists():
        for p in sorted(ADVISOR_LOG_DIR.glob("????-??-??.json")):
            date = p.stem
            if date < cutoff:
                continue
            try:
                recent_advisor[date] = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

    return {
        "today":             today.isoformat(),
        "system_prompt": {
            "current_version": latest_version,
            "char_count":      sp_chars,
            "dify_vars":       sp_vars,
        },
        "cases": {
            "optimize":   len(optimize_cases),
            "holdout":    len(holdout_cases),
            "regression": len(regression_cases),
            "regression_threshold": regression_thr,
        },
        "pending":           pending_files,
        "recent_stats":      recent_stats,
        "recent_advisor":    recent_advisor,
    }


def print_status(status: dict) -> None:
    print(f"\n{'='*60}")
    print(f"  agent-monitor 状态  |  {status['today']}")
    print(f"{'='*60}\n")

    sp = status["system_prompt"]
    print(f"【系统提示词】")
    print(f"  当前版本：{sp['current_version']}")
    print(f"  字符数：  {sp['char_count']}")
    print(f"  Dify 变量：{len(sp['dify_vars'])} 个")
    for v in sp["dify_vars"]:
        print(f"    · {v}")

    cases = status["cases"]
    print(f"\n【测试集】")
    print(f"  optimize（生产失败，驱动迭代）：{cases['optimize']} 条")
    print(f"  holdout （防过拟合）：           {cases['holdout']} 条")
    print(f"  regression（人工维护，守底线）： {cases['regression']} 条 "
          f"（阈值 {cases['regression_threshold']:.0%}）")

    pending = status["pending"]
    print(f"\n【待审核】")
    if not pending:
        print(f"  （无）")
    else:
        for p in pending:
            mtime = p["mtime"].strftime("%Y-%m-%d %H:%M") if p["mtime"] else "?"
            print(f"  {p['version']}  生成于 {mtime}")
            print(f"    {p['path']}")

    print(f"\n【近 7 日 monitor 留资率】")
    if not status["recent_stats"]:
        print(f"  （暂无数据）")
    else:
        for s in status["recent_stats"]:
            rate = s.get("rate", 0.0)
            print(f"  {s['date']}  对话 {s.get('total', 0):3d}  留资 "
                  f"{s.get('converted', 0):3d}  劣质 {s.get('bad', 0):3d}  "
                  f"留资率 {rate*100:5.1f}%")

    print(f"\n【近 7 日 advisor 行动】")
    if not status["recent_advisor"]:
        print(f"  （暂无数据）")
    else:
        for date, runs in status["recent_advisor"].items():
            for r in runs:
                action = r.get("action", "?")
                version = r.get("version", "")
                module = r.get("module", "")
                tail = []
                if version:
                    tail.append(version)
                if module:
                    tail.append(module)
                if action == "failed":
                    tail.append(f"{r.get('attempts', '?')}轮全失败")
                tail_str = "  ".join(tail)
                print(f"  {date}  {action:<18s}  {tail_str}")

    print()


def run_regression_only(prompt_path: Path = SYSTEM_PROMPT_PATH) -> dict:
    """仅对当前 system_prompt.md 跑一遍回归测试集，不修改任何文件。"""
    if not prompt_path.exists():
        raise FileNotFoundError(prompt_path)
    cases, threshold = _load_regression_cases()
    if not cases:
        return {"action": "regression_skipped", "reason": "empty"}
    prompt = prompt_path.read_text(encoding="utf-8")
    result = evaluate_candidate(prompt, cases)
    pass_rate = result["passed_count"] / result["total"] if result["total"] else 0.0
    return {
        "action":    "regression_tested",
        "total":     result["total"],
        "passed":    result["passed_count"],
        "pass_rate": pass_rate,
        "threshold": threshold,
        "ok":        pass_rate >= threshold,
        "failures":  result["failures"],
    }


# ── 钉钉通知 ──────────────────────────────────────────────────────────────────
def send_advisor_dingtalk(result: dict, date: str) -> None:
    import time, hmac, hashlib, base64, urllib.parse
    if not DINGTALK_WEBHOOK or not DINGTALK_SECRET:
        return

    action = result.get("action")
    if action == "published":
        title = f"提示词更新 {result['version']} — {date}"
        lines = [
            f"## {title}", "",
            f"**改动模块**：{result.get('module', '')}",
            f"**原因**：{result.get('reason', '')[:200]}",
            f"**测试**：优化集 {result.get('opt_result')}，验证集 {result.get('hold_result')}",
        ]
    elif action == "pending":
        title = f"候选提示词待审核 {result['version']} — {date}"
        lines = [
            f"## {title}", "",
            f"**改动模块**：{result.get('module', '')}",
            f"**原因**：{result.get('reason', '')[:200]}",
            f"**测试**：优化集 {result.get('opt_result')}，验证集 {result.get('hold_result')}",
            "",
            f"人工审核通过后执行：`python advisor.py --approve {result['version']}`",
        ]
    elif action == "approved":
        title = f"候选已审批发布 {result['version']} — {date}"
        lines = [
            f"## {title}", "",
            f"**改动模块**：{result.get('module', '')}",
            "已上线 system_prompt.md。",
        ]
    elif action == "failed":
        title = f"提示词优化未通过 — {date}"
        top = result.get("failures", [])[:3]
        lines = [
            f"## {title}", "",
            f"**{result.get('attempts')} 次重试全失败**",
            "**失败原因（前3条）：**",
        ] + [f"- {f['id']}: {f['reason']}" for f in top] + [
            "", "建议：请检查 feedback/pending.md 并手动调整",
        ]
    elif action == "rolled_back":
        title = f"提示词已回滚 {result.get('version')} — {date}"
        lines = [f"## {title}", "", f"已回滚到版本 {result.get('version')}"]
    else:
        return

    text = "\n".join(lines)
    ts       = str(round(time.time() * 1000))
    sign_src = f"{ts}\n{DINGTALK_SECRET}".encode("utf-8")
    digest   = hmac.new(DINGTALK_SECRET.encode("utf-8"), sign_src, digestmod=hashlib.sha256).digest()
    sign     = urllib.parse.quote_plus(base64.b64encode(digest))
    url      = f"{DINGTALK_WEBHOOK}&timestamp={ts}&sign={sign}"
    resp = requests.post(url, json={
        "msgtype":  "markdown",
        "markdown": {"title": title, "text": text},
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(data.get("errmsg", str(data)))


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="BeautsGO 客服主管智能体")
    parser.add_argument("--report",       default="",  help="指定日报路径，默认取最新")
    parser.add_argument("--extract-only", action="store_true", help="只提取测试用例，不优化")
    parser.add_argument("--rollback",     default="",  help="回滚到指定版本，如 v002")
    parser.add_argument("--approve",      default="",  help="人工审批 pending 候选，如 v002")
    parser.add_argument("--auto-publish", action="store_true",
                        help="跳过人工审核直接发布（默认半监督模式生成 pending）")
    parser.add_argument("--test-only",    action="store_true",
                        help="只跑回归测试集对照当前 system_prompt.md，不优化")
    parser.add_argument("--status",       action="store_true",
                        help="操作员仪表盘：当前版本/用例集/pending/近 7 日数据")
    parser.add_argument("--push-current", action="store_true",
                        help="把当前 system_prompt.md 推到 Dify 生产（不优化）")
    parser.add_argument("--mine-regression", action="store_true",
                        help="扫历史 reports 按违规聚类，输出 regression_candidates.md 待人工勾选")
    parser.add_argument("--promote-regression", action="store_true",
                        help="读 regression_candidates.md 中 [x] 的候选，写入 regression_set_real.json")
    parser.add_argument("--top-per-rule", type=int, default=5,
                        help="每条规则导出多少候选（默认 5）")
    args = parser.parse_args()

    date_str = datetime.date.today().isoformat()
    print(f"{'='*52}")
    print(f"  BeautsGO Advisor  |  {date_str}")
    print(f"{'='*52}\n")

    if args.status:
        print_status(collect_status())
        return

    if args.mine_regression:
        path = mine_regression_candidates(top_per_rule=args.top_per_rule)
        print(f"  → 候选已写入 {path}")
        print(f"  → 编辑该文件，把要晋升的 [ ] 改为 [x]，然后跑：")
        print(f"      python advisor.py --promote-regression")
        return

    if args.promote_regression:
        try:
            added, path = promote_regression()
        except FileNotFoundError as e:
            print(f"  ✗ {e}")
            return
        print(f"  → 已晋升 {added} 条到 {path}")
        return

    if args.push_current:
        try:
            r = push_current_to_dify()
            print(f"  → 已推送到 Dify 生产，LLM 节点 = {r.get('llm_node', '')}")
            _record_advisor_log(date_str, {"action": "dify_push", "via": "cli", **r})
        except Exception as e:
            print(f"  ✗ 推送失败：{e}")
            sys.exit(2)
        return

    if args.test_only:
        try:
            result = run_regression_only()
        except FileNotFoundError as e:
            print(f"  ✗ {e} 不存在")
            return
        if result["action"] == "regression_skipped":
            print("  → 回归集为空，无需测试")
            return
        rate = result["pass_rate"]
        print(f"  → 回归集：{result['passed']}/{result['total']}（{rate:.0%}），阈值 {result['threshold']:.0%}")
        print(f"  → {'通过' if result['ok'] else '未通过'}")
        if not result["ok"]:
            print("\n  失败 case：")
            for f in result["failures"][:10]:
                print(f"    - {f['id']}: {f['reason']}")
        _record_advisor_log(date_str, result)
        return
    elif args.approve:
        try:
            info = approve_pending(args.approve)
        except (FileNotFoundError, ValueError) as e:
            print(f"  ✗ 审批失败：{e}")
            return
        result = {"action": "approved", "version": info["version"], "module": info["module"]}
        _record_advisor_log(date_str, result)
    elif args.rollback:
        result = run_advisor(report_path=Path("."), rollback=args.rollback)
    else:
        if args.report:
            report_path = Path(args.report)
        else:
            reports = sorted(REPORTS_DIR.glob("????-??-??.md"))
            if not reports:
                print("错误：reports/ 下无日报文件")
                return
            report_path = reports[-1]
        print(f"  日报：{report_path}")
        result = run_advisor(
            report_path,
            extract_only=args.extract_only,
            auto_publish=args.auto_publish,
        )

    print(f"\n  结果：{result['action']}")

    try:
        send_advisor_dingtalk(result, date_str)
        print("  → 钉钉推送成功")
    except Exception as e:
        print(f"  ⚠ 钉钉推送失败：{e}")

    print(f"\n{'='*52}\n")


if __name__ == "__main__":
    main()
