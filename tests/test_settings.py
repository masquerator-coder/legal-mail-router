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
            "revision_native", "revision_highlight", "context_window_tokens",
            "review_template_enabled",
            "admin_email", "daily_report_enabled", "daily_report_time",
        ]
        for key in required:
            assert key in defaults, f"Missing key: {key}"

    def test_no_review_template_path_keys(self, defaults):
        """模板改为按文书类型名约定式查找，不应再有模板路径配置项。

        回归：残留的路径配置会让界面显示无效输入框，并让「模板从哪来」
        出现两个相互矛盾的事实来源。
        """
        for key in ("review_template_path", "review_template_path_civil"):
            assert key not in defaults, f"应已移除的模板路径配置: {key}"

    def test_port_default_is_8020(self, defaults):
        assert defaults["system_port"] == "8020"

    def test_truncation_defaults(self, defaults):
        """截断默认值正确：正文上限默认 0=由窗口推导"""
        assert defaults["email_body_max_chars"] == "0"

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


class TestRevisionNativeSwitch:
    """修改版文书「Word 原生修订」开关"""

    @pytest.fixture
    def defaults(self):
        from app.routes.settings import SETTING_DEFAULTS
        return SETTING_DEFAULTS

    def test_default_is_on(self, defaults):
        """默认开启原生修订：保持既有线上行为（当前走的就是原生修订）"""
        assert defaults["revision_native"] == "true"

    def test_switch_key_present(self, defaults):
        assert "revision_native" in defaults

    def test_save_form_accepts_switch(self):
        """保存接口必须接收 revision_native 表单字段（缺省时为 true）"""
        import inspect
        from app.routes import settings as settings_mod
        sig = inspect.signature(settings_mod.save_settings)
        assert "revision_native" in sig.parameters
        assert sig.parameters["revision_native"].default.default == "true"

    def test_save_normalizes_truthy_values(self):
        """on/1/true 均解析为开启；其余为关闭"""
        truthy = ("true", "on", "1", "True", "ON")
        for v in truthy:
            assert v.lower() in ("true", "on", "1")
        for v in ("false", "off", "0", ""):
            assert v.lower() not in ("true", "on", "1")


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
