# agent-monitor 代码审查标准与流程

> 版本：v1.0 | 最后更新：2026-04-30
>
> 目标：建立可执行、可度量的代码审查制度，持续提升代码质量，降低技术债务积累速度。

---

## 目录

1. [设计理念](#1-设计理念)
2. [代码审查流程](#2-代码审查流程)
3. [审查角色与职责](#3-审查角色与职责)
4. [通用编码标准](#4-通用编码标准)
5. [Python 专项标准](#5-python-专项标准)
6. [LLM / AI 应用专项标准](#6-llm--ai-应用专项标准)
7. [审查 Checklist](#7-审查-checklist)
8. [自动化辅助](#8-自动化辅助)
9. [常见违规示例](#9-常见违规示例)
10. [附录](#10-附录)

---

## 1. 设计理念

### 1.1 审查原则

| 原则 | 说明 |
|------|------|
| **尽早审查** | 代码在分支上即审查，不要等到合并前集中审 |
| **审查代码而非作者** | 针对代码本身提意见，保持专业和尊重 |
| **解释「为什么」** | 反馈包含理由（「这里可能内存泄漏，因为 X」），而非命令（「改成 Y」） |
| **小提交** | 每个 PR 改动不超过 400 行，方便逐行审查 |
| **审查者也有责任** | 批准即背书，审查者需对合入质量和安全问题负责 |
| **一致性优先于偏好** | 如果现有代码不符合标准但项目已大量使用，优先保持一致而非强行整改 |

### 1.2 分级标准

```
P0 - 必须修复（阻止合并）
  安全漏洞、数据丢失、逻辑错误、破坏现有功能

P1 - 应该修复（要求作者回应）
  代码异味、缺少边界处理、性能隐患、可读性差

P2 - 建议改进（不阻塞合并）
  风格偏好、可抽取函数、文档增强
```

---

## 2. 代码审查流程

### 2.1 流程图

```
  [开发者完成编码]
        │
        ▼
  ┌─────────────────────┐
  │  Stage 0: 自检      │  开发者对照 Checklist 逐项检查
  │  (Self-Review)      │  运行 lint + 全量测试
  └─────────┬───────────┘
            │
            ▼
  ┌─────────────────────┐
  │  Stage 1: 自动化    │  CI 自动运行：
  │  (Automated)        │  · ruff / flake8 代码风格检查
  │                     │  · mypy / pyright 类型检查
  │                     │  · pytest 单元测试全量通过
  │                     │  · safety 依赖安全扫描
  └─────────┬───────────┘
            │  ✗ 失败 → 回开发者修复
            ▼  ✓ 通过
  ┌─────────────────────┐
  │  Stage 2: 同行审查  │  至少 1 名 reviewer
  │  (Peer Review)      │  · 架构与设计
  │                     │  · 逻辑正确性
  │                     │  · 测试覆盖
  │                     │  · 代码风格
  └─────────┬───────────┘
            │  驳回 → 修改后重审
            ▼  ✓ 批准
  ┌─────────────────────┐
  │  Stage 3: 安全审查  │  处理敏感数据变更时启用
  │  (Security Review)  │  · API Key / Token 泄露
  │                     │  · SQL 注入
  │                     │  · LLM Prompt 注入
  │                     │  · 权限控制
  └─────────┬───────────┘
            │  可选阶段
            ▼
  ┌─────────────────────┐
  │  Stage 4: 合并      │  Squash merge → 删除分支
  │  (Merge)            │  打 Tag（可选）
  └─────────────────────┘
```

### 2.2 PR 模板

每个 Pull Request 必须包含以下信息：

```markdown
## 描述
[一句话说明改动目的]

## 关联 Issue
Closes #XXX

## 改动清单
- [模块] 具体改动 1
- [模块] 具体改动 2

## 测试验证
- [ ] 单元测试通过
- [ ] 新增测试覆盖
- [ ] 手工测试（请描述）

## 回归影响
[说明本次改动可能影响哪些现有功能]

## 审查 Checklist
- [ ] 对照项目 Code Review Checklist 逐项检查
- [ ] 无硬编码敏感信息
- [ ] 无调试代码残留（print/breakpoint/todo）
- [ ] 已清理未使用的 import
```

---

## 3. 审查角色与职责

### 3.1 开发者（Author）

- 提交前完成**自检**（运行 lint、测试、对照 Checklist）
- PR 描述清晰完整，包含**改动动机**和**验证方式**
- Reviewer 提出 P0/P1 问题后，及时**回应或修复**
- 不要在 reviewer 批准后未经重新审查就追加新改动
- 保持 PR 精炼，**单一职责**

### 3.2 审查者（Reviewer）

- 48 小时内完成首轮审查
- 对每条评论标注严重级别（P0 / P1 / P2）
- 验证改动是否**解决了问题**且**不引入新问题**
- 确认**测试覆盖合理**
- 仅在确实满意的条件下**批准**

### 3.3 代码维护者（Maintainer）

- 维护项目编码标准和审查 Checklist
- 对争议性审查意见做最终仲裁
- 定期审查已合入代码的**质量趋势**
- 主持月度代码质量回顾会

---

## 4. 通用编码标准

### 4.1 命名规范

| 类别 | 规范 | 示例 |
|------|------|------|
| 包/模块 | 全小写、下划线分隔 | `monitor.py`, `score_utils` |
| 类名 | PascalCase | `DifyClient`, `ReportGenerator` |
| 函数/方法 | snake_case | `fetch_conversations()`, `score_conversation()` |
| 变量 | snake_case | `system_prompt`, `bad_count` |
| 常量 | UPPER_SNAKE_CASE | `MAX_RETRIES`, `DS_MODEL` |
| 私有函数/变量 | 前缀 `_` | `_load_cases()`, `_db_conn` |
| 测试函数 | `test_<模块>_<场景>` | `test_score_fallback_on_bad_json` |

### 4.2 文件组织

```
模块职责单一原则：
- 一个文件不超过 500 行（超过则考虑拆分）
- 一个函数不超过 50 行（超过则考虑拆分）
- 一个类不超过 200 行（超过则考虑拆分）

文件结构规范（从上到下）：
1. License / 模块说明 docstring
2. import 块（标准库 → 第三方 → 本地模块，每组空行分隔）
3. 常量定义
4. 工具函数
5. 核心逻辑（类/函数）
6. `if __name__ == "__main__":` 入口
```

### 4.3 Import 规范

```python
# ✓ 正确
import sys
import json
from pathlib import Path

import requests
from openai import OpenAI
from dotenv import load_dotenv

from . import utils
from .models import Case

# ✗ 不要
from pathlib import Path as P  # 无意义的别名
import *                     # 通配导入
```

### 4.4 注释与文档

- **公共函数/类必须有 docstring**，说明功能、参数、返回值
- 复杂逻辑需要内联注释解释**为什么这么做**，而非「做了什么」
- 避免注释代码（删除用版本控制管理）
- TODO 注释需附带 issue 链接或责任人：`# TODO(#123): 需要处理超时重试`
- 不做「显而易见的注释」：

```python
# ✗ 废话注释
x = x + 1  # 将 x 加 1

# ✓ 有用注释
# break 是因为 DeepSeek API 在空 replies 时返回 422
# 而非空数组，临时绕过等待官方修复（#42）
```

### 4.5 错误处理

```python
# ✓ 正确：精确捕获 + 有意义的 fallback
try:
    return json.loads(text)
except json.JSONDecodeError:
    return {"score": 1.0, "violations": []}

# ✗ 不要：吞掉所有异常
try:
    result = do_something()
except Exception:
    pass

# ✗ 不要：过于宽泛的异常捕获
try:
    func()
except:
    pass
```

### 4.6 日志与调试

- 不准提交 `print()` 调试语句（入口 main 函数的流程日志除外）
- 使用 `logging` 模块代替 `print`
- 异常日志包含完整 traceback

```python
import logging
logger = logging.getLogger(__name__)

try:
    ...
except Exception:
    logger.exception("处理对话 %s 时失败", conv_id)
```

---

## 5. Python 专项标准

### 5.1 类型注解

所有公共函数签名必须添加类型注解：

```python
# ✓ 正确
def fetch_conversations(since: datetime.datetime) -> list[dict]: ...
def score_conversation(dialogue: str, system_prompt: str | None = None) -> dict: ...

# ✗ 不要（私有内部函数可以适当放宽）
def fetch_conversations(since):
    ...
```

### 5.2 格式化与 Lint

- 统一使用 **ruff** 作为 linter 和 formatter
- 配置见 `pyproject.toml`（行宽 100, Python 3.11+）
- 提交前运行：`ruff check . && ruff format --check .`

### 5.3 配置管理

- 所有可配置项（API Key、URL、超时、阈值）使用环境变量 + `.env` 文件
- 不允许硬编码敏感信息、URL、端口号
- 提供 `.env.example` 模板（**本项目的 .env.example 缺失，需要补充**）

```python
# ✓ 正确
DIFY_BASE = os.getenv("DIFY_BASE_URL", "http://localhost:80").rstrip("/")

# ✗ 不要
DIFY_BASE = "http://192.168.1.100:80"
DIFY_KEY = "app-xxxxx"
```

### 5.4 并发与线程安全

- `ThreadPoolExecutor` 使用 `max_workers` 作为可配参数（当前已有 `--workers`）
- 线程共享资源必须使用线程安全方式（如 `threading.local()`）
- 全局变量修改需要加锁

当前项目的 thread-local session 用法是好的示例：

```python
# ✓ 正确：每个线程独立的 session
_local = threading.local()
def _get_session():
    if not hasattr(_local, "session"):
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {DIFY_KEY}"
        _local.session = s
    return _local.session
```

### 5.5 测试规范

| 要求 | 说明 |
|------|------|
| **覆盖标准** | 新增代码必须有测试覆盖（分支覆盖率 ≥ 80%） |
| **Bug 修复** | 先写复现 Bug 的测试，再修复 |
| **外部依赖** | 所有 LLM / DB / 网络调用必须 Mock |
| **命名** | `test_<模块>_<场景>` 或 `test_<类>_<方法>_<条件>` |
| **断言** | 使用 `assert` 或 `self.assertXxx`，每个测试至少一个断言 |
| **独立性** | 测试不能互相依赖，不能依赖执行顺序 |
| **临时文件** | 使用 `tmp_path`（pytest）或 `tempfile`（unittest）|
| **清理** | mock 和临时目录在 teardown 中恢复 |

### 5.6 环境隔离

- 本项目使用 `<sys.path.insert>` 方式引入根模块，**测试文件应使用相对可靠的路径方式**

```python
# ✗ 不要：硬编码路径
sys.path.insert(0, "E:/cs-agent")

# ✓ 推荐：基于当前文件计算
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

---

## 6. LLM / AI 应用专项标准

### 6.1 Prompt 安全

| 风险 | 预防措施 |
|------|----------|
| **Prompt 注入** | 用户输入不得直接拼接进 system prompt；需做输入边界校验 |
| **变量泄露** | 确保 `{{#xxx#}}` Dify 变量在 prompt 修改中保持完整 |
| **数据脱敏** | 对话日志存入报告前，检查是否包含个人敏感信息 |
| **Temperature 控制** | 评估场景使用 0.0-0.1；生成场景不超过 0.3 |

### 6.2 LLM 调用规范

```python
# ✓ 正确：统一通过 OpenAI SDK（支持 openai 兼容 API）
r = llm.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": prompt}],
    temperature=0.1,        # 评估场景低温度
    max_tokens=256,          # 始终设上限
)

# ✓ 正确：JSON 解析带 fallback
try:
    return json.loads(text)
except json.JSONDecodeError:
    return {"score": 1.0, "violations": []}  # 保守 fallback
```

### 6.3 多模型协同注意事项

本项目使用三个模型，审查时注意：

1. **DeepSeek（评分）**：`monitor.py` 中 `score_conversation()` 调用
   - 确认传入了正确的 `system_prompt`
   - `max_tokens` 是否足够输出完整 JSON
   - JSON 解析 fallback 是否为保守默认值

2. **Qwen3-14B（留资检测）**：`monitor.py` 中 `detect_conversion()` 调用
   - `max_tokens=20` 足以返回 JSON
   - fallback 逻辑不能误判

3. **Qwen3.5-27B（主管）**：`advisor.py` 中 `generate_candidate()` 调用
   - 确认 `prompt` 中 `current_prompt` 是否被截断（当前截断到 3000 字符）
   - 输出 JSON 解析失败时是否保留了原始 prompt
   - 失败回喂机制中的 `failures` 是否被正确传递

### 6.4 评估通道路径

- `evaluate_candidate()` 的回放机制：确认 history 构建是否正确
- 关键词违规与语义违规的顺序：先关键词后语义，关键词命中直接失败
- 回归集阈值（0.95）不可随意修改，修改需 maintainer 批准

---

## 7. 审查 Checklist

### 7.1 通用 Checklist（所有变更）

```
□ 1. PR 描述完整：改动原因、验证方式、回归影响
□ 2. 无调试残留（print, breakpoint, pdb.set_trace）
□ 3. 无死代码、无用 import
□ 4. 命名符合项目规范（snake_case / UPPER_CASE / PascalCase）
□ 5. docstring 完整（公共函数/类/模块）
□ 6. 类型注解完整（公共函数签名）
□ 7. 异常处理：精确捕获，有意义的 fallback，无 bare `except:`
□ 8. 魔法数字/字符串已提取为常量
□ 9. 硬编码敏感信息已移除
□ 10. 日志使用 logging 模块，非 print
```

### 7.2 Python 专项 Checklist

```
□ 11. 函数 ≤ 50 行，文件 ≤ 500 行（超出需维护者确认）
□ 12. 新增代码有对应的单元测试
□ 13. ruff lint 通过，无 warning
□ 14. ruff format 通过
□ 15. 所有 LLM/DB/网络调用已 mock（测试中）
□ 16. 测试使用 tmp_path / tempfile 管理临时文件
□ 17. 无硬编码的系统路径（如 sys.path.insert）
□ 18. import 分组（标准库→第三方→本地）
□ 19. 环境变量配置有默认值
```

### 7.3 LLM 应用专项 Checklist

```
□ 20. LLM prompt 没有直接拼接用户输入
□ 21. temperature / max_tokens 设置合理
□ 22. JSON 返回有稳健的 parse + fallback
□ 23. Dify 变量（{{#xxx#}}）未丢失或新增
□ 24. 多轮对话回放的历史构建正确
□ 25. 评估用例的 must_not_contain / must_not_violate_rules 完整
□ 26. 回归集阈值未私自修改
□ 27. LLM 调用超时有合理设置
```

### 7.4 安全专项 Checklist

```
□ 28. API Key / Token 未硬编码
□ 29. 用户输入无直接拼接到 system prompt
□ 30. 对话数据是否包含未脱敏的个人信息
□ 31. 文件路径操作使用 Path 而非字符串拼接
□ 32. SQL 查询使用参数化查询（本项目使用 pymysql cursor）
```

---

## 8. 自动化辅助

### 8.1 推荐工具链

| 工具 | 用途 | 配置参考 |
|------|------|----------|
| **ruff** | Lint + Format 二合一 | 见下方 `pyproject.toml` |
| **mypy** | 静态类型检查 | `mypy .` |
| **pytest** | 测试框架 | 已使用 |
| **pre-commit** | Git 钩子自动化 | `.pre-commit-config.yaml` 见下方 |
| **safety / pip-audit** | 依赖安全扫描 | `pip-audit` |

### 8.2 推荐配置

**pyproject.toml：**

```toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "W", "UP", "B", "SIM", "ARG", "C4"]

[tool.ruff.format]
quote-style = "double"

[tool.mypy]
python_version = "3.11"
strict = false
ignore_missing_imports = true

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
```

**.pre-commit-config.yaml：**

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.9.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-added-large-files
        args: ['--maxkb=500']

  - repo: local
    hooks:
      - id: pytest
        name: pytest
        entry: python -m pytest tests/ -v
        language: system
        pass_filenames: false
        always_run: true
```

### 8.3 CI 集成

GitHub Actions 配置参考（`.github/workflows/ci.yml`）：

```yaml
name: CI
on: [push, pull_request]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install ruff mypy
      - run: ruff check .
      - run: ruff format --check .
      - run: mypy .

  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt pytest
      - run: python -m pytest tests/ -v --tb=short
```

---

## 9. 常见违规示例

以下来自本项目实际代码的分析（基于 v1 基线）：

### 9.1 P0 级——必须修复

```python
# ✗ 问题：硬编码路径
sys.path.insert(0, "E:/cs-agent")
# ✓ 修复：基于文件计算
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

```python
# ✗ 问题：捕获笼统 Exception（有多处）
try:
    return json.loads(text)
except Exception:
    return {"score": 1.0, "violations": []}
# ✓ 修复：精确到 json.JSONDecodeError
```

### 9.2 P1 级——应该修复

```python
# ✗ 问题：函数过长，monitor.py 的 main() 约 100 行
# ✓ 应当拆分为：fetch_phase()、score_phase()、report_phase()
```

```python
# ✗ 问题：缺少类型注解（score_conversation 等公共函数）
def score_conversation(dialogue, system_prompt=None):
# ✓ 应当补齐
def score_conversation(dialogue: str, system_prompt: str | None = None) -> dict:
```

```python
# ✗ 问题：缺少 .env.example
# 新成员不知道需要配置哪些环境变量
# ✓ 应当补一个 .env.example 包含所有键（值留空或用占位符）
```

### 9.3 P2 级——建议改进

```python
# monitor.py (618行) 和 advisor.py (1044行) 偏大
# 建议拆分为：
#   dify_client.py    — Dify API 封装
#   scoring.py        — 评分逻辑
#   report_builder.py — 日报/周报生成
#   advisor_core.py   — 主管循环核心
```

---

## 10. 附录

### 10.1 审查速度参考

| 改动规模 | 审查时限 | 建议块大小 |
|----------|----------|------------|
| < 100 行 | 24 小时内 | - |
| 100-400 行 | 48 小时内 | 逐行审查 |
| > 400 行 | 要求拆分提交 | 不符合规范，退回拆分 |

### 10.2 审查意见标签

```
[P0] 安全漏洞 / 功能破坏 — 必须修复才能合并
[P1] 代码异味 / 边界缺失 — 要求回应或修复
[P2] 风格/可读性建议 — 不阻塞合并
[Q]  提问 — 需要澄清理解，不要求改代码
```

### 10.3 代码健康度指标（目标值）

| 指标 | 目标 | 测量方式 |
|------|------|----------|
| 静态类型覆盖 | ≥ 80% 公共函数 | mypy --strict |
| 行覆盖率 | ≥ 80% | pytest --cov |
| 文件大小 | ≤ 500 行/文件 | wc -l |
| 函数大小 | ≤ 50 行/函数 | ruff |
| 审查响应时间 | ≤ 48 小时 | PR 统计数据 |
| PR 退回率 | ≤ 20%（因 P0/P1 退回） | PR 统计数据 |

### 10.4 修订历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-04-30 | 初版制定，基于 agent-monitor 项目基线分析 |
