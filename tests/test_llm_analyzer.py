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

    def test_get_doc_types_contains_new_admin_types(self):
        """新增类型：政府信息公开 / 信访件 / 履职申请 / 咨询 / 投诉举报"""
        types = _get_doc_types()
        assert "政府信息公开" in types
        assert "信访件" in types
        assert "履职申请" in types
        assert "咨询" in types
        assert "投诉举报" in types

    def test_get_doc_analysis_prompt_contract(self):
        prompt = _get_doc_analysis_prompt("合同协议")
        assert "条款完整性" in prompt
        assert "修订版文书" in prompt

    def test_new_types_have_dedicated_prompts(self):
        """新类型须有专属提示词，不得回退「其他法律文书」兜底"""
        for name, keyword in (("政府信息公开", "20 个工作日"), ("信访件", "60 日"),
                              ("履职申请", "两个月"), ("咨询", "12345"),
                              ("投诉举报", "投诉")):
            prompt = _get_doc_analysis_prompt(name)
            assert prompt, f"{name} 提示词为空"
            assert "专属分析流程(兜底)" not in prompt, f"{name} 回退到了兜底提示词"
            assert name in prompt
            assert keyword in prompt
            assert "{today}" in prompt  # 期限计算须以当前日期为基准

    def test_admin_entry_prompts_do_not_fall_back_to_each_other(self):
        """五个入口各有独立提示词，内容互不相同（拆分为独立类型的回归保护）"""
        names = ("政府信息公开", "履职申请", "信访件", "咨询", "投诉举报")
        prompts = {n: _get_doc_analysis_prompt(n) for n in names}
        for n, p in prompts.items():
            assert p.strip(), f"{n} 提示词为空"
        assert len({p for p in prompts.values()}) == len(names), "存在重复的提示词内容"

    def test_new_types_pass_classify_validation(self):
        """类型识别输出新类型时不得被回退为「其他法律文书」"""
        for name in ("政府信息公开", "履职申请", "信访件", "咨询", "投诉举报"):
            r = m._validate_classify_output({"doc_type": name, "confidence": 0.9})
            assert r["doc_type"] == name

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


class TestMcpToolLoopFallback:
    """MCP 工具循环不收敛时，应优雅回退为不使用工具的普通分析（不整篇失败）。"""

    def _tool_call_reply(self):
        return {"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "search_article", "arguments": "{}"}}]}}]}

    def _json_reply(self, content):
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    class _FakeResp:
        def __init__(self, data):
            self.data = data
        def raise_for_status(self):
            return None
        def json(self):
            return self.data

    class _FakeMcp:
        def __init__(self, servers):
            from types import SimpleNamespace
            self.tools = [SimpleNamespace(name="search_article", description="d",
                                          input_schema={}, server="s1")]
            self.connected_servers = ["s1"]
            self.citation_links = []
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        def has_tools(self):
            return True
        def list_openai_tools(self):
            return [{"type": "function", "function": {"name": "search_article", "parameters": {}}}]
        async def call(self, name, args):
            return "法规检索结果"

    @pytest.mark.asyncio
    async def test_tool_loop_nonconvergence_falls_back(self, monkeypatch):
        # 前两轮模型只回 tool_calls（不收敛），第三轮（回退的普通分析）返回有效 JSON
        replies = [
            self._tool_call_reply(),
            self._tool_call_reply(),
            self._json_reply('{"doc_type": "合同协议", "case_summary": "s", '
                             '"ai_interpretation": "a", "urgency": "high", "confidence": 0.9}'),
        ]
        state = {"i": 0}

        class _FakeClient:
            def __init__(self, *a, **k):
                self._closed = False
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                self._closed = True
                return None
            async def post(self, *a, **k):
                # 若回退块被错误地放到 async with 块外，client 已关闭，此处应抛错（与生产一致）
                if self._closed:
                    raise RuntimeError("Cannot send a request, as the client has been closed.")
                data = replies[min(state["i"], len(replies) - 1)]
                state["i"] += 1
                return TestMcpToolLoopFallback._FakeResp(data)

        monkeypatch.setattr(m, "decrypt", lambda e: "test-key")
        monkeypatch.setattr(m, "httpx", type("HH", (), {"AsyncClient": _FakeClient}))
        from app.services import mcp_client as mcp_mod
        monkeypatch.setattr(mcp_mod, "MCPClient", self._FakeMcp)

        result = await m.analyze_email(
            api_url="http://llm/v1", api_key_encrypted="enc", model_name="m",
            subject="主题", sender="发件人", body="正文",
            mcp_servers=[{"name": "s1", "url": "http://mcp", "headers": {}}],
            mcp_max_turns=1,  # max_rounds=2 → 两轮 tool_calls 后触发回退
        )
        # 回退结果应正常返回，且共发出 3 次请求（2 轮工具 + 1 次普通分析）
        assert result["doc_type"] == "合同协议"
        assert state["i"] == 3


class TestClassifyMaxTokens:
    """分类请求的输出预算下限：推理型模型推理过程计入 max_tokens，预算过小会截断 JSON 输出"""

    class _FakeResp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self.data

    def _install_fake_client(self, monkeypatch, captured, content='{"doc_type": "信访件", "confidence": 0.9}',
                             finish_reason="stop"):
        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def post(self, *a, **k):
                captured["payload"] = k.get("json")
                data = {"choices": [{"message": {"role": "assistant", "content": content},
                                     "finish_reason": finish_reason}]}
                return TestClassifyMaxTokens._FakeResp(data)

        monkeypatch.setattr(m, "decrypt", lambda e: "test-key")
        monkeypatch.setattr(m, "httpx", type("HH", (), {"AsyncClient": _FakeClient}))

    @pytest.mark.asyncio
    async def test_budget_floor_applied_when_unset(self, monkeypatch):
        """未指定 max_tokens 时应使用下限，而不是历史的 100"""
        captured = {}
        self._install_fake_client(monkeypatch, captured)

        result = await m.classify_doc_type(
            api_url="http://llm/v1", api_key_encrypted="enc", model_name="m",
            subject="s", sender="f", body="b",
        )
        assert captured["payload"]["max_tokens"] == m._CLASSIFY_MIN_MAX_TOKENS
        assert captured["payload"]["max_tokens"] > 100
        assert result["doc_type"] == "信访件"
        assert not result.get("failed")

    @pytest.mark.asyncio
    async def test_small_configured_value_raised_to_floor(self, monkeypatch):
        """配置里给了过小的值（100/0/负数）时必须抬到下限，否则推理会吃光预算"""
        for small in (100, 0, -5, None):
            captured = {}
            self._install_fake_client(monkeypatch, captured)
            await m.classify_doc_type(
                api_url="http://llm/v1", api_key_encrypted="enc", model_name="m",
                subject="s", sender="f", body="b", max_tokens=small,
            )
            assert captured["payload"]["max_tokens"] == m._CLASSIFY_MIN_MAX_TOKENS

    @pytest.mark.asyncio
    async def test_configured_larger_value_respected(self, monkeypatch):
        """配置值更大时应尊重配置，不被下限压低"""
        captured = {}
        self._install_fake_client(monkeypatch, captured)
        await m.classify_doc_type(
            api_url="http://llm/v1", api_key_encrypted="enc", model_name="m",
            subject="s", sender="f", body="b", max_tokens=4096,
        )
        assert captured["payload"]["max_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_parse_failure_marks_failed(self, monkeypatch):
        """解析失败时兜底值必须带 failed 标记，以便与模型真实判断区分"""
        captured = {}
        # 复现生产故障：模型只输出了推理过程，没有 JSON
        self._install_fake_client(
            monkeypatch, captured,
            content='我们根据邮件内容判断：邮件主题是"回复：信访件基本情况登记表"，属于"通知书"类文书。',
            finish_reason="length",
        )
        result = await m.classify_doc_type(
            api_url="http://llm/v1", api_key_encrypted="enc", model_name="m",
            subject="s", sender="f", body="b",
        )
        assert result["doc_type"] == "其他法律文书"
        assert result["confidence"] == 0.5
        assert result["failed"] is True

    @pytest.mark.asyncio
    async def test_request_error_marks_failed(self, monkeypatch):
        """请求层异常时同样带 failed 标记"""
        monkeypatch.setattr(m, "decrypt", lambda e: "test-key")

        class _Boom:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def post(self, *a, **k):
                raise RuntimeError("connection reset")

        monkeypatch.setattr(m, "httpx", type("HH", (), {"AsyncClient": _Boom}))
        result = await m.classify_doc_type(
            api_url="http://llm/v1", api_key_encrypted="enc", model_name="m",
            subject="s", sender="f", body="b",
        )
        assert result["failed"] is True

    @pytest.mark.asyncio
    async def test_model_chosen_other_legal_is_not_marked_failed(self, monkeypatch):
        """模型主动判断为「其他法律文书」时不得带 failed（否则会误判为识别失败）"""
        captured = {}
        self._install_fake_client(
            monkeypatch, captured,
            content='{"doc_type": "其他法律文书", "confidence": 0.85}',
        )
        result = await m.classify_doc_type(
            api_url="http://llm/v1", api_key_encrypted="enc", model_name="m",
            subject="s", sender="f", body="b",
        )
        assert result["doc_type"] == "其他法律文书"
        assert result["confidence"] == 0.85
        assert not result.get("failed")


class TestClassifyDisambiguation:
    """默认分类模板的易混淆类型消歧规则"""

    def test_template_has_disambiguation_rules(self):
        template = m._get_default_classify_prompt()
        # 信访事项答复类文书必须明确归「信访件」而非「通知书」
        assert "信访件" in template
        assert "政府信息公开" in template
        assert "通知书" in template
        assert "访答" in template  # 信访专用文号特征

    def test_template_covers_all_admin_entry_types(self):
        """行政程序五个入口均须有消歧规则（否则新类型易被误判为通知书）"""
        template = m._get_default_classify_prompt()
        for name in ("政府信息公开", "履职申请", "信访件", "咨询", "投诉举报"):
            assert f"- {name}：" in template, f"{name} 缺少边界规则"

    def test_petition_reply_maps_to_petition_type(self):
        """信访类文书的边界规则应排在「通知书」规则之前，先入为主地引导模型"""
        prompt = build_classify_prompt(
            subject="回复：信访件基本情况登记表 -王庆坤", sender="s@e.com",
            body="信访事项处理意见书",
        )
        rules = prompt.split("易混淆类型的区分规则")[1]
        # 逐条规则的位置比较（引导句中提及的「通知书」不算规则）
        petition_pos = rules.index("- 信访件：")
        notice_pos = rules.index("- 通知书：")
        assert petition_pos < notice_pos
        # 引导句不得把「通知书」作为示例类型抛出（否则反而强化该词）
        assert "等字样就归入通知书" not in rules


class TestTwoStageClassifyFailure:
    """第一阶段识别失败时，失败标记必须传到调用方"""

    @pytest.mark.asyncio
    async def test_flag_propagates_to_result(self, monkeypatch):
        async def fake_classify(*args, **kwargs):
            return {"doc_type": "其他法律文书", "confidence": 0.5, "failed": True}

        async def fake_analyze(*args, **kwargs):
            return {
                "doc_type": "通知书", "case_summary": "x", "ai_interpretation": "y",
                "urgency": "medium", "key_date": None, "case_number": None,
                "involved_parties": "", "confidence": 0.5, "revised_document": None,
            }

        monkeypatch.setattr(m, "classify_doc_type", fake_classify)
        monkeypatch.setattr(m, "analyze_email", fake_analyze)

        result = await m.analyze_email_two_stage(
            api_url="u", api_key_encrypted="k", model_name="m",
            subject="s", sender="f", body="b",
        )
        assert result["classify_failed"] is True
        # 第二阶段给出的类型不得覆盖兜底值（它不具备独立识别意义）
        assert result["doc_type"] == "其他法律文书"

    @pytest.mark.asyncio
    async def test_normal_classify_has_no_failure_flag(self, monkeypatch):
        async def fake_classify(*args, **kwargs):
            return {"doc_type": "信访件", "confidence": 0.9}

        async def fake_analyze(*args, **kwargs):
            return {
                "doc_type": "信访件", "case_summary": "x", "ai_interpretation": "y",
                "urgency": "medium", "key_date": None, "case_number": None,
                "involved_parties": "", "confidence": 0.5, "revised_document": None,
            }

        monkeypatch.setattr(m, "classify_doc_type", fake_classify)
        monkeypatch.setattr(m, "analyze_email", fake_analyze)

        result = await m.analyze_email_two_stage(
            api_url="u", api_key_encrypted="k", model_name="m",
            subject="s", sender="f", body="b",
        )
        assert result["classify_failed"] is False

    @pytest.mark.asyncio
    async def test_classifier_max_tokens_forwarded(self, monkeypatch):
        """classifier_max_tokens 应传给分类阶段"""
        captured = {}

        async def fake_classify(*args, **kwargs):
            captured.update(kwargs)
            return {"doc_type": "信访件", "confidence": 0.9}

        async def fake_analyze(*args, **kwargs):
            return {
                "doc_type": "信访件", "case_summary": "x", "ai_interpretation": "y",
                "urgency": "medium", "key_date": None, "case_number": None,
                "involved_parties": "", "confidence": 0.5, "revised_document": None,
            }

        monkeypatch.setattr(m, "classify_doc_type", fake_classify)
        monkeypatch.setattr(m, "analyze_email", fake_analyze)

        await m.analyze_email_two_stage(
            api_url="u", api_key_encrypted="k", model_name="m",
            subject="s", sender="f", body="b", classifier_max_tokens=2048,
        )
        assert captured["max_tokens"] == 2048


class TestDocTypeCapabilities:
    """类型能力标记解析（文书类型.conf 是唯一事实来源）"""

    def test_parse_plain_type(self):
        """无标记的行 → 类型名 + 空能力集合"""
        assert m._parse_doc_type_line("判决书") == ("判决书", frozenset())

    def test_parse_single_marker(self):
        name, caps = m._parse_doc_type_line("合同协议  #修订")
        assert name == "合同协议"
        assert caps == frozenset({m.CAP_REVISION})

    def test_parse_multiple_markers(self):
        name, caps = m._parse_doc_type_line("合同协议   #修订 #审查 #合同")
        assert name == "合同协议"
        assert caps == frozenset({m.CAP_REVISION, m.CAP_REVIEW, m.CAP_CONTRACT})

    def test_parse_marker_without_space(self):
        """# 紧跟类型名也应识别"""
        name, caps = m._parse_doc_type_line("合同协议#修订")
        assert name == "合同协议"
        assert caps == frozenset({m.CAP_REVISION})

    def test_parse_comment_line(self):
        """整行注释 → 空类型"""
        assert m._parse_doc_type_line("# 这是注释 #修订") == ("", frozenset())

    def test_parse_blank_line(self):
        assert m._parse_doc_type_line("   ") == ("", frozenset())
        assert m._parse_doc_type_line("") == ("", frozenset())

    def test_parse_unknown_marker_ignored(self):
        """未识别的标记被忽略，但类型名仍保留（笔误不导致类型丢失）"""
        name, caps = m._parse_doc_type_line("起诉状  #修订 #拼错的标记")
        assert name == "起诉状"
        assert caps == frozenset({m.CAP_REVISION})

    def test_unknown_doc_type_has_no_capabilities(self):
        """不在 conf 中的类型 → 无任何能力（保守：不生成修改版与审查意见）"""
        assert m.get_doc_type_capabilities("不存在的类型") == frozenset()
        assert m.get_doc_type_capabilities(None) == frozenset()
        assert m.get_doc_type_capabilities("") == frozenset()

    def test_fallback_types_always_present(self):
        """兜底类型无论 conf 是否包含都会补齐"""
        types = m._get_doc_types()
        for fb in m._FALLBACK_DOC_TYPES:
            assert fb in types
        assert "其他法律文书" in types,"兜底类型应始终可用"


class TestRealConfCapabilities:
    """针对当前仓库 文书类型.conf 的回归（防止新增类型漏配标记）"""

    def test_contract_is_revisable_and_contract_review(self):
        assert m.should_generate_revision("合同协议") is True
        assert m.should_generate_review("合同协议") is True
        assert m.is_contract_type("合同协议") is True

    def test_court_documents_not_revisable(self):
        """法院出具的裁判文书/程序性告知不修订、不出审查意见"""
        for t in ("判决书", "裁定书", "传票"):
            assert m.should_generate_revision(t) is False, t
            assert m.should_generate_review(t) is False, t

    def test_newly_added_types_are_revisable(self):
        """行政程序五个入口：此前因硬编码白名单遗漏而丢失修改版（本次修复点）"""
        for t in ("政府信息公开", "履职申请", "信访件", "咨询", "投诉举报"):
            assert m.should_generate_revision(t) is True, t
            assert m.should_generate_review(t) is True, t

    def test_non_contract_review_uses_civil_template(self):
        """非合同类型有 #审查 但无 #合同 → 用律师审查意见模板"""
        for t in ("起诉状", "律师函", "政府信息公开", "履职申请", "信访件",
                  "咨询", "投诉举报", "其他法律文书", "通知书"):
            assert m.should_generate_review(t) is True, t
            assert m.is_contract_type(t) is False, t

    def test_no_hardcoded_doc_type_list_in_scheduler(self):
        """scheduler 不应再维护文书类型清单（回归保护）"""
        import app.services.scheduler as sched
        assert not hasattr(sched, "REVISION_CANDIDATE_TYPES")
        assert not hasattr(sched, "_should_generate_revision")


class TestRevisionSectionByType:
    """主模板修订章节按类型能力渲染"""

    def test_revisable_type_gets_marking_spec(self):
        sec = m._build_revision_section("合同协议")
        assert "生成修改版文书" in sec
        assert "【新增】" in sec and "【/新增】" in sec

    def test_non_revisable_type_gets_null_instruction(self):
        for t in ("判决书", "裁定书", "传票", "证据材料"):
            sec = m._build_revision_section(t)
            assert "不生成修改版文书" in sec, t
            assert "null" in sec, t
            assert "【新增】" not in sec, t

    def test_none_doc_type_is_non_revisable(self):
        sec = m._build_revision_section(None)
        assert "不生成修改版文书" in sec

    def test_build_prompt_renders_revision_field_desc(self):
        """Output schema 的 revised_document 说明随类型变化，且无占位符残留"""
        p_rev = build_prompt("s", "a@b.c", "body", doc_type="合同协议")
        p_none = build_prompt("s", "a@b.c", "body", doc_type="判决书")
        assert "{revision_field_desc}" not in p_rev
        assert "{revision_field_desc}" not in p_none
        assert "{revision_instructions}" not in p_rev
        assert "{revision_instructions}" not in p_none
        assert "固定为null（本类型无需修订）" in p_none
        assert "固定为null（本类型无需修订）" not in p_rev

    def test_build_prompt_backward_compatible_without_doc_type(self):
        """不传 doc_type 时按「不可修订」渲染（保守），不抛异常"""
        prompt = build_prompt("s", "a@b.c", "body")
        assert "不生成修改版文书" in prompt

    def test_custom_prompt_without_revision_placeholder_gets_appended(self):
        """自定义模板未含 {revision_instructions} 时，修订要求追加到末尾"""
        prompt = build_prompt(
            "s", "a@b.c", "body",
            custom_prompt="自定义模板 {subject} {body}",
            doc_type="合同协议",
        )
        assert "自定义模板" in prompt
        assert "生成修改版文书" in prompt

    def test_classify_prompt_unaffected_by_markers(self):
        """类型识别候选清单：标记不应进入 {doc_types} 列表"""
        prompt = build_classify_prompt(subject="s", sender="a@b.c", body="b")
        assert "#修订" not in prompt
        assert "#审查" not in prompt
        for t in ("合同协议", "判决书", "政府信息公开", "信访件"):
            assert t in prompt


class TestTwoStageForwardsDocType:
    """回归：第二阶段必须收到第一阶段识别出的 doc_type（否则提示词无法按类型渲染）"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("doc_type,expect_revision", [
        ("合同协议", True),
        ("政府信息公开", True),   # 此前因硬编码白名单遗漏
        ("信访件", True),         # 此前因硬编码白名单遗漏
        ("判决书", False),
    ])
    async def test_doc_type_reaches_analyze_email(self, monkeypatch,
                                                  doc_type, expect_revision):
        captured = {}

        async def fake_classify(*args, **kwargs):
            return {"doc_type": doc_type, "confidence": 0.9}

        async def fake_analyze(*args, **kwargs):
            captured["doc_type"] = kwargs.get("doc_type")
            return {
                "doc_type": doc_type, "case_summary": "x", "ai_interpretation": "y",
                "urgency": "medium", "key_date": None, "case_number": None,
                "involved_parties": "", "confidence": 0.5, "revised_document": None,
            }

        monkeypatch.setattr(m, "classify_doc_type", fake_classify)
        monkeypatch.setattr(m, "analyze_email", fake_analyze)

        await m.analyze_email_two_stage(
            api_url="u", api_key_encrypted="k", model_name="m",
            subject="s", sender="f", body="b",
        )
        assert captured["doc_type"] == doc_type
        assert m.should_generate_revision(captured["doc_type"]) is expect_revision
