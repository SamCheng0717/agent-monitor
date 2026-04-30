"""agent-monitor 飞轮可视化 web 服务。
内网访问，无鉴权。读取 reports/ + prompts/ + tests/ 目录现有数据。
"""
import sys, json, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import advisor as adv

app = FastAPI(title="agent-monitor 数据飞轮")

WEB_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def home():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
def api_status():
    """聚合状态（同 advisor.py --status）"""
    s = adv.collect_status()
    # datetime 不可 JSON 序列化，平摊一下
    pending = []
    for p in s["pending"]:
        pending.append({
            "version": p["version"],
            "path":    p["path"],
            "mtime":   p["mtime"].isoformat() if p["mtime"] else None,
        })
    s["pending"] = pending
    return s


@app.get("/api/today")
def api_today():
    """今天（或最近）的 monitor 结构化数据。"""
    today = datetime.date.today()
    for back in range(7):
        date = (today - datetime.timedelta(days=back)).isoformat()
        json_path = adv.REPORTS_DIR / f"{date}.json"
        if not json_path.exists():
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        data["_data_date"] = date

        violations: dict[str, int] = {}
        for r in data.get("bad_conversations", []):
            for v in r.get("violations", []):
                rule = (v.get("rule") or "")[:80]
                if rule:
                    violations[rule] = violations.get(rule, 0) + 1
        top = sorted(violations.items(), key=lambda x: -x[1])[:8]
        data["top_violations"] = [{"rule": r, "count": c} for r, c in top]
        return data
    return {"_data_date": None, "summary": {}, "bad_conversations": [], "top_violations": []}


@app.get("/api/history")
def api_history(days: int = 30):
    """近 N 天 monitor 留资率/对话量序列。"""
    stats_path = adv.REPORTS_DIR / "stats.json"
    if not stats_path.exists():
        return []
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return [s for s in stats if s.get("date", "") >= cutoff]


@app.get("/api/advisor-history")
def api_advisor_history(days: int = 30):
    """近 N 天 advisor 行动列表。"""
    if not adv.ADVISOR_LOG_DIR.exists():
        return {}
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    out: dict[str, list] = {}
    for p in sorted(adv.ADVISOR_LOG_DIR.glob("????-??-??.json")):
        date = p.stem
        if date < cutoff:
            continue
        try:
            out[date] = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
    return out


@app.get("/api/versions")
def api_versions():
    """版本时间线：v000-vNNN。"""
    if not adv.VERSIONS_DIR.exists():
        return []
    out = []
    for p in sorted(adv.VERSIONS_DIR.glob("v*.md")):
        try:
            ver, date = p.stem.split("_", 1)
        except ValueError:
            ver, date = p.stem, ""
        out.append({"version": ver, "date": date, "size": p.stat().st_size})
    return out


@app.get("/api/pending/{version}")
def api_pending_diff(version: str):
    """获取 pending 候选 + 当前 prompt + 元数据，给前端做 diff。"""
    md_files = sorted(adv.PENDING_DIR.glob(f"pending_{version}_*.md"))
    if not md_files:
        raise HTTPException(404, f"未找到 pending_{version}")
    candidate = md_files[-1].read_text(encoding="utf-8")
    current = adv.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8") if adv.SYSTEM_PROMPT_PATH.exists() else ""
    meta_path = md_files[-1].with_suffix(".json")
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "version":   version,
        "current":   current,
        "candidate": candidate,
        "metadata":  meta,
    }


@app.post("/api/approve/{version}")
def api_approve(version: str):
    try:
        info = adv.approve_pending(version)
        adv._record_advisor_log(
            datetime.date.today().isoformat(),
            {"action": "approved", "version": info["version"], "module": info.get("module", ""), "via": "web"},
        )
        return {"ok": True, **info}
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/regression/candidates")
def api_regression_candidates(top_per_rule: int = 5):
    """挖掘候选：扫 reports 按违规规则分组，跳过已晋升的。"""
    return adv.get_mining_candidates(top_per_rule=top_per_rule)


from fastapi import Body

@app.post("/api/regression/promote")
def api_regression_promote(payload: dict = Body(...)):
    """selections: [{source, rule}, ...] → 写入 regression_set_real.json。"""
    selections = payload.get("selections") or []
    if not isinstance(selections, list):
        raise HTTPException(400, "selections must be a list")
    added = adv._promote_by_sources(selections)
    adv._record_advisor_log(
        datetime.date.today().isoformat(),
        {"action": "regression_promoted", "added": added, "via": "web"},
    )
    return {"ok": True, "added": added}


@app.post("/api/reject/{version}")
def api_reject(version: str):
    """删除 pending，不发布。"""
    md_files = sorted(adv.PENDING_DIR.glob(f"pending_{version}_*.md"))
    if not md_files:
        raise HTTPException(404, f"未找到 pending_{version}")
    for md in md_files:
        meta = md.with_suffix(".json")
        md.unlink()
        if meta.exists():
            meta.unlink()
    adv._record_advisor_log(
        datetime.date.today().isoformat(),
        {"action": "rejected", "version": version, "via": "web"},
    )
    return {"ok": True, "version": version, "action": "rejected"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
