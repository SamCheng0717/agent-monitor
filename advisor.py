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
CASES_PATH         = Path("tests/cases.json")
FEEDBACK_PATH      = Path("feedback/pending.md")
REPORTS_DIR        = Path("reports")

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
    except Exception:
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
    except Exception:
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


def approve_pending(version: str) -> dict:
    """人工审核通过 → 真正调用 publish_version 上线，清理 pending 文件。"""
    md_files = sorted(PENDING_DIR.glob(f"pending_{version}_*.md"))
    if not md_files:
        raise FileNotFoundError(f"未找到待审核版本：pending_{version}_*.md")
    md_path = md_files[-1]
    meta_path = md_path.with_suffix(".json")
    if not meta_path.exists():
        raise FileNotFoundError(f"待审核版本缺少元数据：{meta_path.name}")

    candidate = md_path.read_text(encoding="utf-8")
    change_info = json.loads(meta_path.read_text(encoding="utf-8"))

    publish_version(candidate=candidate, version=version, change_info=change_info)

    md_path.unlink()
    meta_path.unlink()
    return {"version": version, "module": change_info.get("module", "")}


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
    except Exception:
        return {
            "module": "未知",
            "reason": "解析失败",
            "expected_effect": "",
            "candidate_prompt": current_prompt,
        }


# ── 主循环 ────────────────────────────────────────────────────────────────────
MAX_RETRIES = 3
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


def run_advisor(
    report_path: Path,
    extract_only: bool = False,
    rollback: str = "",
    auto_publish: bool = False,
) -> dict:
    """默认半监督模式：候选通过测试后写入 pending 等待人工审核。
    auto_publish=True 时直接发布（仅用于全自动场景）。
    """
    if rollback:
        rollback_version(rollback)
        return {"action": "rolled_back", "version": rollback}

    new_cases = extract_cases(report_path)
    print(f"  → 新增测试用例 {len(new_cases)} 条")

    if extract_only:
        return {"action": "extracted", "new_cases": len(new_cases)}

    report_text    = report_path.read_text(encoding="utf-8")
    current_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    feedback_text  = FEEDBACK_PATH.read_text(encoding="utf-8") if FEEDBACK_PATH.exists() else ""
    optimize_cases, holdout_cases = _load_cases()

    if not optimize_cases:
        print("  → 优化集为空，跳过本轮优化")
        return {"action": "skipped", "reason": "no_cases"}

    failures = None
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"\n  [优化 {attempt}/{MAX_RETRIES}] 生成候选提示词...")
        result = generate_candidate(
            report_text, feedback_text, current_prompt, optimize_cases, failures
        )
        candidate = result.get("candidate_prompt", current_prompt)

        print(f"  → 评估优化集（{len(optimize_cases)} 条）...")
        opt_eval = evaluate_candidate(candidate, optimize_cases)
        print(f"     优化集：{opt_eval['passed_count']}/{opt_eval['total']}")

        if not opt_eval["passed"]:
            failures = opt_eval["failures"]
            print(f"     ✗ 优化集未通过，失败 {len(failures)} 条")
            continue

        print(f"  → 评估验证集（{len(holdout_cases)} 条）...")
        if holdout_cases:
            hold_eval = evaluate_candidate(candidate, holdout_cases)
            hold_rate = hold_eval["passed_count"] / hold_eval["total"]
            print(f"     验证集：{hold_eval['passed_count']}/{hold_eval['total']}（{hold_rate:.0%}）")
            if hold_rate < HOLDOUT_PASS_THRESHOLD:
                failures = hold_eval["failures"]
                print(f"     ✗ 验证集通过率 {hold_rate:.0%} < {HOLDOUT_PASS_THRESHOLD:.0%}，重试")
                continue
            hold_result = f"{hold_eval['passed_count']}/{hold_eval['total']}"
        else:
            hold_result = "N/A（验证集为空）"

        version = get_next_version()
        change_info = {
            "module":            result.get("module", ""),
            "violated_rule":     result.get("violated_rule", ""),
            "conversion_impact": result.get("conversion_impact", ""),
            "reason":            result.get("reason", ""),
            "opt_result":        f"{opt_eval['passed_count']}/{opt_eval['total']}",
            "hold_result":       hold_result,
        }

        if auto_publish:
            try:
                publish_version(candidate=candidate, version=version, change_info=change_info)
            except ValueError as e:
                print(f"  ✗ 发布被拒：{e}")
                failures = [{"id": "_dify_var_check", "reason": str(e)}]
                continue
            print(f"  → 自动发布 {version} 成功！模块：{result.get('module')}")
            action = "published"
        else:
            try:
                pending_path = stage_pending(candidate=candidate, version=version, change_info=change_info)
            except ValueError as e:
                print(f"  ✗ 候选被拒：{e}")
                failures = [{"id": "_dify_var_check", "reason": str(e)}]
                continue
            print(f"  → 候选 {version} 已生成，等待人工审核：{pending_path}")
            action = "pending"

        # 仅在成功生成候选时清空反馈，失败时保留供下次重试
        if FEEDBACK_PATH.exists() and feedback_text.strip():
            FEEDBACK_PATH.write_text("", encoding="utf-8")

        return {
            "action":      action,
            "version":     version,
            "module":      result.get("module"),
            "reason":      result.get("reason"),
            "opt_result":  f"{opt_eval['passed_count']}/{opt_eval['total']}",
            "hold_result": hold_result,
        }

    print(f"  → {MAX_RETRIES} 次重试全失败，本轮放弃")
    return {
        "action":   "failed",
        "attempts": MAX_RETRIES,
        "failures": failures or [],
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
    args = parser.parse_args()

    date_str = datetime.date.today().isoformat()
    print(f"{'='*52}")
    print(f"  BeautsGO Advisor  |  {date_str}")
    print(f"{'='*52}\n")

    if args.approve:
        try:
            info = approve_pending(args.approve)
        except (FileNotFoundError, ValueError) as e:
            print(f"  ✗ 审批失败：{e}")
            return
        result = {"action": "approved", "version": info["version"], "module": info["module"]}
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
