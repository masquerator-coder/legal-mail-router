"""
法律知识库客户端 — 对接 LLM Wiki HTTP API 进行法律/法规检索
"""
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── 默认值 ──
DEFAULT_API_BASE = "http://127.0.0.1:19828"
DEFAULT_TIMEOUT = 15.0  # 知识库检索超时（秒），不宜太长以免阻塞分析


async def health_check(api_base: str = DEFAULT_API_BASE) -> bool:
    """检查知识库服务是否可用（无需鉴权）"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{api_base.rstrip('/')}/api/v1/health")
            return resp.status_code == 200
    except Exception:
        return False


async def list_projects(
    api_base: str = DEFAULT_API_BASE,
    token: str = "",
) -> list[dict]:
    """获取知识库项目列表"""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            f"{api_base.rstrip('/')}/api/v1/projects",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


async def search_knowledge(
    query: str,
    project_id: str,
    api_base: str = DEFAULT_API_BASE,
    token: str = "",
    top_k: int = 5,
) -> dict:
    """
    Hybrid 混合检索（关键词 + 向量）

    返回:
    {
        "mode": "hybrid",
        "tokenHits": [{"file": ..., "content": ..., "score": ...}, ...],
        "vectorHits": [{"file": ..., "content": ..., "score": ..., "vectorScore": ...}, ...],
    }
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = {"query": query, "topK": top_k}

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base.rstrip('/')}/api/v1/projects/{project_id}/search",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


def format_search_results(search_result: dict, max_chars: int = 2000) -> str:
    """
    将知识库检索结果格式化为 prompt 可用的法律参考文本。

    合并 token hits + vector hits，去重（按 file），
    截断到 max_chars 以内。
    """
    seen_files: set[str] = set()
    chunks: list[str] = []
    total_chars = 0

    # 优先取向量检索结果（语义匹配），再补充关键词结果
    all_hits = []
    for hit in search_result.get("vectorHits", []):
        all_hits.append((hit, hit.get("vectorScore", 0)))
    for hit in search_result.get("tokenHits", []):
        all_hits.append((hit, hit.get("score", 0)))

    # 按分数降序
    all_hits.sort(key=lambda x: x[1], reverse=True)

    for hit, _ in all_hits:
        file_path = hit.get("file", "未知文件")
        if file_path in seen_files:
            continue
        seen_files.add(file_path)

        content = hit.get("content", "").strip()
        if not content:
            continue

        entry = f"【{file_path}】\n{content}"
        if total_chars + len(entry) > max_chars:
            # 截断最后一条以不超限
            remaining = max_chars - total_chars - 20
            if remaining > 100:
                entry = entry[:remaining] + "...(截断)"
                chunks.append(entry)
            break

        chunks.append(entry)
        total_chars += len(entry)

    if not chunks:
        return ""

    return "## 相关法律法规参考（来自知识库）\n" + "\n\n".join(chunks)


async def search_and_format(
    query: str,
    project_id: str,
    api_base: str = DEFAULT_API_BASE,
    token: str = "",
    top_k: int = 5,
    max_chars: int = 2000,
) -> str:
    """
    一站式：检索 + 格式化，失败时返回空字符串（不中断主流程）
    """
    if not project_id:
        return ""

    try:
        result = await search_knowledge(query, project_id, api_base, token, top_k)
        return format_search_results(result, max_chars)
    except httpx.HTTPStatusError as e:
        logger.warning("知识库检索 HTTP 错误 (%d): %s", e.response.status_code, str(e)[:100])
        return ""
    except httpx.TimeoutException:
        logger.warning("知识库检索超时")
        return ""
    except Exception as e:
        logger.warning("知识库检索失败: %s", str(e)[:100])
        return ""
