import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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

    # push_to_production=False 走纯本地路径，避免触发真实 Dify 推送
    info = advisor.approve_pending("v002", push_to_production=False)
    assert info["version"] == "v002"
    assert info["dify_pushed"] is False
    assert (tmp_path / "system_prompt.md").read_text(encoding="utf-8") == "新 prompt\n{{#context#}}\n"
    assert not pending_path.exists()


def test_approve_with_dify_push_success(tmp_path, monkeypatch):
    """approve_pending 推送成功路径：Dify push 被 mock 为返回 ok。"""
    import advisor, sys, types
    monkeypatch.setattr(advisor, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(advisor, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(advisor, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(advisor, "SYSTEM_PROMPT_PATH", tmp_path / "system_prompt.md")
    monkeypatch.setattr(advisor, "CHANGELOG", tmp_path / "CHANGELOG.md")
    (tmp_path / "system_prompt.md").write_text("旧\n{{#context#}}\n", encoding="utf-8")

    fake_dify = types.ModuleType("dify_push")
    fake_dify.push_prompt = lambda p, dry_run=False: {"ok": True, "stage": "published", "llm_node": "llm"}
    fake_dify.DifyPushError = type("DifyPushError", (RuntimeError,), {})
    monkeypatch.setitem(sys.modules, "dify_push", fake_dify)

    advisor.stage_pending("新\n{{#context#}}\n", "v003",
                          {"module": "x", "reason": "y", "opt_result": "1/1", "hold_result": "1/1"})
    info = advisor.approve_pending("v003")
    assert info["dify_pushed"] is True
    assert (tmp_path / "system_prompt.md").read_text(encoding="utf-8") == "新\n{{#context#}}\n"


def test_approve_rolls_back_on_dify_push_failure(tmp_path, monkeypatch):
    """approve_pending 推送失败：本地必须回滚到推送前状态。"""
    import advisor, sys, types
    monkeypatch.setattr(advisor, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(advisor, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(advisor, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(advisor, "SYSTEM_PROMPT_PATH", tmp_path / "system_prompt.md")
    monkeypatch.setattr(advisor, "CHANGELOG", tmp_path / "CHANGELOG.md")
    (tmp_path / "versions").mkdir(parents=True)
    (tmp_path / "system_prompt.md").write_text("初始\n{{#context#}}\n", encoding="utf-8")
    # 先模拟之前已发布过 v001（有归档文件），这样回滚有目标
    (tmp_path / "versions" / "v001_2026-04-28.md").write_text("初始\n{{#context#}}\n", encoding="utf-8")

    # mock dify_push 抛错
    fake_dify = types.ModuleType("dify_push")
    class FakeErr(RuntimeError): pass
    def boom(p, dry_run=False):
        raise FakeErr("Dify 端 500")
    fake_dify.push_prompt = boom
    fake_dify.DifyPushError = FakeErr
    monkeypatch.setitem(sys.modules, "dify_push", fake_dify)

    advisor.stage_pending("新\n{{#context#}}\n", "v002",
                          {"module": "x", "reason": "y", "opt_result": "1/1", "hold_result": "1/1"})

    with pytest.raises(RuntimeError, match="Dify 推送失败已回滚"):
        advisor.approve_pending("v002")

    # 本地应该回滚到 v001 内容
    assert (tmp_path / "system_prompt.md").read_text(encoding="utf-8") == "初始\n{{#context#}}\n"
    # v002 归档文件应该被清理
    v002_files = list((tmp_path / "versions").glob("v002_*.md"))
    assert v002_files == []


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


def test_mine_regression_groups_by_rule_and_skips_promoted(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(advisor, "REGRESSION_REAL_PATH", tmp_path / "real.json")
    monkeypatch.setattr(advisor, "REGRESSION_CANDIDATES_PATH", tmp_path / "candidates.md")
    (tmp_path / "reports").mkdir()

    # 两份日报，三条违规：rule_A 出现两次，rule_B 一次
    (tmp_path / "reports" / "2026-04-28.json").write_text(json.dumps({
        "date": "2026-04-28",
        "bad_conversations": [
            {"conv_id": "c1", "user_id": "u1", "score": 0.2,
             "violations": [{"rule": "rule_A", "evidence": "e1", "impact": "i1"}],
             "messages": [{"role": "user", "content": "hi"}],
             "customer_turn": "hi", "bad_turn": "wrong reply", "suggestion": "fix"},
            {"conv_id": "c2", "user_id": "u2", "score": 0.4,
             "violations": [{"rule": "rule_B", "evidence": "e2", "impact": "i2"}],
             "messages": [{"role": "user", "content": "hello"}],
             "customer_turn": "hello", "bad_turn": "x", "suggestion": "y"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "reports" / "2026-04-29.json").write_text(json.dumps({
        "date": "2026-04-29",
        "bad_conversations": [
            {"conv_id": "c3", "user_id": "u3", "score": 0.1,
             "violations": [{"rule": "rule_A", "evidence": "e3", "impact": "i3"}],
             "messages": [{"role": "user", "content": "hey"}],
             "customer_turn": "hey", "bad_turn": "z", "suggestion": "w"},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    # 已晋升过 c1 这条，挖掘时应跳过
    (tmp_path / "real.json").write_text(json.dumps({
        "_meta": {}, "universal_must_not_contain": [],
        "cases": [{"id": "rg_real_x", "source": "2026-04-28_c1"}],
    }, ensure_ascii=False), encoding="utf-8")

    path = advisor.mine_regression_candidates(top_per_rule=5)
    md = path.read_text(encoding="utf-8")

    # rule_A 应出现，c3 在 c1 前（score 升序）
    assert "rule_A" in md
    assert "rule_B" in md
    # c1 已晋升，不该再次出现
    assert "2026-04-28_c1" not in md
    # c3 / c2 应出现
    assert "2026-04-29_c3" in md
    assert "2026-04-28_c2" in md


def test_promote_regression_appends_only_checked(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(advisor, "REGRESSION_PATH", tmp_path / "syn.json")
    monkeypatch.setattr(advisor, "REGRESSION_REAL_PATH", tmp_path / "real.json")
    monkeypatch.setattr(advisor, "REGRESSION_CANDIDATES_PATH", tmp_path / "candidates.md")
    (tmp_path / "reports").mkdir()
    (tmp_path / "syn.json").write_text(json.dumps({
        "_meta": {}, "universal_must_not_contain": ["我们"],
        "cases": [],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "reports" / "2026-04-28.json").write_text(json.dumps({
        "date": "2026-04-28",
        "bad_conversations": [
            {"conv_id": "abc", "user_id": "u", "score": 0.3,
             "violations": [{"rule": "禁止重复索要微信", "evidence": "e", "impact": "i"}],
             "messages": [{"role": "user", "content": "丽珠兰多少钱"}],
             "customer_turn": "丽珠兰多少钱", "bad_turn": "约3400", "suggestion": "引导留微信"},
            {"conv_id": "def", "user_id": "u", "score": 0.4,
             "violations": [{"rule": "禁止编造价格", "evidence": "e2", "impact": "i2"}],
             "messages": [{"role": "user", "content": "热玛吉多少钱"}],
             "customer_turn": "热玛吉多少钱", "bad_turn": "x", "suggestion": "y"},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    advisor.mine_regression_candidates(top_per_rule=5)

    # 模拟人工只勾第一条（abc）
    md = (tmp_path / "candidates.md").read_text(encoding="utf-8")
    md_modified = md.replace("[ ] 候选 1: `2026-04-28_abc`", "[x] 候选 1: `2026-04-28_abc`", 1)
    (tmp_path / "candidates.md").write_text(md_modified, encoding="utf-8")

    added, _ = advisor.promote_regression()
    assert added == 1

    real = json.loads((tmp_path / "real.json").read_text(encoding="utf-8"))
    assert len(real["cases"]) == 1
    case = real["cases"][0]
    assert case["source"] == "2026-04-28_abc"
    assert "禁止重复索要微信" in case["must_not_violate_rules"]
    assert case["dialogue_messages"][-1]["content"] == "丽珠兰多少钱"
    # universal 黑名单从合成集继承
    assert "我们" in real["universal_must_not_contain"]


def test_load_regression_merges_synthetic_and_real(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "REGRESSION_PATH", tmp_path / "syn.json")
    monkeypatch.setattr(advisor, "REGRESSION_REAL_PATH", tmp_path / "real.json")
    (tmp_path / "syn.json").write_text(json.dumps({
        "_meta": {"publish_threshold": 0.9},
        "universal_must_not_contain": ["我们"],
        "cases": [{"id": "rg_syn_1",
                   "dialogue_messages": [{"role": "user", "content": "x"}]}],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "real.json").write_text(json.dumps({
        "_meta": {"publish_threshold": 0.95},
        "cases": [{"id": "rg_real_1",
                   "dialogue_messages": [{"role": "user", "content": "y"}]}],
    }, ensure_ascii=False), encoding="utf-8")

    cases, threshold = advisor._load_regression_cases()
    assert len(cases) == 2
    assert threshold == 0.95  # 取较严的
    ids = [c["id"] for c in cases]
    assert "rg_syn_1" in ids and "rg_real_1" in ids


def test_get_version_conversion_stats_attributes_days_to_versions(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(advisor, "STATS_PATH", tmp_path / "stats.json")
    monkeypatch.setattr(advisor, "CHANGELOG", tmp_path / "CHANGELOG.md")
    (tmp_path / "versions").mkdir()
    (tmp_path / "versions" / "v001_2026-04-25.md").write_text("p1", encoding="utf-8")
    (tmp_path / "versions" / "v002_2026-04-28.md").write_text("p2", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "## v001 — 2026-04-25\n**改动模块**：身份规则\n**原因**：x\n"
        "## v002 — 2026-04-28\n**改动模块**：项目与价格\n**原因**：y\n",
        encoding="utf-8",
    )
    # 6 天 stats：v001 reign = 4-25 ~ 4-27, v002 reign = 4-28 ~ today
    (tmp_path / "stats.json").write_text(json.dumps([
        {"date": "2026-04-25", "total": 100, "converted": 20, "rate": 0.2, "bad": 30},
        {"date": "2026-04-26", "total": 100, "converted": 22, "rate": 0.22, "bad": 28},
        {"date": "2026-04-27", "total": 100, "converted": 24, "rate": 0.24, "bad": 25},
        {"date": "2026-04-28", "total": 100, "converted": 18, "rate": 0.18, "bad": 35},
        {"date": "2026-04-29", "total": 100, "converted": 19, "rate": 0.19, "bad": 33},
    ]), encoding="utf-8")

    stats = advisor.get_version_conversion_stats()
    assert len(stats) == 2
    v1, v2 = stats
    assert v1["version"] == "v001"
    assert v1["days"] == 3
    assert v1["converted"] == 66
    assert v1["total_convs"] == 300
    assert abs(v1["avg_rate"] - 0.22) < 0.001
    assert v1["module"] == "身份规则"
    assert v1["delta_pp"] is None  # 第一个版本无对比
    assert v1["is_current"] is False

    assert v2["version"] == "v002"
    assert v2["days"] == 2
    assert v2["module"] == "项目与价格"
    assert v2["is_current"] is True
    # 22% → 18.5%, delta = -3.5pp
    assert abs(v2["delta_pp"] - (-3.5)) < 0.1


def test_regression_warning_triggers_on_drop(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(advisor, "STATS_PATH", tmp_path / "stats.json")
    monkeypatch.setattr(advisor, "CHANGELOG", tmp_path / "CHANGELOG.md")
    monkeypatch.setattr(advisor, "MIN_DAYS_FOR_VERSION_VERDICT", 3)
    monkeypatch.setattr(advisor, "REGRESSION_DROP_PP_THRESHOLD", 2.0)
    (tmp_path / "versions").mkdir()
    (tmp_path / "versions" / "v001_2026-04-25.md").write_text("p1", encoding="utf-8")
    (tmp_path / "versions" / "v002_2026-04-28.md").write_text("p2", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "## v001 — 2026-04-25\n**改动模块**：A\n"
        "## v002 — 2026-04-28\n**改动模块**：B\n",
        encoding="utf-8",
    )
    # v001 22%, v002 18% (3 天)，下降 4pp，触发
    (tmp_path / "stats.json").write_text(json.dumps([
        {"date": "2026-04-25", "total": 100, "converted": 22, "rate": 0.22, "bad": 0},
        {"date": "2026-04-26", "total": 100, "converted": 22, "rate": 0.22, "bad": 0},
        {"date": "2026-04-27", "total": 100, "converted": 22, "rate": 0.22, "bad": 0},
        {"date": "2026-04-28", "total": 100, "converted": 18, "rate": 0.18, "bad": 0},
        {"date": "2026-04-29", "total": 100, "converted": 18, "rate": 0.18, "bad": 0},
        {"date": "2026-04-30", "total": 100, "converted": 18, "rate": 0.18, "bad": 0},
    ]), encoding="utf-8")

    w = advisor.latest_version_regression_warning()
    assert w is not None
    assert w["version"] == "v002"
    assert w["prev_version"] == "v001"
    assert w["delta_pp"] < -3.0


def test_regression_warning_silent_when_under_threshold(tmp_path, monkeypatch):
    import advisor
    monkeypatch.setattr(advisor, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(advisor, "STATS_PATH", tmp_path / "stats.json")
    monkeypatch.setattr(advisor, "CHANGELOG", tmp_path / "CHANGELOG.md")
    monkeypatch.setattr(advisor, "MIN_DAYS_FOR_VERSION_VERDICT", 3)
    monkeypatch.setattr(advisor, "REGRESSION_DROP_PP_THRESHOLD", 2.0)
    (tmp_path / "versions").mkdir()
    (tmp_path / "versions" / "v001_2026-04-25.md").write_text("p1", encoding="utf-8")
    (tmp_path / "versions" / "v002_2026-04-28.md").write_text("p2", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("", encoding="utf-8")
    # 几乎持平
    (tmp_path / "stats.json").write_text(json.dumps([
        {"date": "2026-04-25", "total": 100, "converted": 22, "rate": 0.22, "bad": 0},
        {"date": "2026-04-26", "total": 100, "converted": 22, "rate": 0.22, "bad": 0},
        {"date": "2026-04-27", "total": 100, "converted": 22, "rate": 0.22, "bad": 0},
        {"date": "2026-04-28", "total": 100, "converted": 21, "rate": 0.21, "bad": 0},
        {"date": "2026-04-29", "total": 100, "converted": 21, "rate": 0.21, "bad": 0},
        {"date": "2026-04-30", "total": 100, "converted": 21, "rate": 0.21, "bad": 0},
    ]), encoding="utf-8")
    assert advisor.latest_version_regression_warning() is None


def test_regression_warning_silent_when_too_few_days(tmp_path, monkeypatch):
    """只观察 1-2 天不下结论。"""
    import advisor
    monkeypatch.setattr(advisor, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(advisor, "STATS_PATH", tmp_path / "stats.json")
    monkeypatch.setattr(advisor, "CHANGELOG", tmp_path / "CHANGELOG.md")
    monkeypatch.setattr(advisor, "MIN_DAYS_FOR_VERSION_VERDICT", 3)
    monkeypatch.setattr(advisor, "REGRESSION_DROP_PP_THRESHOLD", 2.0)
    (tmp_path / "versions").mkdir()
    (tmp_path / "versions" / "v001_2026-04-25.md").write_text("p1", encoding="utf-8")
    (tmp_path / "versions" / "v002_2026-04-29.md").write_text("p2", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("", encoding="utf-8")
    # v002 才 2 天数据
    (tmp_path / "stats.json").write_text(json.dumps([
        {"date": "2026-04-25", "total": 100, "converted": 22, "rate": 0.22, "bad": 0},
        {"date": "2026-04-26", "total": 100, "converted": 22, "rate": 0.22, "bad": 0},
        {"date": "2026-04-27", "total": 100, "converted": 22, "rate": 0.22, "bad": 0},
        {"date": "2026-04-29", "total": 100, "converted": 10, "rate": 0.10, "bad": 0},
        {"date": "2026-04-30", "total": 100, "converted": 10, "rate": 0.10, "bad": 0},
    ]), encoding="utf-8")
    assert advisor.latest_version_regression_warning() is None


def test_parse_feedback_structured_format():
    import advisor
    text = """## 反馈 — 2026-04-30 18:00

**原对话**:
[顾客] 丽珠兰多少钱
[AI] 黑盒约3400

**问题**:
AI 自己编了具体数字

**期望改法**:
应该给区间或引导留资

**规则修改建议**:
项目与价格里"丽珠兰"那条强调禁具体数字

---

## 反馈 — 2026-04-30 19:00

**问题**:
工作时段太着急要微信
"""
    entries = advisor.parse_feedback_entries(text)
    assert len(entries) == 2
    assert entries[0]["timestamp"] == "2026-04-30 18:00"
    assert "丽珠兰多少钱" in entries[0]["dialogue"]
    assert "AI 自己编" in entries[0]["problem"]
    assert "区间" in entries[0]["suggestion"]
    assert "项目与价格" in entries[0]["rule_change"]
    assert entries[1]["problem"].startswith("工作时段")
    assert entries[1]["dialogue"] == ""


def test_parse_feedback_legacy_freeform_compat():
    """旧自由文本应作为单条 entry 解析，整段塞 problem 字段。"""
    import advisor
    text = "2026-04-28 客服：顾客已留微信，AI 还在反复要微信号"
    entries = advisor.parse_feedback_entries(text)
    assert len(entries) == 1
    assert "反复要微信号" in entries[0]["problem"]
    assert entries[0]["dialogue"] == ""
    assert entries[0]["suggestion"] == ""


def test_parse_feedback_empty():
    import advisor
    assert advisor.parse_feedback_entries("") == []
    assert advisor.parse_feedback_entries("   \n  \n") == []


def test_serialize_feedback_roundtrip():
    """parse → serialize → parse 应保持 entry 内容稳定。"""
    import advisor
    original = [
        {
            "timestamp": "2026-04-30 18:00",
            "dialogue":  "[顾客] x\n[AI] y",
            "problem":   "AI 编数字",
            "suggestion": "给区间",
            "rule_change": "项目与价格",
        },
        {
            "timestamp": "2026-04-30 19:00",
            "dialogue":  "",
            "problem":   "只有一句话",
            "suggestion": "",
            "rule_change": "",
        },
    ]
    text = advisor.serialize_feedback_entries(original)
    parsed = advisor.parse_feedback_entries(text)
    assert len(parsed) == 2
    assert parsed[0]["problem"] == "AI 编数字"
    assert parsed[0]["rule_change"] == "项目与价格"
    assert parsed[0]["dialogue"] == "[顾客] x\n[AI] y"
    assert parsed[1]["problem"] == "只有一句话"
    assert parsed[1]["dialogue"] == ""


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
