"""
系统设置 — 配置完整性测试
"""
import pytest
import sys
import os

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestSettingsDefaults:
    """SETTING_DEFAULTS 完整性"""

    @pytest.fixture
    def defaults(self):
        from app.routes.settings import SETTING_DEFAULTS
        return SETTING_DEFAULTS

    def test_all_required_keys_present(self, defaults):
        """所有必要配置项存在"""
        required = [
            "system_name", "system_port",
            "default_check_interval", "monitor_days",
            "log_retention_days", "llm_retry_interval", "llm_max_retries",
            "analysis_output_mode", "revision_enabled",
            "revision_highlight", "context_window_tokens",
            "review_template_enabled", "review_template_path",
            "admin_email", "daily_report_enabled", "daily_report_time",
        ]
        for key in required:
            assert key in defaults, f"Missing key: {key}"

    def test_port_default_is_8020(self, defaults):
        assert defaults["system_port"] == "8020"

    def test_truncation_defaults(self, defaults):
        """截断默认值正确"""
        assert defaults["email_body_max_chars"] == "8000"

    def test_no_kb_keys(self, defaults):
        """知识库功能已移除，不应再存在 kb_* 配置项"""
        for key in ("kb_enabled", "kb_api_base", "kb_token",
                    "kb_project_id", "kb_search_max_chars"):
            assert key not in defaults, f"KB key should be removed: {key}"

    def test_mcp_keys_present(self, defaults):
        """MCP 配置项存在"""
        for key in ("mcp_enabled", "mcp_servers", "mcp_max_turns"):
            assert key in defaults, f"Missing MCP key: {key}"

    def test_mcp_defaults(self, defaults):
        """MCP 默认值正确"""
        assert defaults["mcp_enabled"] == "false"
        assert defaults["mcp_max_turns"] == "5"
        # 预填北大法宝 JSON，Token 为占位符，不写真实凭据
        import json as _json
        cfg = _json.loads(defaults["mcp_servers"])
        assert "mcpServers" in cfg
        assert "pkulaw-law-search" in cfg["mcpServers"]
        auth = cfg["mcpServers"]["pkulaw-law-search"]["headers"]["Authorization"]
        assert "__PKULAW_TOKEN__" in auth
        assert "620dcbb8" not in auth  # 不得泄露真实 Token


class TestPortDefaults:
    """端口相关默认值"""

    def test_config_system_port(self):
        from app.settings import SYSTEM_PORT
        assert SYSTEM_PORT == "8020"

    def test_config_set_system_port(self):
        import app.settings as cfg
        original = cfg.SYSTEM_PORT
        cfg.set_system_port("9999")
        assert cfg.SYSTEM_PORT == "9999"
        cfg.set_system_port(original)
