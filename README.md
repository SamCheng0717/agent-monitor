# agent-monitor

> AI + 人工半监督的客服质量监控与提示词演进飞轮

每天自动从 [Dify](https://dify.ai/) 拉取生产对话，让多个 LLM 协同找出系统提示词被违反的地方，迭代生成更强的候选 prompt，经过三阶段闸门 + 人工审批后上线，持续提升客服 AI 的留资转化率。

---

## 解决什么问题

基于 Dify / LangChain 等框架搭建的客服 AI，上线后会面对两个长期痛点：

1. **质量回归无感知**：系统提示词改一改、知识库改一改，AI 行为悄悄退化没人发现
2. **改 prompt 全靠拍脑袋**：哪条规则被违反、改哪条最有效、改完会不会破坏其他能力，全凭手感

这个项目把它做成了一个**自驱动飞轮**：生产对话即标签，每天发现今日的失败、迭代修复、人工审批后上线，明天验证修复效果并发现新失败。

---

## 架构

```
┌───────────────────────────────────────────────────────────────────┐
│                       每日数据飞轮                                │
│                                                                   │
│   ┌─────────────┐     拉取昨日全量 Dify 对话                     │
│   │  monitor.py │ ──> 三 LLM 协同：                               │
│   └─────────────┘     · 留资识别（Qwen3-14B）                    │
│         │             · 质量评分 + 违规检测（DeepSeek，对照       │
│         │               system_prompt.md 找具体违规）             │
│         ↓                                                         │
│   reports/<date>.json   含完整多轮对话 / violations / 留资状态   │
│   reports/<date>.md     人工可读日报                              │
│         │                                                         │
│         ↓                                                         │
│   ┌─────────────┐    迭代直到三阶段闸门全过：                     │
│   │ advisor.py  │ ─> · optimize 集（生产失败案例，驱动迭代）      │
│   └─────────────┘    · holdout 子集（防过拟合）                  │
│         │             · regression 测试集（人工维护，守底线）     │
│         │              任一阶段失败 → failures 回喂下一轮         │
│         │              主管 LLM（Qwen3.5-27B）精准加强 prompt     │
│         ↓                                                         │
│   prompts/pending/pending_v00X_<date>.md                          │
│   钉钉通知人工审核                                                │
│         │                                                         │
│         ↓                                                         │
│   人工 diff 审核 →  python advisor.py --approve v00X              │
│         │                                                         │
│         ↓                                                         │
│   prompts/system_prompt.md 上线 → 生产 Dify 应用立即生效          │
│         │                                                         │
│         ↓                                                         │
│   次日 monitor 验证修复 / 发现新失败 ──┐                          │
│                                        │                          │
│         ↻ 循环 ───────────────────────┘                          │
└───────────────────────────────────────────────────────────────────┘
```

---

## 关键设计

### 1. AI 找违规 — 对照真实 prompt 而非硬编码规则

`monitor.py` 的评分 LLM 接收完整的 `system_prompt.md` 作为上下文，输出结构化 violations：

```json
{
  "score": 0.35,
  "violations": [
    {
      "rule": "禁止使用第一人称我/我们/我帮/我来",
      "evidence": "AI第3条回复出现「我帮您」",
      "impact": "破坏客服身份"
    }
  ]
}
```

prompt 修改后，scoring 会自动适配新规则，不需要同步代码。

### 2. 三阶段闸门 + 失败回喂

| 阶段 | 数据来源 | 作用 | 失败处理 |
|---|---|---|---|
| optimize | `cases.json` 来自生产失败 | 驱动迭代 | failures 回喂 generate_candidate |
| holdout  | `cases.json` 随机 20% | 防过拟合 | failures 回喂 |
| regression | `tests/regression_set.json` 人工维护 20 条 | 稳定性底线 | failures 回喂（关键：测试失败也驱动修复） |

任一阶段失败都把具体 case + 失败原因塞进下一轮 prompt 让主管 LLM 修复，最多迭代 5 轮。

### 3. 多轮回放评估

候选 prompt 的测试不是单轮 mock，而是把完整对话历史以 `[system + user + assistant + user...]` 形式回放，让候选模型在原场景下生成回复。这样才能验证「顾客已留微信，AI 不得再次索要」这类多轮上下文相关的规则。

### 4. 安全护栏

- **Dify 变量集合校验**：候选 prompt 的 `{{#xxx#}}` 占位符必须与当前 system_prompt 完全一致，丢失或新增任何一个直接拒绝发布
- **半监督闸门**：默认 stage_pending 写入 `prompts/pending/`，**不直接覆盖** `system_prompt.md`，必须 `--approve` 才上线
- **版本管理**：v000-vNNN 全量归档到 `prompts/versions/`，CHANGELOG.md 记录每次变更的违规规则、影响、三阶段通过率
- **一键回滚**：`python advisor.py --rollback v003`

### 5. 每日记录

`reports/advisor/<date>.json` 追加每次运行的状态：

- `extracted` — 只提取了用例没优化
- `pending` — 候选已生成等待审核
- `published` — 已发布
- `approved` — 人工审批通过
- `failed` — 迭代用尽未收敛
- `rolled_back` — 已回滚
- `regression_tested` — 单独跑了回归冒烟

每条记录含 timestamp + 各阶段通过率 + 失败 case 数。

---

## 快速开始

### 依赖

```bash
git clone https://github.com/SamCheng0717/agent-monitor.git
cd agent-monitor
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 环境变量

```bash
cp .env.example .env
# 填入 DeepSeek API Key、本地 Qwen URL、Dify App API Key + App ID、
# OceanBase/MySQL 连接信息、可选钉钉机器人
```

### 系统提示词

把当前 Dify 应用使用的系统提示词放到 `prompts/system_prompt.md`（仓库已 .gitignore 此目录，需自行创建）。

### 跑一遍

```bash
# 拉取生产对话生成日报
python monitor.py

# 操作员仪表盘：当前版本 / 用例集 / 待审 pending / 近 7 日数据
python advisor.py --status

# 先跑回归集冒烟看当前 prompt 健康度
python advisor.py --test-only

# 正式优化（生成 pending）
python advisor.py

# 人工审核 prompts/pending/pending_v001_*.md 后
python advisor.py --approve v001

# 出问题回滚
python advisor.py --rollback v001
```

### Web 仪表盘（飞轮可视化）

```bash
# 启动（默认 0.0.0.0:8080，内网随便访问）
python web/app.py
# 或
uvicorn web.app:app --host 0.0.0.0 --port 8080
```

浏览器打开 `http://服务器IP:8080`：

- 中央 SVG 飞轮图，今日活跃节点高亮，弧线粒子流动
- 4 张 KPI 卡（留资率 / 回归通过率 / 已发布版本 / 待审候选）
- 近 30 天留资率折线图 + 劣质对话柱状图
- 今日 Top 违规排行
- 待审候选一键 diff + 批准/驳回（替代 `--approve` 命令行）
- 版本时间线 + 近 7 日 advisor 行动表
- 每 60 秒自动刷新

### 测试

```bash
pip install pytest
python -m pytest tests/ -v
```

39 条测试覆盖：Dify API 客户端、留资检测、质量评分、统计持久化、日报生成、周报生成、用例提取（JSON + Markdown 双路径）、多轮回放、Dify 变量校验、pending/approve 流程、回归集加载等。

---

## 部署

服务器 cron：

```cron
# 每天 02:00 跑监控
0 2 * * * cd /www/wwwroot/agent-monitor && venv/bin/python monitor.py >> logs/monitor.log 2>&1

# 每天 02:30 跑 advisor 生成 pending
30 2 * * * cd /www/wwwroot/agent-monitor && venv/bin/python advisor.py >> logs/advisor.log 2>&1
```

人工早晨上班看钉钉通知，diff 一下 pending 文件，`--approve` 即可上线。

---

## 目录结构

```
agent-monitor/
├── monitor.py               # 拉取 Dify 对话 + 三 LLM 评分 + 生成日报
├── advisor.py               # 主管 Agent + 三阶段闸门 + 版本管理 + pending/approve
├── tests/
│   ├── regression_set.json  # 人工维护的稳定回归测试集（20 条）
│   ├── cases.json           # 自动累积的生产失败用例（飞轮训练集）
│   ├── test_monitor.py
│   └── test_advisor.py
├── feedback/
│   └── pending.md           # 人工反馈，advisor 下次运行时读取后清空
├── prompts/                 # .gitignore（运行时生成）
│   ├── system_prompt.md     # 生产 prompt（被 Dify 引用）
│   ├── versions/            # vNNN_<date>.md 历史归档
│   ├── pending/             # 候选 prompt 待审核
│   └── CHANGELOG.md         # 每次发布留痕
├── reports/                 # .gitignore（运行时生成）
│   ├── <date>.md            # 每日人类可读日报
│   ├── <date>.json          # 每日结构化数据（advisor 读这个）
│   ├── stats.json           # 留资率历史
│   └── advisor/<date>.json  # advisor 每日运行记录
└── requirements.txt
```

---

## 技术栈

| 层 | 选型 |
|---|---|
| 主管 / 评估 LLM | Qwen3.5-27B-FP8（本地 vLLM） |
| 评分 LLM | DeepSeek-Chat（API） |
| 留资检测 LLM | Qwen3-14B-AWQ（本地 vLLM） |
| 对话采集 | Dify App API |
| 群成员名单 | OceanBase / MySQL |
| 通知 | 钉钉自定义机器人（HMAC-SHA256 签名） |
| 调度 | cron |
| 测试 | pytest（39 条全绿） |

---

## 适用场景

如果你也在维护一个基于 Dify / LangChain / 自研框架的对话型 AI 应用，并且有：

- ✅ 一个明确的核心指标（留资率 / 转化率 / NPS / 解决率）
- ✅ 一份每天会被违反的系统提示词
- ✅ 来自生产的真实对话日志可读取
- ✅ 一个愿意每天花 5 分钟 review prompt diff 的人

那这个仓库可以直接 fork 改用。换掉 `monitor.py` 里 Dify API 部分换成你的对话源即可。

---

## License

MIT
