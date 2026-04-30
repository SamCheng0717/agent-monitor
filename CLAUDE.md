# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## Project Context

### 项目本质

agent-monitor 是一个 **AI + 人工半监督的客服质量监控与提示词演进飞轮**。每天 cron 自动从 Dify 拉对话，让多个 LLM 找出系统提示词被违反的地方，迭代生成更强的候选 prompt，三阶段闸门评估通过后入 pending 等人工审批，审批后才上线。次日继续验证修复并发现新失败，构成数据飞轮。

### 三个 LLM 协同（不要混淆）

- **DEEPSEEK**（评分）：每日扫描对话，对照 `prompts/system_prompt.md` 找违规，输出 `violations[{rule, evidence, impact}]`
- **本地 Qwen3-14B**（留资识别）：`detect_conversion()` 判断对话是否留下微信号/手机号，唯一核心 KPI
- **本地 Qwen3.5-27B**（主管）：`generate_candidate()` 读日报 + cases + feedback，生成完整新 system_prompt 候选

### 关键文件 / 数据流

```
生产对话（Dify）
  ↓ monitor.py 拉取
reports/<date>.json     ← 结构化（advisor 读这个）
reports/<date>.md       ← 人类可读
reports/stats.json      ← 留资率历史
  ↓ advisor.py extract_cases
tests/cases.json        ← 自动累积，split=optimize/holdout，飞轮训练集
tests/regression_set.json ← 人工维护 20 条，飞轮测试集（稳定不变）
  ↓ advisor.py 三阶段闸门
prompts/pending/pending_v00X_<date>.md  ← 候选待审
  ↓ approve_pending()
prompts/system_prompt.md   ← 上线
prompts/versions/v00X_<date>.md ← 归档
prompts/CHANGELOG.md       ← 自动追加
reports/advisor/<date>.json ← 每日运行留痕
feedback/pending.md ← 人工反馈，发布成功后清空
```

### 重要语义（容易踩的坑）

1. **`prompts/versions/vNNN_<date>.md` 存的是该版本的候选内容**（非发布前的旧版本）。`rollback vNNN` = 恢复 vNNN 发布时的 prompt。首次发布时 v000 备份原始 prompt。
2. **Dify 变量必须原样保留**：`{{#context#}}` 等 `{{#xxx#}}` 占位符任何丢失/新增都会让 publish_version 抛 ValueError。修改 prompt 模板时绝不能动这些变量。
3. **测试集分两层**：`cases.json[optimize]` 是动态训练集（驱动迭代），`regression_set.json` 是稳定测试集（守底线，人工维护）。**两者用途不同，不要合并**。
4. **失败回喂机制**：三阶段（optimize / holdout / regression）任一阶段失败，failures 都会被塞进下一轮 `generate_candidate` 让主管 LLM 修复，最多迭代 5 轮。
5. **半监督默认值**：`run_advisor()` 默认 `auto_publish=False`，候选写到 `prompts/pending/`，**不会直接覆盖** `system_prompt.md`。必须 `--approve` 或 web 点按钮才上线。
6. **scoring 必须传 system_prompt**：`score_conversation(dialogue, system_prompt)` 第二参数缺了会自动从 `prompts/system_prompt.md` 读，但这个文件在 .gitignore 中，新机器需要手动放。

### CLI 速查

```bash
python monitor.py                      # 拉对话 → reports/<date>.{md,json}
python advisor.py                      # 完整飞轮一轮
python advisor.py --status             # 操作员仪表盘（CLI 版）
python advisor.py --test-only          # 仅跑回归集对照当前 prompt
python advisor.py --extract-only       # 仅从日报抽用例不优化
python advisor.py --approve v00X       # 人工审批 pending → 上线
python advisor.py --rollback v00X      # 回滚到指定版本
python advisor.py --auto-publish       # 跳过人工审核（仅全自动场景）
python web/app.py                      # 启动 web 仪表盘（默认 8080）
python -m pytest tests/ -v             # 跑 40 条单元测试
```

### Web 仪表盘端点（web/app.py）

```
GET  /                       index.html
GET  /api/status             同 advisor.collect_status()
GET  /api/today              今日 monitor 数据 + Top 违规
GET  /api/history?days=N     近 N 日 stats.json 序列
GET  /api/advisor-history    近 N 日 reports/advisor/*.json
GET  /api/versions           prompts/versions/ 列表
GET  /api/pending/<v>        候选 vs 当前 + 元数据
POST /api/approve/<v>        审批上线
POST /api/reject/<v>         驳回删除 pending
```

### 测试

`tests/test_advisor.py` + `tests/test_monitor.py`，pytest 跑 40 条全绿。
所有需要外部 LLM 的测试都用 `monkeypatch` mock 了 `advisor.llm_advisor` / `monitor.llm_ds` / `monitor.llm_local`，**不需要本地起 vLLM 也能跑测试**。

### 部署

服务器 `/www/wwwroot/agent-monitor`，cron 每天 02:00 跑 monitor、02:30 跑 advisor。Web 仪表盘 `nohup uvicorn web.app:app --host 0.0.0.0 --port 8090 &` 常驻。
内网访问 `http://192.168.103.66:8090/`。

### 公开仓库

GitHub: https://github.com/SamCheng0717/agent-monitor（public, MIT）
