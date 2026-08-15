"""
MCP（Model Context Protocol）客户端 — 连接 MCP 工具服务器，供 LLM 分析时实时调用工具。

典型用途：连接「北大法宝」等法律法规检索服务器，让 LLM 在文书分析阶段按需查询
权威法规条文，并将来源链接注入报告/修改版文书。

依赖:
- mcp>=2.0.0（streamable HTTP transport）
- httpx（携带 Authorization 等自定义头）

配置形态（宽松兼容）:
1. mcpServers 对象：{"mcpServers": {"name": {"type": "streamableHttp", "url": "...", "headers": {...}}}}
2. 服务器列表：      [{"name": "...", "url": "...", "headers": {...}}, ...]
3. 名称映射字典：    {"name": {"url": "...", "headers": {...}}}

url 支持 "@url:`https://...`" 包裹形式，会自动去除包裹。
"""
import json
import logging
import re
from dataclasses import dataclass, field

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0


@dataclass
class MCPServer:
    """单个 MCP 服务器配置"""
    name: str
    url: str
    headers: dict = field(default_factory=dict)


@dataclass
class MCPTool:
    """单个 MCP 工具（含所属服务器名）"""
    name: str
    description: str
    input_schema: dict
    server: str  # 所属服务器名称


def _clean_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("@url:`") and url.endswith("`"):
        url = url[len("@url:`"):-1].strip()
    return url


def parse_server_configs(raw) -> list[MCPServer]:
    """将用户粘贴的各种 MCP 服务器配置归一化为 list[MCPServer]。

    解析失败或为空返回 []（不抛异常，调用方据此优雅降级）。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("MCP 服务器配置 JSON 解析失败: %s", e)
            return []

    # 支持 {"mcpServers": {...}} 包裹
    if isinstance(raw, dict) and isinstance(raw.get("mcpServers"), dict):
        raw = raw["mcpServers"]

    # 归一化为 [(name, cfg), ...]
    if isinstance(raw, dict):
        items = [(name, cfg) for name, cfg in raw.items()]
    elif isinstance(raw, list):
        items = []
        for it in raw:
            if isinstance(it, dict):
                name = it.get("name") or it.get("serverName") or ""
                items.append((name, it))
    else:
        return []

    servers = []
    for name, cfg in items:
        if not isinstance(cfg, dict):
            continue
        url = _clean_url(cfg.get("url") or "")
        if not url:
            continue
        headers = cfg.get("headers") or {}
        if not isinstance(headers, dict):
            headers = {}
        servers.append(MCPServer(name=name or url, url=url, headers=dict(headers)))
    return servers


class MCPClient:
    """MCP 客户端：连接多个服务器，发现工具，按需调用。

    用法（async）:
        async with MCPClient(servers) as mcp:
            tools = mcp.list_openai_tools()   # OpenAI function 定义
            result = await mcp.call("adjust_provisions", {...})
    """

    def __init__(self, servers: list[MCPServer] | list[dict]):
        self.servers = [s if isinstance(s, MCPServer) else MCPServer(**{
            "name": s.get("name", ""), "url": s.get("url", ""),
            "headers": s.get("headers") or {},
        }) for s in (servers or []) if isinstance(s, (MCPServer, dict))]

        self._http_clients: dict[str, httpx.AsyncClient] = {}
        self._cms: dict[str, object] = {}           # streamable_http_client 上下文（保持开启）
        self._sessions: dict[str, ClientSession] = {}  # server -> session
        self.tools: list[MCPTool] = []
        self._by_name: dict[str, MCPTool] = {}
        self.citation_links: list[str] = []  # 从工具结果中提取到的法规来源链接
        self.connected_servers: list[str] = []

    # ── 生命周期 ──
    async def __aenter__(self) -> "MCPClient":
        await self._connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        for s in self._sessions.values():
            try:
                await s.__aexit__(exc_type, exc, tb)
            except BaseException as e:
                logger.debug("MCP session close error: %s", e)
        for cm in self._cms.values():
            try:
                await cm.__aexit__(exc_type, exc, tb)
            except BaseException as e:
                logger.debug("MCP transport close error: %s", e)
        for c in self._http_clients.values():
            try:
                await c.aclose()
            except BaseException as e:
                logger.debug("MCP http close error: %s", e)

    async def _connect(self):
        for s in self.servers:
            try:
                http_client = httpx.AsyncClient(headers=s.headers, timeout=DEFAULT_TIMEOUT)
                self._http_clients[s.url] = http_client
                cm = streamable_http_client(s.url, http_client=http_client, terminate_on_close=False)
                read_stream, write_stream = await cm.__aenter__()
                self._cms[s.url] = cm
                session = ClientSession(read_stream, write_stream)
                await session.__aenter__()
                await session.initialize()
                self._sessions[s.name] = session
                self.connected_servers.append(s.name)
                tools = await session.list_tools()
                added = 0
                for t in tools.tools:
                    name = getattr(t, "name", "")
                    if not name or name in self._by_name:
                        continue
                    tool = MCPTool(
                        name=name,
                        description=getattr(t, "description", "") or "",
                        input_schema=getattr(t, "input_schema", None) or {"type": "object", "properties": {}},
                        server=s.name,
                    )
                    self.tools.append(tool)
                    self._by_name[name] = tool
                    added += 1
                logger.info("MCP 服务器「%s」已连接，发现 %d 个工具（新 %d）", s.name, len(tools.tools), added)
            except BaseException as e:
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                # mcp SDK 在鉴权失败/服务不可用时常抛 asyncio.CancelledError（BaseException），
                # 必须捕获并作为「该服务器连接失败」优雅降级，不得向上冒泡成 500。
                logger.warning("MCP 服务器「%s」连接失败（跳过）: [%s] %s", s.name, type(e).__name__, str(e)[:200])
                await self._cleanup_server(s)

    async def _cleanup_server(self, s: MCPServer):
        try:
            if s.name in self._sessions:
                await self._sessions.pop(s.name).__aexit__(None, None, None)
            if s.url in self._cms:
                await self._cms.pop(s.url).__aexit__(None, None, None)
            if s.url in self._http_clients:
                await self._http_clients.pop(s.url).aclose()
        except BaseException:
            pass

    # ── 工具清单 ──
    def list_openai_tools(self) -> list[dict]:
        """转换为 OpenAI Chat Completions 的 function calling 定义。"""
        out = []
        for t in self.tools:
            out.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.input_schema or {"type": "object", "properties": {}},
                },
            })
        return out

    def has_tools(self) -> bool:
        return bool(self.tools)

    # ── 工具调用 ──
    async def call(self, name: str, args: dict | None) -> str:
        """调用指定 MCP 工具，返回文本化结果；失败返回以 ERROR 开头的消息（不抛出，供模型继续）。"""
        tool = self._by_name.get(name)
        if not tool:
            return f"ERROR: 工具 '{name}' 不存在或未连接"
        session = self._sessions.get(tool.server)
        if not session:
            return f"ERROR: 工具 '{name}' 所在服务器未连接"
        try:
            res = await session.call_tool(name, args or {})
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.warning("MCP 工具「%s」调用异常: [%s] %s", name, type(e).__name__, str(e)[:200])
            return f"ERROR: 工具 '{name}' 调用失败: {e}"

        texts = []
        for c in res.content:
            if getattr(c, "type", None) == "text":
                texts.append(getattr(c, "text", "") or "")
            else:
                texts.append(str(c))
        body = "\n".join(t for t in texts if t)
        # 提取法规来源链接（pkulaw 等）
        for url in re.findall(r"https?://[^\s\]\[)\"']+", body):
            if url not in self.citation_links:
                self.citation_links.append(url)
        return body if body else "(工具返回空结果)"


def list_mcp_tools(servers: list[MCPServer]) -> dict:
    """在独立线程 + 独立事件循环里连接并枚举 MCP 工具，返回可 JSON 序列化的字典。

    为什么必须在线程里跑：mcp SDK 重度使用 anyio 任务组（streamable_http_client
    内部维护一个后台读取任务组）。若直接在 FastAPI 异步端点里运行，该读取任务
    存活在请求事件循环中，一旦套了基于 Starlette BaseHTTPMiddleware 的中间件
    （如本项目 /settings 的 CSRF 中间件），请求结束时中间件的 collapsing task
    group 会去取消这个后台任务，触发
    'Attempted to exit a cancel scope that isn't the current task's current cancel scope'
    的 ExceptionGroup，最终返回 HTTP 500（即使连接全部成功）。

    这里用 asyncio.run() 在线程里开一个全新的事件循环，让 SDK 的取消作用域
    与该循环共存亡，与请求生命周期彻底隔离。任何连接失败/超时都优雅降级
    （success=False），绝不向上抛异常 → 绝不 500。调用方在 async 上下文里用
    ``await asyncio.to_thread(list_mcp_tools, servers)`` 调用。
    """
    import asyncio

    async def worker():
        async with MCPClient(servers) as mcp:
            tools = []
            for t in mcp.tools:
                tools.append({
                    "name": t.name,
                    "server": t.server,
                    "description": (t.description or "")[:150],
                    "params": list((t.input_schema or {}).get("properties", {}).keys()),
                })
            failed = [s.name for s in servers if s.name not in mcp.connected_servers]
            return {
                "success": True,
                "connected": list(mcp.connected_servers),
                "failed": failed,
                "tools": tools,
                "message": f"已连接 {len(mcp.connected_servers)}/{len(servers)} 个服务器，发现 {len(tools)} 个工具",
            }

    try:
        return asyncio.run(worker())
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        logger.error("MCP 连接/枚举失败: %s", e)
        return {"success": False, "message": f"MCP 连接/枚举失败: {e}", "tools": []}

