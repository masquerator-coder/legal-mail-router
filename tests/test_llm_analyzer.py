"""
LLM 分析器 — prompt 构建 & 截断逻辑测试（两阶段分析）
"""
import pytest
from app.services import llm_analyzer as m
from app.services.llm_analyzer import (
    build_prompt,
    build_classify_prompt,
    _get_doc_types,
    _get_doc_analysis_prompt,
)


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

    def test_custom_prompt_without_placeholders(self):
        """自定义提示词不含已知占位符时不报错"""
        prompt = build_prompt(
            subject="test", sender="s", body="body",
            custom_prompt="自定义模板: {subject} - {sender}",
        )
        assert "自定义模板" in prompt
        assert "test" in prompt

    def test_default_template_has_no_doc_types(self):
        """默认模板不再内嵌文书类型识别步骤，改为占位符机制"""
        prompt = build_prompt(
            subject="test", sender="s", body="body",
        )
        assert "revised_document" in prompt
        assert "{analysis_instructions}" not in prompt  # 占位符已被渲染（为空）
        # 原「步骤一：文书识别」的分类指引已从主模板移除
        assert "从以下类型中选择最匹配" not in prompt

    def test_analysis_instructions_injection(self):
        """分析流程提示词应嵌入 {analysis_instructions} 占位符位置"""
        inst = _get_doc_analysis_prompt("合同协议")
        assert inst  # 提示词文件应存在
        prompt = build_prompt(
            subject="test", sender="s", body="body",
            analysis_instructions=inst,
        )
        assert "{analysis_instructions}" not in prompt
        assert "条款完整性" in prompt  # 合同分析流程已嵌入

    def test_analysis_instructions_today_replaced(self):
        """分析流程提示词中的 {today} 应被替换为真实日期"""
        inst = "截止日计算参考：{today}"
        prompt = build_prompt(
            subject="test", sender="s", body="body",
            analysis_instructions=inst,
            today_str="2026-01-01",
        )
        assert "{today}" not in prompt
        assert "2026-01-01" in prompt

    def test_analysis_instructions_with_braces(self):
        """分析流程提示词含 JSON 示例花括号时不被 format 破坏"""
        inst = '返回格式：{"doc_type": "类型"}'
        prompt = build_prompt(
            subject="test", sender="s", body="body",
            analysis_instructions=inst,
        )
        assert '{"doc_type": "类型"}' in prompt

    def test_custom_prompt_without_placeholder_appends(self):
        """自定义模板未含 {analysis_instructions} 占位符时，指令应追加到末尾而非丢失"""
        inst = _get_doc_analysis_prompt("合同协议")
        prompt = build_prompt(
            subject="test", sender="s", body="body",
            custom_prompt="自定义模板: {subject}",
            analysis_instructions=inst,
        )
        assert "自定义模板" in prompt
        assert "条款完整性" in prompt  # 指令已追加，未丢失


class TestDocTypesFile:
    """文书类型清单（文件驱动）"""

    def test_get_doc_types_contains_defaults(self):
        types = _get_doc_types()
        assert "合同协议" in types
        assert "起诉状" in types
        assert "其他法律文书" in types
        assert "非法律文书" in types

    def test_get_doc_types_no_duplicates(self):
        types = _get_doc_types()
        assert len(types) == len(set(types))

    def test_get_doc_analysis_prompt_contract(self):
        prompt = _get_doc_analysis_prompt("合同协议")
        assert "条款完整性" in prompt
        assert "修订版文书" in prompt

    def test_get_doc_analysis_prompt_fallback(self):
        """未知类型应回退到「其他法律文书」提示词"""
        prompt = _get_doc_analysis_prompt("不存在的类型XYZ")
        assert "其他法律文书" in prompt
        assert "案情摘要" in prompt

    def test_get_doc_analysis_prompt_empty_fallback(self):
        prompt = _get_doc_analysis_prompt("")
        assert "其他法律文书" in prompt


class TestClassifyPrompt:
    """第一阶段：文书类型识别 prompt"""

    def test_classify_prompt_contains_types(self):
        prompt = build_classify_prompt(
            subject="测试", sender="s@e.com", body="正文",
            attachment_texts="附: 采购合同",
        )
        assert "合同协议" in prompt
        assert "非法律文书" in prompt
        assert "doc_type" in prompt
        assert "附: 采购合同" in prompt

    def test_default_classify_prompt_template(self):
        """默认分类模板应含占位符，供 UI 填充默认模板使用"""
        template = m._get_default_classify_prompt()
        assert "{subject}" in template
        assert "{sender}" in template
        assert "{body}" in template
        assert "{doc_types}" in template
        assert "非法律文书" in template

    def test_classify_prompt_custom(self):
        """自定义分类提示词模板应生效"""
        prompt = build_classify_prompt(
            subject="测试", sender="s@e.com", body="正文",
            custom_prompt="自定义分类: {subject} | {sender} | {doc_types}",
        )
        assert "自定义分类" in prompt
        assert "合同协议" in prompt  # {doc_types} 已替换
        assert "你是法律文书类型识别专家" not in prompt  # 默认模板未被使用

    def test_classify_prompt_custom_with_braces(self):
        """自定义分类提示词含 JSON 示例花括号时不报错"""
        prompt = build_classify_prompt(
            subject="t", sender="s", body="b",
            custom_prompt='返回：{{"doc_type": "类型"}}，{subject}',
        )
        assert '{"doc_type": "类型"}' in prompt
        assert "t" in prompt


class TestTwoStage:
    """两阶段编排"""

    @pytest.mark.asyncio
    async def test_non_legal_short_circuit(self, monkeypatch):
        async def fake_classify(*args, **kwargs):
            return {"doc_type": "非法律文书", "confidence": 0.95}

        async def fake_analyze(*args, **kwargs):
            raise AssertionError("非法律文书不应进入第二阶段")

        monkeypatch.setattr(m, "classify_doc_type", fake_classify)
        monkeypatch.setattr(m, "analyze_email", fake_analyze)

        result = await m.analyze_email_two_stage(
            api_url="u", api_key_encrypted="k", model_name="m",
            subject="s", sender="f", body="b",
        )
        assert result["doc_type"] == "非法律文书"
        assert result["confidence"] == 0.95
        assert result["revised_document"] is None

    @pytest.mark.asyncio
    async def test_merges_stage1_type(self, monkeypatch):
        captured = {}

        async def fake_classify(*args, **kwargs):
            return {"doc_type": "合同协议", "confidence": 0.9}

        async def fake_analyze(*args, **kwargs):
            captured["analysis_instructions"] = kwargs.get("analysis_instructions", "")
            # 第二阶段返回不同类型，应被第一阶段覆盖
            return {
                "doc_type": "起诉状", "case_summary": "x", "ai_interpretation": "y",
                "urgency": "high", "key_date": None, "case_number": None,
                "involved_parties": "", "confidence": 0.5, "revised_document": None,
            }

        monkeypatch.setattr(m, "classify_doc_type", fake_classify)
        monkeypatch.setattr(m, "analyze_email", fake_analyze)

        result = await m.analyze_email_two_stage(
            api_url="u", api_key_encrypted="k", model_name="m",
            subject="s", sender="f", body="b",
        )
        assert result["doc_type"] == "合同协议"  # 以第一阶段为准
        assert result["confidence"] == 0.9
        # 第二阶段被调用时应传入该类型的分析流程提示词
        assert captured.get("analysis_instructions") and "条款完整性" in captured["analysis_instructions"]

    @pytest.mark.asyncio
    async def test_classify_fallback_uses_other(self, monkeypatch):
        async def fake_classify(*args, **kwargs):
            return {"doc_type": "其他法律文书", "confidence": 0.5}

        async def fake_analyze(*args, **kwargs):
            return {
                "doc_type": "其他法律文书", "case_summary": "x", "ai_interpretation": "y",
                "urgency": "medium", "key_date": None, "case_number": None,
                "involved_parties": "", "confidence": 0.5, "revised_document": None,
            }

        monkeypatch.setattr(m, "classify_doc_type", fake_classify)
        monkeypatch.setattr(m, "analyze_email", fake_analyze)

        result = await m.analyze_email_two_stage(
            api_url="u", api_key_encrypted="k", model_name="m",
            subject="s", sender="f", body="b",
        )
        assert result["doc_type"] == "其他法律文书"

    @pytest.mark.asyncio
    async def test_classifier_cfg_passed(self, monkeypatch):
        captured = {}

        async def fake_classify(*args, **kwargs):
            captured["args"] = (args, kwargs)
            return {"doc_type": "通知书", "confidence": 0.8}

        async def fake_analyze(*args, **kwargs):
            return {
                "doc_type": "通知书", "case_summary": "x", "ai_interpretation": "y",
                "urgency": "medium", "key_date": None, "case_number": None,
                "involved_parties": "", "confidence": 0.5, "revised_document": None,
            }

        monkeypatch.setattr(m, "classify_doc_type", fake_classify)
        monkeypatch.setattr(m, "analyze_email", fake_analyze)

        await m.analyze_email_two_stage(
            api_url="analyzer-url", api_key_encrypted="analyzer-key", model_name="analyzer-model",
            subject="s", sender="f", body="b",
            classifier_cfg={"api_url": "cls-url", "api_key_encrypted": "cls-key",
                            "model_name": "cls-model", "analysis_prompt": "自定义分类提示词"},
        )
        args, kwargs = captured["args"]
        assert kwargs["api_url"] == "cls-url"
        assert kwargs["model_name"] == "cls-model"
        assert kwargs["api_key_encrypted"] == "cls-key"
        assert kwargs["custom_prompt"] == "自定义分类提示词"


class TestEscapeJsonStringNewlines:
    """JSON 字符串内未转义换行的修复（原 PCRE \\K 正则在 Python 3.12+ 抛 re.error）"""

    def test_escapes_newlines_in_string_values(self):
        from app.services.llm_analyzer import _escape_json_string_newlines
        text = '{"doc_type": "合同", "revised_document": "第一行\n第二行"}'
        repaired = _escape_json_string_newlines(text)
        # 修复后应可被 json.loads 解析，且换行保留为 \n 字面转义
        import json
        parsed = json.loads(repaired)
        assert parsed["revised_document"] == "第一行\n第二行"

    def test_does_not_break_existing_escapes(self):
        from app.services.llm_analyzer import _escape_json_string_newlines
        text = '{"a": "已有转义 \\n 和 \\"引号\\"", "b": "x"}'
        repaired = _escape_json_string_newlines(text)
        import json
        parsed = json.loads(repaired)
        assert parsed["a"] == '已有转义 \n 和 "引号"'
        assert parsed["b"] == "x"

    def test_no_newline_no_change(self):
        from app.services.llm_analyzer import _escape_json_string_newlines
        text = '{"a": "simple", "b": 123}'
        assert _escape_json_string_newlines(text) == text


class TestGroupPrompt:
    """分组分析（多文书分组）prompt"""

    def test_default_group_prompt_template(self):
        """默认分组模板应含占位符，供 UI 填充默认模板使用"""
        template = m._get_default_group_prompt()
        assert "{subject}" in template
        assert "{file_list}" in template
        assert "groups" in template

    def test_build_group_prompt_default(self):
        prompt = m.build_group_prompt(subject="测试邮件", file_list="0. a.pdf\n   内容预览: xx")
        assert "测试邮件" in prompt
        assert "0. a.pdf" in prompt
        assert '"groups": [[indices...], ...]' in prompt

    def test_build_group_prompt_custom(self):
        prompt = m.build_group_prompt(
            subject="t", file_list="0. a.pdf",
            custom_prompt="自定义分组: {subject} / {file_list}",
        )
        assert "自定义分组" in prompt
        assert "t" in prompt
        assert "0. a.pdf" in prompt
        assert "你是法律文档分类助手" not in prompt

    def test_build_group_prompt_custom_without_file_list(self):
        """自定义模板未含 {file_list} 时，附件列表应追加到末尾"""
        prompt = m.build_group_prompt(
            subject="t", file_list="0. a.pdf",
            custom_prompt="自定义分组仅主题: {subject}",
        )
        assert "自定义分组仅主题" in prompt
        assert "0. a.pdf" in prompt  # 已追加


@pytest.fixture
def role_db():
    """独立内存库：角色读取测试（不触碰真实 data 目录）"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models import LLMConfig, DefaultConfig  # noqa: F401  # 注册模型

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    yield db
    db.close()
    engine.dispose()


class TestRoleLlmCfg:
    """模型角色读取与自动回退"""

    def test_no_config_returns_none(self, role_db):
        from app.services.scheduler import _get_role_llm_cfg
        assert _get_role_llm_cfg(role_db, "analyzer") is None
        assert _get_role_llm_cfg(role_db, "classifier") is None
        assert _get_role_llm_cfg(role_db, "group") is None

    def test_single_config_all_roles_fallback(self, role_db):
        from app.models import LLMConfig
        from app.services.scheduler import _get_role_llm_cfg
        role_db.add(LLMConfig(name="唯一模型", api_url="u", api_key_encrypted="k",
                              model_name="m", is_active=True))
        role_db.commit()
        assert _get_role_llm_cfg(role_db, "analyzer").name == "唯一模型"
        assert _get_role_llm_cfg(role_db, "classifier").name == "唯一模型"
        assert _get_role_llm_cfg(role_db, "group").name == "唯一模型"

    def test_explicit_role_assignment(self, role_db):
        from app.models import LLMConfig, DefaultConfig
        from app.services.scheduler import _get_role_llm_cfg
        a = LLMConfig(name="A", api_url="u", api_key_encrypted="k", model_name="m", is_active=True)
        c = LLMConfig(name="C", api_url="u", api_key_encrypted="k", model_name="m", is_active=True)
        role_db.add_all([a, c])
        role_db.commit()
        role_db.add(DefaultConfig(key="llm_role_classifier", value=str(c.id)))
        role_db.add(DefaultConfig(key="llm_role_group", value=str(a.id)))
        role_db.commit()
        assert _get_role_llm_cfg(role_db, "classifier").name == "C"
        assert _get_role_llm_cfg(role_db, "group").name == "A"
        assert _get_role_llm_cfg(role_db, "analyzer").name == "A"  # 未配置→第一个激活

    def test_legacy_config_role_inference(self, role_db):
        """未配置角色时，按旧 config_role 字段推断"""
        from app.models import LLMConfig
        from app.services.scheduler import _get_role_llm_cfg
        a = LLMConfig(name="旧分析", api_url="u", api_key_encrypted="k",
                      model_name="m", is_active=True, config_role="analyzer")
        c = LLMConfig(name="旧分类", api_url="u", api_key_encrypted="k",
                      model_name="m", is_active=True, config_role="classifier")
        role_db.add_all([a, c])
        role_db.commit()
        assert _get_role_llm_cfg(role_db, "classifier").name == "旧分类"
        assert _get_role_llm_cfg(role_db, "analyzer").name == "旧分析"

    def test_invalid_role_id_falls_back(self, role_db):
        from app.models import LLMConfig, DefaultConfig
        from app.services.scheduler import _get_role_llm_cfg
        a = LLMConfig(name="A", api_url="u", api_key_encrypted="k", model_name="m", is_active=True)
        role_db.add(a)
        role_db.commit()
        role_db.add(DefaultConfig(key="llm_role_classifier", value="99999"))  # 不存在的ID
        role_db.commit()
        assert _get_role_llm_cfg(role_db, "classifier").name == "A"  # 回退 analyzer
