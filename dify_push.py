"""把 system_prompt.md 推送到 Dify chatflow 应用的 LLM 节点。

Dify 1.13+ Console API 流程：
  1. POST /console/api/login   body: {email, password=base64(pw), remember_me}
     → Set-Cookie: access_token / refresh_token / csrf_token
  2. GET  /console/api/apps/{app_id}/workflows/draft → {graph, hash, ...}
  3. 改 graph.nodes[type=llm].data.prompt_template[role=system].text = 新 prompt
  4. POST /console/api/apps/{app_id}/workflows/draft
     body: {graph, features, hash, environment_variables, conversation_variables}
  5. POST /console/api/apps/{app_id}/workflows/publish

注意 Dify 的 "encrypt" 实际就是 base64（源码自承）。
"""
import os, sys, base64, logging
from typing import Any
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class DifyPushError(RuntimeError):
    """Dify 推送失败的根错误。"""


def _check_config() -> tuple[str, str, str, str]:
    """每次都从 os.environ 读，方便测试 monkeypatch。"""
    cfg = {
        "DIFY_BASE_URL":       (os.getenv("DIFY_BASE_URL") or "").rstrip("/"),
        "DIFY_APP_ID":         os.getenv("DIFY_APP_ID") or "",
        "DIFY_ADMIN_EMAIL":    os.getenv("DIFY_ADMIN_EMAIL") or "",
        "DIFY_ADMIN_PASSWORD": os.getenv("DIFY_ADMIN_PASSWORD") or "",
    }
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise DifyPushError(f"缺少环境变量：{missing}")
    return cfg["DIFY_BASE_URL"], cfg["DIFY_APP_ID"], cfg["DIFY_ADMIN_EMAIL"], cfg["DIFY_ADMIN_PASSWORD"]


def _login(base: str, email: str, password: str, timeout: int = 10) -> requests.Session:
    s = requests.Session()
    encoded_pw = base64.b64encode(password.encode("utf-8")).decode("ascii")
    r = s.post(
        f"{base}/console/api/login",
        json={"email": email, "password": encoded_pw, "remember_me": False},
        timeout=timeout,
    )
    if r.status_code != 200:
        raise DifyPushError(f"登录失败 HTTP {r.status_code}: {r.text[:200]}")
    payload = r.json()
    if payload.get("result") != "success":
        raise DifyPushError(f"登录返回非 success: {payload}")
    if "access_token" not in s.cookies:
        raise DifyPushError(f"登录成功但未拿到 access_token cookie")
    return s


def _get_draft(s: requests.Session, base: str, app_id: str, timeout: int = 15) -> dict[str, Any]:
    r = s.get(f"{base}/console/api/apps/{app_id}/workflows/draft", timeout=timeout)
    if r.status_code != 200:
        raise DifyPushError(f"读取 draft 失败 HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _save_draft(s: requests.Session, base: str, app_id: str, body: dict, timeout: int = 30) -> dict[str, Any]:
    r = s.post(f"{base}/console/api/apps/{app_id}/workflows/draft", json=body, timeout=timeout)
    if r.status_code not in (200, 201):
        raise DifyPushError(f"保存 draft 失败 HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def _publish(s: requests.Session, base: str, app_id: str, timeout: int = 30) -> dict[str, Any]:
    r = s.post(f"{base}/console/api/apps/{app_id}/workflows/publish", json={}, timeout=timeout)
    if r.status_code not in (200, 201):
        raise DifyPushError(f"发布 workflow 失败 HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def _patch_graph(graph: dict, new_system_prompt: str) -> tuple[dict, str]:
    """就地修改 graph.nodes[type=llm].prompt_template[role=system].text。
    返回 (graph, llm_node_id)；找不到 LLM 节点或 system 段则抛错。
    """
    nodes = graph.get("nodes", [])
    llm_nodes = [n for n in nodes if n.get("data", {}).get("type") == "llm"]
    if not llm_nodes:
        raise DifyPushError("graph 中未找到 type=llm 的节点")
    if len(llm_nodes) > 1:
        # 简单策略：仅当有唯一 LLM 节点时才推；多 LLM 需要人工指定
        ids = [n.get("id", "?") for n in llm_nodes]
        raise DifyPushError(f"graph 中有多个 LLM 节点 {ids}，需要人工指定要更新哪个")

    llm = llm_nodes[0]
    pt = llm["data"].get("prompt_template", [])
    system_entries = [p for p in pt if p.get("role") == "system"]
    if not system_entries:
        raise DifyPushError(f"LLM 节点 {llm.get('id')} 的 prompt_template 中没有 role=system 项")
    if len(system_entries) > 1:
        raise DifyPushError(f"LLM 节点 {llm.get('id')} 的 prompt_template 有多条 role=system，无法决定")

    system_entries[0]["text"] = new_system_prompt
    return graph, llm.get("id", "")


def push_prompt(new_system_prompt: str, dry_run: bool = False) -> dict[str, Any]:
    """端到端推送：登录 → 取 draft → 改 LLM 节点 → 保存 draft → 发布。
    dry_run=True 仅做修改，不调 publish 端点（前面四步都跑，便于排查）。
    成功返回元数据；失败抛 DifyPushError。
    """
    base, app_id, email, password = _check_config()

    s = _login(base, email, password)
    draft = _get_draft(s, base, app_id)
    graph = draft.get("graph") or {}
    if not graph.get("nodes"):
        raise DifyPushError("draft.graph.nodes 为空")

    graph, llm_id = _patch_graph(graph, new_system_prompt)

    body = {
        "graph":                  graph,
        "features":               draft.get("features", {}),
        "hash":                   draft.get("hash", ""),
        "environment_variables":  draft.get("environment_variables", []),
        "conversation_variables": draft.get("conversation_variables", []),
    }
    save_resp = _save_draft(s, base, app_id, body)

    if dry_run:
        return {"ok": True, "stage": "draft_saved", "llm_node": llm_id, "save": save_resp}

    publish_resp = _publish(s, base, app_id)
    return {
        "ok": True,
        "stage": "published",
        "llm_node": llm_id,
        "publish": publish_resp,
    }


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="推送 system_prompt 到 Dify")
    parser.add_argument("path", nargs="?", default="prompts/system_prompt.md",
                        help="要推送的 prompt 文件，默认 prompts/system_prompt.md")
    parser.add_argument("--dry-run", action="store_true",
                        help="只保存 draft，不调 publish（生产生效需要 publish）")
    args = parser.parse_args()

    p = Path(args.path)
    if not p.exists():
        print(f"  [X] 文件不存在：{p}")
        sys.exit(1)

    new_prompt = p.read_text(encoding="utf-8")
    print(f"  -> 读取 {p}（{len(new_prompt)} 字符）")
    try:
        result = push_prompt(new_prompt, dry_run=args.dry_run)
        action = "已保存到 draft（未发布）" if args.dry_run else "已发布到生产"
        print(f"  [OK] {action}，LLM 节点 = {result['llm_node']}")
    except DifyPushError as e:
        print(f"  [X] 推送失败：{e}")
        sys.exit(2)
