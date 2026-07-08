"""
LLM 分析器 — prompt 构建 & 截断逻辑测试
"""
import pytest
from app.llm_analyzer import build_prompt, DEFAULT_ANALYSIS_PROMPT


class TestBuildPrompt:
    """测试提示词构建"""

    def test_basic_prompt_structure(self):
        prompt = build_prompt(
            subject="合同审核",
            sender="client@example.com",
            body="请审核附件中的合同条款。",
        )
        assert "合同审核" in prompt
        assert "client@example.com" in prompt
        assert "请审核附件中的合同条款" in prompt
        assert "doc_type" in prompt  # JSON 格式要求

    def test_body_truncation_default(self):
        """默认 8000 字符截断"""
        long_body = "测试内容。" * 2000  # ~12000 chars
        prompt = build_prompt(
            subject="test", sender="s", body=long_body,
        )
        # 截断后的正文不应全量出现
        assert long_body not in prompt
        # 但提示词仍包含关键结构
        assert "doc_type" in prompt

    def test_body_truncation_custom(self):
        """自定义截断值"""
        body = "A" * 5000
        prompt = build_prompt(
            subject="test", sender="s", body=body,
            body_max_chars=100,
        )
        # 截断后只应保留前100个A，不应出现200个连续的A
        assert "A" * 200 not in prompt
        assert "A" * 100 in prompt
        assert len(prompt) < len(body) + 2000  # 不应该包含完整原文

    def test_body_no_truncation(self):
        """body_max_chars=0 不截断"""
        body = "B" * 3000
        prompt = build_prompt(
            subject="test", sender="s", body=body,
            body_max_chars=0,
        )
        assert body in prompt

    def test_kb_context_injection(self):
        """知识库上下文应注入到 prompt 末尾"""
        kb_ctx = "## 相关法律法规参考（来自知识库）\n【民法典.md】\n第一百二十条..."
        prompt = build_prompt(
            subject="test", sender="s", body="body",
            kb_context=kb_ctx,
        )
        assert kb_ctx in prompt
        # kb_context 应在邮件内容之后
        assert prompt.index("body") < prompt.index("相关法律法规参考")

    def test_empty_kb_context_ignored(self):
        """空 kb_context 不影响输出"""
        prompt_no_kb = build_prompt(subject="a", sender="b", body="c")
        prompt_empty_kb = build_prompt(subject="a", sender="b", body="c", kb_context="")
        assert prompt_no_kb == prompt_empty_kb

    def test_custom_prompt_without_doc_types_placeholder(self):
        """自定义提示词不含 {doc_types} 时不报错"""
        prompt = build_prompt(
            subject="test", sender="s", body="body",
            custom_prompt="自定义模板: {subject} - {sender}",
        )
        assert "自定义模板" in prompt
        assert "test" in prompt

    def test_doc_types_injection(self):
        """默认提示词应包含全部文书类型"""
        prompt = build_prompt(
            subject="test", sender="s", body="body",
        )
        assert "合同协议" in prompt
        assert "起诉状" in prompt
        assert "非法律文书" in prompt


class TestDefaultPrompt:
    """默认提示词模板"""

    def test_default_prompt_exists(self):
        assert DEFAULT_ANALYSIS_PROMPT
        assert "doc_type" in DEFAULT_ANALYSIS_PROMPT
        assert "Role" in DEFAULT_ANALYSIS_PROMPT
        assert "资深律师" in DEFAULT_ANALYSIS_PROMPT
        # RGTO 结构
        assert "Goal" in DEFAULT_ANALYSIS_PROMPT
        assert "Task" in DEFAULT_ANALYSIS_PROMPT
        # 合同审查增强
        assert "审查条款完整性" in DEFAULT_ANALYSIS_PROMPT
        assert "对客户不利的条款" in DEFAULT_ANALYSIS_PROMPT
        assert "合同特别审查" in DEFAULT_ANALYSIS_PROMPT

    def test_all_required_fields_in_output_format(self):
        """输出格式包含所有必要字段"""
        required = [
            "doc_type", "case_summary", "ai_interpretation",
            "urgency", "key_date", "case_number",
            "involved_parties", "confidence"
        ]
        for field in required:
            assert field in DEFAULT_ANALYSIS_PROMPT, f"Missing field: {field}"
