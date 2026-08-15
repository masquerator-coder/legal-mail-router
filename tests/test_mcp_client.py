"""
MCP 客户端 — 纯逻辑单元测试（不依赖网络/凭据）。
包含服务器配置解析与引用链接兜底助手。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.mcp_client import parse_server_configs
from app.services.llm_analyzer import _finalize_with_citations


class TestParseServerConfigs:
    """各种配置形态的归一化"""

    def test_mcp_servers_object_with_wrapped_url(self):
        cfg = {"mcpServers": {
            "pkulaw-law-search": {
                "type": "streamableHttp",
                "url": "@url:`https://apim-gateway.pkulaw.com/mcp-law-search-service`",
                "headers": {"Authorization": "Bearer xyz"},
            },
        }}
        servers = parse_server_configs(cfg)
        assert len(servers) == 1
        s = servers[0]
        assert s.name == "pkulaw-law-search"
        assert s.url == "https://apim-gateway.pkulaw.com/mcp-law-search-service"
        assert s.headers == {"Authorization": "Bearer xyz"}

    def test_list_form(self):
        servers = parse_server_configs([
            {"name": "s1", "url": "https://x.example", "headers": {"a": "b"}},
        ])
        assert len(servers) == 1
        assert servers[0].name == "s1"
        assert servers[0].url == "https://x.example"

    def test_name_map_dict(self):
        servers = parse_server_configs({"s2": {"url": "https://y.example"}})
        assert len(servers) == 1
        assert servers[0].name == "s2"
        assert servers[0].url == "https://y.example"
        assert servers[0].headers == {}

    def test_empty_and_invalid(self):
        assert parse_server_configs(None) == []
        assert parse_server_configs("") == []
        assert parse_server_configs("   ") == []
        assert parse_server_configs("{not-json{{") == []

    def test_skips_missing_url(self):
        assert parse_server_configs({"s": {"type": "streamableHttp"}}) == []
        assert parse_server_configs({"s": {"url": ""}}) == []

    def test_plain_url_untouched(self):
        servers = parse_server_configs({"s": {"url": "http://127.0.0.1:8080/mcp"}})
        assert servers[0].url == "http://127.0.0.1:8080/mcp"


class TestFinalizeWithCitations:
    """引用链接兜底追加"""

    class _FakeMCP:
        def __init__(self, links):
            self.citation_links = links

    def test_appends_when_missing(self):
        out = _finalize_with_citations(
            {"ai_interpretation": "报告正文", "revised_document": "正文"},
            True, self._FakeMCP(["https://pkulaw.com/chl/abc.html"]),
        )
        assert "https://pkulaw.com/chl/abc.html" in out["ai_interpretation"]
        assert "https://pkulaw.com/chl/abc.html" in out["revised_document"]
        assert "引用依据" in out["ai_interpretation"]

    def test_noop_when_not_used(self):
        out = _finalize_with_citations({"ai_interpretation": "报告"}, False, None)
        assert out["ai_interpretation"] == "报告"

    def test_noop_when_no_links(self):
        out = _finalize_with_citations({"ai_interpretation": "报告"}, True, self._FakeMCP([]))
        assert out["ai_interpretation"] == "报告"

    def test_no_double_append(self):
        out = _finalize_with_citations(
            {"ai_interpretation": "报告 https://pkulaw.com/x.html", "revised_document": None},
            True, self._FakeMCP(["https://pkulaw.com/x.html"]),
        )
        assert out["ai_interpretation"].count("pkulaw.com") == 1

    def test_dedup_links(self):
        out = _finalize_with_citations(
            {"ai_interpretation": "报告"},
            True, self._FakeMCP(["https://pkulaw.com/a.html", "https://pkulaw.com/a.html"]),
        )
        assert out["ai_interpretation"].count("https://pkulaw.com/a.html") == 1
