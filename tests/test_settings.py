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

    def test_kb_keys_present(self, defaults):
        """知识库配置项存在"""
        kb_keys = ["kb_enabled", "kb_api_base", "kb_token",
                    "kb_project_id", "kb_search_max_chars", "email_body_max_chars"]
        for key in kb_keys:
            assert key in defaults, f"Missing KB key: {key}"

    def test_port_default_is_8020(self, defaults):
        assert defaults["system_port"] == "8020"

    def test_truncation_defaults(self, defaults):
        """截断默认值正确"""
        assert defaults["kb_search_max_chars"] == "5000"
        assert defaults["email_body_max_chars"] == "8000"

    def test_kb_defaults(self, defaults):
        """知识库默认值正确"""
        assert defaults["kb_enabled"] == "false"
        assert defaults["kb_api_base"] == "http://127.0.0.1:19828"
        assert defaults["kb_token"] == ""
        assert defaults["kb_project_id"] == ""


class TestGetKbConfig:
    """get_kb_config 函数"""

    def test_import(self):
        """函数可导入"""
        from app.routes.settings import get_kb_config
        assert callable(get_kb_config)


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
