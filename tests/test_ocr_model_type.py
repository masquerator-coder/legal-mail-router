"""
模型类型检测 — _classify_model_type / _VL_RE / _entry_vision_hint 纯逻辑测试

覆盖重点：oMLX 平台上 OCR/文档类多模态模型（DeepSeek-OCR-2、MinerU2.5、
PaddleOCR-VL 等）不应被误判为纯文本；探测全 unreadable 时应回退 unknown
而非 text。
"""
import pytest
from app.services.ocr import (
    _VL_RE,
    _entry_vision_hint,
    _classify_model_type,
)


def _probe(outcome, answered=None, prompt_tokens=None, raw=""):
    return {
        "outcome": outcome,
        "asked": "red",
        "answered": answered,
        "raw": raw,
        "prompt_tokens": prompt_tokens,
    }


class TestVLRegex:
    """oMLX 平台模型命名的视觉关键词命中"""

    @pytest.mark.parametrize("model_id", [
        "DeepSeek-OCR-2-bf16",
        "PaddleOCR-VL-1.5-bf16",
        "MinerU2.5-2509-1.2B-bf16",
        "Unlimited-OCR-MLX",
        "MarkItDown",
        "Qwen-OCR-7B",
        "gpt-ocr-mini",
    ])
    def test_vision_keywords_hit(self, model_id):
        assert _VL_RE.search(model_id.lower()), f"{model_id} 应命中视觉关键词"

    @pytest.mark.parametrize("model_id", [
        "Qwen3.6-27B-mxfp8",
        "Qwen3.6-35B-A3B-8bit",
        "Qwen2.5-0.5B-Instruct-8bit",
        "DeepSeek-V4-Flash-0731-MLX",
        "Qwen3-Embedding-8B-4bit-DWQ",
        "text-embedding-3-large",
    ])
    def test_pure_text_ids_miss(self, model_id):
        assert not _VL_RE.search(model_id.lower()), f"{model_id} 不应命中视觉关键词"


class TestEntryVisionHint:
    """模型条目视觉能力声明：oMLX 条目只有 id，靠名称兜底"""

    def test_entry_with_capabilities(self):
        entry = {"id": "some-model", "capabilities": ["image", "vision"]}
        assert _entry_vision_hint(entry) is True

    def test_entry_id_ocr_hint(self):
        entry = {"id": "DeepSeek-OCR-2-bf16"}
        assert _entry_vision_hint(entry) is True

    def test_entry_id_pure_text(self):
        entry = {"id": "Qwen3.6-27B-mxfp8"}
        assert _entry_vision_hint(entry) is False

    def test_entry_no_vision_fields(self):
        """oMLX /v1/models 只返回 id/object/created/owned_by/max_model_len"""
        entry = {"id": "MinerU2.5-2509-1.2B-bf16", "object": "model",
                 "created": 1786033880, "owned_by": "omlx", "max_model_len": 16384}
        assert _entry_vision_hint(entry) is True


class TestProbeStatusCodes:
    """HTTP 状态码 → outcome 映射（409 加载失败不得判 rejected）"""

    @staticmethod
    def _run(status_code, body):
        import asyncio
        import httpx
        from app.services.ocr import _probe_vision_once

        def handler(request):
            return httpx.Response(status_code, json={"error": {"message": body}})

        async def run():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                return await _probe_vision_once(
                    client, "http://x/v1/chat/completions",
                    {"Authorization": "Bearer k"}, "m", "red",
                )

        return asyncio.run(run())

    def test_400_with_reject_hint_is_rejected(self):
        r = self._run(400, "image_url is not supported by this model")
        assert r["outcome"] == "rejected"

    def test_400_without_hint_is_error(self):
        r = self._run(400, "invalid api key")
        assert r["outcome"] == "error"

    def test_422_with_reject_hint_is_rejected(self):
        r = self._run(422, "model does not support images")
        assert r["outcome"] == "rejected"

    def test_409_load_failed_is_error_not_rejected(self):
        """oMLX 模型加载失败 409 → error（服务端状态），不得判 rejected 钉死为纯文本"""
        r = self._run(409, "VLM load failed: Model type not supported")
        assert r["outcome"] == "error"

    def test_404_is_error(self):
        r = self._run(404, "not found")
        assert r["outcome"] == "error"

    def test_500_is_error(self):
        r = self._run(500, "internal error")
        assert r["outcome"] == "error"

    def test_409_load_failed_classified_as_unknown(self):
        """409 加载失败且无视觉信号 → unknown"""
        probes = [_probe("error", raw="HTTP 409: VLM load failed")]
        mt, _ = _classify_model_type(False, False, probes, 1, ["red"])
        assert mt == "unknown"

    def test_409_load_failed_with_name_hint_is_multimodal(self):
        """409 加载失败但名称含 OCR → 有视觉信号 → multimodal（提示运维）"""
        probes = [_probe("error", raw="HTTP 409: VLM load failed")]
        mt, detail = _classify_model_type(False, True, probes, 1, ["red"])
        assert mt == "multimodal"
        assert "建议运维" in detail


class TestClassifyModelType:
    """判定函数分支"""

    def test_rejected_is_text(self):
        mt, detail = _classify_model_type(False, False, [_probe("rejected")], 1, ["red"])
        assert mt == "text"

    def test_all_correct_distinct_is_multimodal(self):
        probes = [
            _probe("correct", answered="red"),
            _probe("correct", answered="green"),
        ]
        mt, _ = _classify_model_type(False, False, probes, 2, ["red", "green"])
        assert mt == "multimodal"

    def test_all_error_with_hint_is_multimodal(self):
        probes = [_probe("error"), _probe("error")]
        mt, _ = _classify_model_type(True, False, probes, 2, ["red", "green"])
        assert mt == "multimodal"

    def test_all_error_without_hint_is_unknown(self):
        probes = [_probe("error"), _probe("error")]
        mt, _ = _classify_model_type(False, False, probes, 2, ["red", "green"])
        assert mt == "unknown"

    def test_entry_vision_overrides_unreadable(self):
        """oMLX 静默丢图 + 元数据声明视觉 → multimodal（回归保护）"""
        probes = [_probe("unreadable"), _probe("unreadable")]
        mt, detail = _classify_model_type(True, False, probes, 2, ["red", "green"])
        assert mt == "multimodal"
        assert "已向请求塞入图片" in detail

    def test_all_unreadable_no_hint_is_unknown(self):
        """本次修复：全 unreadable 且无视觉信号 → unknown，而非 text"""
        probes = [_probe("unreadable"), _probe("unreadable")]
        mt, detail = _classify_model_type(False, False, probes, 2, ["red", "green"])
        assert mt == "unknown"
        assert "手动指定" in detail

    def test_wrong_no_hint_is_text(self):
        """真实答错颜色且无视觉信号 → text（后端接受图片但模型看不见）"""
        probes = [
            _probe("wrong", answered="blue"),
            _probe("wrong", answered="yellow"),
        ]
        mt, detail = _classify_model_type(False, False, probes, 2, ["red", "green"])
        assert mt == "text"
        assert "答错" in detail

    def test_mixed_correct_wrong_no_hint_is_text(self):
        """部分答对但含明确答错（瞎猜）→ text（有 wrong 即确证看不见）"""
        probes = [
            _probe("correct", answered="red"),
            _probe("wrong", answered="blue"),
        ]
        mt, _ = _classify_model_type(False, False, probes, 2, ["red", "green"])
        assert mt == "text"

    def test_mixed_correct_unreadable_no_hint_is_unknown(self):
        """无 wrong、但有 unreadable → unknown（证据不足，不谎报）"""
        probes = [
            _probe("correct", answered="red"),
            _probe("unreadable"),
        ]
        mt, _ = _classify_model_type(False, False, probes, 2, ["red", "green"])
        assert mt == "unknown"

    def test_name_hint_saves_all_error(self):
        probes = [_probe("error")]
        mt, _ = _classify_model_type(False, True, probes, 1, ["red"])
        assert mt == "multimodal"


class TestBaselineTokens:
    """无图基线 token 对比（远程并入的硬证据）"""

    def test_saw_image_but_token_equals_baseline_is_unknown(self):
        """全对但 token 与无图基线一致 → 盲猜，降级 unknown"""
        probes = [
            _probe("correct", answered="red", prompt_tokens=100),
            _probe("correct", answered="green", prompt_tokens=100),
        ]
        mt, detail = _classify_model_type(False, False, probes, 2, ["red", "green"],
                                          baseline_tokens=100)
        assert mt == "unknown"
        assert "盲猜" in detail

    def test_saw_image_token_above_baseline_is_multimodal(self):
        """全对且 token 高于基线 → 图片进入上下文，确证多模态"""
        probes = [
            _probe("correct", answered="red", prompt_tokens=428),
            _probe("correct", answered="green", prompt_tokens=428),
        ]
        mt, _ = _classify_model_type(False, False, probes, 2, ["red", "green"],
                                     baseline_tokens=100)
        assert mt == "multimodal"

    def test_unreadable_token_equals_baseline_is_unknown(self):
        """探测没看见且 token 与基线一致 → 后端静默丢弃图片的铁证"""
        probes = [
            _probe("unreadable", prompt_tokens=100),
            _probe("unreadable", prompt_tokens=100),
        ]
        mt, detail = _classify_model_type(False, False, probes, 2, ["red", "green"],
                                          baseline_tokens=100)
        assert mt == "unknown"
        assert "静默丢弃" in detail

    def test_unreadable_token_above_baseline_still_unknown(self):
        """token 高于基线但探测没确认 → 仍 unknown（图片可能进了但辨色不适配）"""
        probes = [
            _probe("unreadable", prompt_tokens=428),
            _probe("unreadable", prompt_tokens=428),
        ]
        mt, _ = _classify_model_type(False, False, probes, 2, ["red", "green"],
                                     baseline_tokens=100)
        assert mt == "unknown"

    def test_entry_vision_with_token_equals_baseline(self):
        """元数据声明视觉但 token 与基线一致 → multimodal + 佐证说明"""
        probes = [
            _probe("unreadable", prompt_tokens=100),
            _probe("unreadable", prompt_tokens=100),
        ]
        mt, detail = _classify_model_type(True, False, probes, 2, ["red", "green"],
                                          baseline_tokens=100)
        assert mt == "multimodal"
        assert "图片可能未真正进入上下文" in detail

    def test_baseline_none_wrong_is_text(self):
        """基线探测不可用且辨色答错 → text（wrong 本身即确证看不见）"""
        probes = [
            _probe("wrong", answered="blue"),
            _probe("wrong", answered="yellow"),
        ]
        mt, _ = _classify_model_type(False, False, probes, 2, ["red", "green"])
        assert mt == "text"
