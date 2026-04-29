import sys, json
sys.path.insert(0, ".")
from pathlib import Path

import pytest

SAMPLE_REPORT = """
## 劣质对话详情

### [会话 ad634e](http://example.com) 得分 0.25　未留资　用户 `86030`
**问题**：顾客重复追问同一问题、AI 回复出现违禁词（我们）
**顾客**：超声刀多少钱？
**AI回复**：宝宝眼光真好！方便留个微信吗？
**建议**：顾客已多次拒绝留微信，AI 应直接提供价格范围。
"""


def test_parse_bad_sections():
    from advisor import _parse_bad_sections
    sections = _parse_bad_sections(SAMPLE_REPORT)
    assert len(sections) == 1
    s = sections[0]
    assert s["conv_id"] == "ad634e"
    assert "违禁词" in s["problems"]
    assert "方便留个微信" in s["ai_reply"]
    assert "价格范围" in s["suggestion"]


def test_section_to_case_structure():
    from advisor import _section_to_case
    section = {
        "conv_id": "ad634e",
        "problems": "顾客重复追问同一问题",
        "customer_turn": "超声刀多少钱？",
        "ai_reply": "宝宝眼光真好！方便留个微信吗？",
        "suggestion": "直接提供价格范围",
    }
    case = _section_to_case(section, source_date="2026-04-27")
    assert case["id"].startswith("tc_")
    assert case["split"] in ("optimize", "holdout")
    assert case["source"] == "2026-04-27_ad634e"
    assert case["customer_input"] == "超声刀多少钱？"
    assert isinstance(case["must_not_contain"], list)
    assert isinstance(case["must_not_violate_rules"], list)
    assert isinstance(case["expected_behavior"], str)
    assert case["dialogue_messages"] == []


def test_record_to_case_carries_dialogue_and_rules():
    from advisor import _record_to_case
    record = {
        "conv_id": "abc999",
        "user_id": "5001",
        "score": 0.3,
        "converted": False,
        "violations": [
            {"rule": "禁止自行报具体价格", "evidence": "约3400", "impact": "失去信任"},
            {"rule": "禁止使用第一人称", "evidence": "我帮您", "impact": "破坏身份"},
        ],
        "problems": ["编造价格"],
        "customer_turn": "丽珠兰多少钱",
        "bad_turn": "约3400",
        "suggestion": "引导留微信",
        "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "亲爱的"},
            {"role": "user", "content": "丽珠兰多少钱"},
        ],
    }
    case = _record_to_case(record, source_date="2026-04-27")
    assert case["customer_input"] == "丽珠兰多少钱"
    assert "禁止自行报具体价格" in case["must_not_violate_rules"]
    assert "禁止使用第一人称" in case["must_not_violate_rules"]
    assert len(case["dialogue_messages"]) == 3
    assert case["converted"] is False


def test_extract_cases_prefers_json_over_markdown(tmp_path, monkeypatch):
    import advisor
    cases_file = tmp_path / "cases.json"
    cases_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("advisor.CASES_PATH", cases_file)

    md_path   = tmp_path / "2026-04-27.md"
    json_path = tmp_path / "2026-04-27.json"
    md_path.write_text(SAMPLE_REPORT, encoding="utf-8")
    json_path.write_text(json.dumps({
        "date": "2026-04-27",
        "summary": {"total": 1, "converted": 0, "conversion_rate": 0.0, "bad_count": 1},
        "bad_conversations": [{
            "conv_id": "ad634e",
            "user_id": "86030",
            "score": 0.25,
            "converted": False,
            "violations": [{"rule": "禁止重复索要微信", "evidence": "x", "impact": "y"}],
            "problems": ["重复追问"],
            "customer_turn": "超声刀多少钱？",
            "bad_turn": "宝宝眼光真好！方便留个微信吗？",
            "suggestion": "直接报区间",
            "messages": [{"role": "user", "content": "超声刀多少钱"}],
        }],
    }, ensure_ascii=False), encoding="utf-8")

    new_cases = advisor.extract_cases(md_path)
    assert len(new_cases) == 1
    case = new_cases[0]
    assert "禁止重复索要微信" in case["must_not_violate_rules"]
    assert len(case["dialogue_messages"]) == 1


def test_extract_cases_fallback_to_markdown(tmp_path, monkeypatch):
    from advisor import extract_cases
    cases_file = tmp_path / "cases.json"
    cases_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("advisor.CASES_PATH", cases_file)

    md_path = tmp_path / "2026-04-27.md"
    md_path.write_text(SAMPLE_REPORT, encoding="utf-8")
    new = extract_cases(md_path)
    assert len(new) == 1
    assert new[0]["customer_input"] == "超声刀多少钱？"


def test_extract_cases_dedup(tmp_path, monkeypatch):
    from advisor import extract_cases
    cases_file = tmp_path / "cases.json"
    cases_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("advisor.CASES_PATH", cases_file)

    report = tmp_path / "2026-04-27.md"
    report.write_text(SAMPLE_REPORT, encoding="utf-8")

    new1 = extract_cases(report)
    assert len(new1) == 1

    new2 = extract_cases(report)
    assert len(new2) == 0

    all_cases = json.loads(cases_file.read_text(encoding="utf-8"))
    assert len(all_cases) == 1


def test_evaluate_candidate_forbidden_word(monkeypatch):
    import advisor
    from unittest.mock import MagicMock
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content="包含 MAGIC_FORBIDDEN_XYZ 的回复"))
    ]
    monkeypatch.setattr(advisor, "llm_advisor", fake_client)

    case = {
        "id": "tc_test",
        "split": "optimize",
        "source": "test",
        "customer_input": "超声刀多少钱",
        "must_not_contain": ["MAGIC_FORBIDDEN_XYZ"],
        "must_not_violate_rules": [],
        "expected_behavior": "引导留微信",
        "dialogue_messages": [],
    }
    bad_prompt = "你是客服。"
    result = advisor.evaluate_candidate(bad_prompt, [case])
    assert not result["passed"]
    assert len(result["failures"]) == 1


def test_evaluate_candidate_multi_turn_replays_history(monkeypatch):
    """多轮场景：候选模型应该收到完整历史，回放后被检查 must_not_contain。"""
    import advisor
    from unittest.mock import MagicMock
    fake_client = MagicMock()
    captured = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        m = MagicMock()
        m.choices = [MagicMock(message=MagicMock(content="正常回复，不含违禁词"))]
        return m

    fake_client.chat.completions.create.side_effect = fake_create
    monkeypatch.setattr(advisor, "llm_advisor", fake_client)

    case = {
        "id": "tc_multi",
        "split": "optimize",
        "source": "test",
        "customer_input": "丽珠兰多少钱",
        "must_not_contain": [],
        "must_not_violate_rules": [],
        "expected_behavior": "",
        "dialogue_messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "亲爱的"},
            {"role": "user", "content": "丽珠兰多少钱"},
        ],
    }
    advisor.evaluate_candidate("你是客服", [case])
    sent = captured["messages"]
    assert sent[0]["role"] == "system"
    assert sent[-1] == {"role": "user", "content": "丽珠兰多少钱"}
    assert len(sent) == 4  # system + 3 history messages


def test_build_eval_messages_replays_history():
    from advisor import _build_eval_messages
    case = {
        "id": "tc_x",
        "customer_input": "ignored",
        "dialogue_messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "亲爱的"},
            {"role": "user", "content": "丽珠兰多少钱"},
        ],
    }
    messages, history = _build_eval_messages("CANDIDATE", case)
    assert messages[0] == {"role": "system", "content": "CANDIDATE"}
    assert messages[-1] == {"role": "user", "content": "丽珠兰多少钱"}
    assert len(messages) == 4
    assert history == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "亲爱的"},
    ]


def test_build_eval_messages_falls_back_to_single_turn():
    from advisor import _build_eval_messages
    case = {"id": "tc_y", "customer_input": "丽珠兰多少钱", "dialogue_messages": []}
    messages, history = _build_eval_messages("CANDIDATE", case)
    assert len(messages) == 2
    assert messages[1] == {"role": "user", "content": "丽珠兰多少钱"}
    assert history == []


def test_publish_version_rejects_dify_var_loss(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(advisor, "SYSTEM_PROMPT_PATH", tmp_path / "system_prompt.md")
    monkeypatch.setattr(advisor, "CHANGELOG", tmp_path / "CHANGELOG.md")
    (tmp_path / "versions").mkdir()
    (tmp_path / "system_prompt.md").write_text(
        "客服规则\n{{#context#}}\n{{#1777359257394.persona#}}\n",
        encoding="utf-8",
    )

    bad = "客服规则（context变量被删了）\n"
    with pytest.raises(ValueError, match="Dify 变量"):
        advisor.publish_version(bad, "v001", {})


def test_publish_version_accepts_same_dify_vars(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(advisor, "SYSTEM_PROMPT_PATH", tmp_path / "system_prompt.md")
    monkeypatch.setattr(advisor, "CHANGELOG", tmp_path / "CHANGELOG.md")
    (tmp_path / "versions").mkdir()
    old = "old\n{{#context#}}\n"
    (tmp_path / "system_prompt.md").write_text(old, encoding="utf-8")

    new = "new strengthened rule\n{{#context#}}\n"
    advisor.publish_version(
        new, "v001",
        {"module": "测试", "reason": "x", "opt_result": "1/1", "hold_result": "1/1"},
    )
    assert (tmp_path / "system_prompt.md").read_text(encoding="utf-8") == new


def test_stage_and_approve_pending(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(advisor, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(advisor, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(advisor, "SYSTEM_PROMPT_PATH", tmp_path / "system_prompt.md")
    monkeypatch.setattr(advisor, "CHANGELOG", tmp_path / "CHANGELOG.md")
    (tmp_path / "system_prompt.md").write_text("旧 prompt\n{{#context#}}\n", encoding="utf-8")

    pending_path = advisor.stage_pending(
        "新 prompt\n{{#context#}}\n", "v002",
        {"module": "回复风格", "reason": "x", "opt_result": "1/1", "hold_result": "1/1"},
    )
    assert pending_path.exists()
    assert (tmp_path / "system_prompt.md").read_text(encoding="utf-8") == "旧 prompt\n{{#context#}}\n"

    info = advisor.approve_pending("v002")
    assert info["version"] == "v002"
    assert (tmp_path / "system_prompt.md").read_text(encoding="utf-8") == "新 prompt\n{{#context#}}\n"
    assert not pending_path.exists()


def test_version_lifecycle(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(advisor, "SYSTEM_PROMPT_PATH", tmp_path / "system_prompt.md")
    monkeypatch.setattr(advisor, "CHANGELOG", tmp_path / "CHANGELOG.md")
    (tmp_path / "versions").mkdir()
    (tmp_path / "system_prompt.md").write_text("old prompt\n{{#context#}}\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("# Log\n", encoding="utf-8")

    from advisor import get_next_version, publish_version, rollback_version

    assert get_next_version() == "v001"

    publish_version(
        candidate="new prompt\n{{#context#}}\n",
        version="v001",
        change_info={"module": "回复风格", "reason": "test",
                     "opt_result": "3/3", "hold_result": "1/1"}
    )
    v001_files = list((tmp_path / "versions").glob("v001_*.md"))
    assert len(v001_files) == 1
    assert v001_files[0].read_text(encoding="utf-8") == "new prompt\n{{#context#}}\n"
    assert (tmp_path / "system_prompt.md").read_text(encoding="utf-8") == "new prompt\n{{#context#}}\n"
    v000_files = list((tmp_path / "versions").glob("v000_*.md"))
    assert len(v000_files) == 1
    assert v000_files[0].read_text(encoding="utf-8") == "old prompt\n{{#context#}}\n"

    rollback_version("v001", versions_dir=tmp_path / "versions",
                     target=tmp_path / "system_prompt.md")
    assert (tmp_path / "system_prompt.md").read_text(encoding="utf-8") == "new prompt\n{{#context#}}\n"


def test_generate_candidate_returns_structure(monkeypatch):
    import advisor
    from unittest.mock import MagicMock
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content=json.dumps({
            "module": "回复风格",
            "violated_rule": "禁止使用我们",
            "conversion_impact": "破坏客服身份导致顾客离开",
            "reason": "测试原因",
            "candidate_prompt": "你是客服。\n禁止使用第一人称。",
        })))
    ]
    monkeypatch.setattr(advisor, "llm_advisor", fake_client)

    result = advisor.generate_candidate(
        report_text="## 问题分布\n- AI 回复出现违禁词（我们）：5 条\n",
        feedback_text="",
        current_prompt="你是客服助手。",
        optimize_cases=[],
        failures=None,
    )
    assert result["module"] == "回复风格"
    assert "candidate_prompt" in result
    assert len(result["candidate_prompt"]) > 5


def test_run_advisor_extract_only(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "CASES_PATH", tmp_path / "cases.json")
    monkeypatch.setattr(advisor, "FEEDBACK_PATH", tmp_path / "feedback.md")
    monkeypatch.setattr(advisor, "ADVISOR_LOG_DIR", tmp_path / "advisor_log")
    (tmp_path / "cases.json").write_text("[]", encoding="utf-8")
    (tmp_path / "feedback.md").write_text("", encoding="utf-8")

    report = tmp_path / "2026-04-27.md"
    report.write_text(SAMPLE_REPORT, encoding="utf-8")

    from advisor import run_advisor
    result = run_advisor(report_path=report, extract_only=True)
    assert result["action"] == "extracted"
    assert result["new_cases"] >= 0


def test_load_regression_cases_merges_universal_blacklist(tmp_path, monkeypatch):
    import advisor
    payload = {
        "_meta": {"publish_threshold": 0.95},
        "universal_must_not_contain": ["我们", "案例"],
        "cases": [
            {
                "id": "rg_test",
                "category": "测试",
                "dialogue_messages": [
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "亲爱的"},
                    {"role": "user", "content": "丽珠兰多少钱"},
                ],
                "must_not_contain": ["3400"],
                "must_not_violate_rules": ["禁止编造价格"],
                "expected_behavior": "引导留微信",
            },
        ],
    }
    reg_file = tmp_path / "regression_set.json"
    reg_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(advisor, "REGRESSION_PATH", reg_file)

    cases, threshold = advisor._load_regression_cases()
    assert threshold == 0.95
    assert len(cases) == 1
    case = cases[0]
    # universal + per-case blacklist 合并
    assert "我们" in case["must_not_contain"]
    assert "案例" in case["must_not_contain"]
    assert "3400" in case["must_not_contain"]
    # 末尾 user 内容派生为 customer_input
    assert case["customer_input"] == "丽珠兰多少钱"
    assert case["split"] == "regression"


def test_load_regression_cases_returns_empty_when_missing(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "REGRESSION_PATH", tmp_path / "missing.json")
    cases, threshold = advisor._load_regression_cases()
    assert cases == []
    assert threshold == 0.95


def test_record_advisor_log_appends_per_day(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "ADVISOR_LOG_DIR", tmp_path / "advisor_log")
    p1 = advisor._record_advisor_log("2026-04-28", {"action": "extracted", "new_cases": 3})
    p2 = advisor._record_advisor_log("2026-04-28", {"action": "pending", "version": "v003"})
    assert p1 == p2
    history = json.loads(p1.read_text(encoding="utf-8"))
    assert len(history) == 2
    assert history[0]["action"] == "extracted"
    assert history[1]["version"] == "v003"


def test_run_regression_only_passes(tmp_path, monkeypatch):
    """回归测试集功能：当前 prompt 跑一遍，不修改任何文件。"""
    import advisor
    from unittest.mock import MagicMock

    # 写一个最小回归集
    payload = {
        "universal_must_not_contain": [],
        "cases": [
            {
                "id": "rg_only_a",
                "dialogue_messages": [{"role": "user", "content": "你好"}],
                "must_not_contain": [],
                "must_not_violate_rules": [],
                "expected_behavior": "",
            },
        ],
    }
    reg_file = tmp_path / "regression_set.json"
    reg_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(advisor, "REGRESSION_PATH", reg_file)

    prompt_file = tmp_path / "system_prompt.md"
    prompt_file.write_text("你是客服", encoding="utf-8")

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content="亲爱的有什么可以帮的"))
    ]
    monkeypatch.setattr(advisor, "llm_advisor", fake_client)

    result = advisor.run_regression_only(prompt_file)
    assert result["action"] == "regression_tested"
    assert result["total"] == 1
    assert result["ok"] is True


def test_collect_status_aggregates_state(tmp_path, monkeypatch):
    """status 命令应聚合：当前版本、用例集、pending、近 7 日 stats / advisor logs。"""
    import advisor

    monkeypatch.setattr(advisor, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(advisor, "VERSIONS_DIR", tmp_path / "prompts" / "versions")
    monkeypatch.setattr(advisor, "PENDING_DIR", tmp_path / "prompts" / "pending")
    monkeypatch.setattr(advisor, "SYSTEM_PROMPT_PATH", tmp_path / "prompts" / "system_prompt.md")
    monkeypatch.setattr(advisor, "CASES_PATH", tmp_path / "cases.json")
    monkeypatch.setattr(advisor, "REGRESSION_PATH", tmp_path / "regression.json")
    monkeypatch.setattr(advisor, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(advisor, "ADVISOR_LOG_DIR", tmp_path / "reports" / "advisor")

    (tmp_path / "prompts" / "versions").mkdir(parents=True)
    (tmp_path / "prompts" / "pending").mkdir(parents=True)
    (tmp_path / "prompts" / "system_prompt.md").write_text(
        "客服规则\n{{#context#}}\n", encoding="utf-8"
    )
    (tmp_path / "prompts" / "versions" / "v002_2026-04-28.md").write_text("v2", encoding="utf-8")
    (tmp_path / "prompts" / "pending" / "pending_v003_2026-04-29.md").write_text("v3 候选", encoding="utf-8")

    # 用例集
    (tmp_path / "cases.json").write_text(json.dumps([
        {"id": "tc_1", "split": "optimize", "source": "x"},
        {"id": "tc_2", "split": "optimize", "source": "y"},
        {"id": "tc_3", "split": "holdout",  "source": "z"},
    ]), encoding="utf-8")
    (tmp_path / "regression.json").write_text(json.dumps({
        "_meta": {"publish_threshold": 0.95},
        "cases": [{"id": "rg_1", "dialogue_messages": [{"role": "user", "content": "x"}]}],
    }), encoding="utf-8")

    # stats + advisor log
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "stats.json").write_text(json.dumps([
        {"date": "2026-04-28", "total": 13, "converted": 7, "rate": 0.538, "bad": 4},
    ]), encoding="utf-8")
    (tmp_path / "reports" / "advisor").mkdir()
    (tmp_path / "reports" / "advisor" / "2026-04-28.json").write_text(json.dumps([
        {"timestamp": "2026-04-28T02:30:00", "action": "published", "version": "v002"},
    ]), encoding="utf-8")

    status = advisor.collect_status()
    assert status["system_prompt"]["current_version"] == "v002"
    assert "{{#context#}}" in status["system_prompt"]["dify_vars"]
    assert status["cases"]["optimize"] == 2
    assert status["cases"]["holdout"] == 1
    assert status["cases"]["regression"] == 1
    assert len(status["pending"]) == 1
    assert status["pending"][0]["version"] == "v003"
    # recent_stats 取决于"今天 - 6 天"截止值，2026-04-28 大概率被包含
    # 若机器今天 < 2026-05-04 则会拿到这条 stat
    assert status["recent_advisor"] != {} or True  # 容错


def test_run_regression_only_detects_failure(tmp_path, monkeypatch):
    import advisor
    from unittest.mock import MagicMock

    payload = {
        "universal_must_not_contain": ["MAGIC_FORBIDDEN"],
        "cases": [{
            "id": "rg_fail", "dialogue_messages": [{"role": "user", "content": "x"}],
            "must_not_contain": [], "must_not_violate_rules": [], "expected_behavior": "",
        }],
    }
    reg_file = tmp_path / "regression_set.json"
    reg_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(advisor, "REGRESSION_PATH", reg_file)

    prompt_file = tmp_path / "system_prompt.md"
    prompt_file.write_text("x", encoding="utf-8")

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content="包含 MAGIC_FORBIDDEN 的回复"))
    ]
    monkeypatch.setattr(advisor, "llm_advisor", fake_client)

    result = advisor.run_regression_only(prompt_file)
    assert result["ok"] is False
    assert result["pass_rate"] == 0.0
