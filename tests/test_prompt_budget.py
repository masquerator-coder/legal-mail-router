"""
附件文本提取上限 — 上下文窗口自动匹配测试。

验证 compute_attachment_char_budget 由预算公式反推字符上限，
并与 truncate_prompt_parts 配合时不会让单附件撑爆输入预算。
"""
import pytest

from app.services.prompt_budget import (
    compute_attachment_char_budget,
    compute_body_and_attachment_char_budget,
    estimate_tokens,
    truncate_prompt_parts,
)


class TestComputeAttachmentCharBudget:
    def test_1m_large_window_capped(self):
        """1M 上下文 → 应对 cap 封顶（默认 50 万字符）"""
        budget = compute_attachment_char_budget(1_048_576)
        assert budget == 500_000

    def test_1m_no_cap(self):
        """1M 上下文且不封顶 → 由预算公式推导（约 77 万字符）"""
        budget = compute_attachment_char_budget(1_048_576, cap=None)
        # (int(1_048_576*0.5) - 2000 - 预留) * 1.5 ≈ 77 万
        assert 700_000 < budget < 900_000

    def test_128k_window(self):
        """128K 上下文 → 约 8.5 万字符/附件"""
        budget = compute_attachment_char_budget(131_072)
        assert 80_000 <= budget <= 90_000

    def test_200k_window(self):
        budget = compute_attachment_char_budget(200_000)
        assert 120_000 <= budget <= 150_000

    def test_unknown_window_legacy_default(self):
        """窗口未知（<=0）→ 回退历史硬编码 6000，保持旧行为"""
        assert compute_attachment_char_budget(0) == 6000
        assert compute_attachment_char_budget(-1, legacy_default=5000) == 5000

    def test_tiny_window_floor(self):
        """极小窗口 → 至少 1024 字符下限，避免裁到 0"""
        budget = compute_attachment_char_budget(4096)
        assert budget >= 1024

    def test_monotonic_with_window(self):
        """窗口越大字符上限越大（单调不减）"""
        small = compute_attachment_char_budget(131_072)
        large = compute_attachment_char_budget(1_048_576)
        assert large >= small

    def test_custom_ratio_affects_budget(self):
        """usage_ratio 影响可用预算"""
        low = compute_attachment_char_budget(131_072, usage_ratio=0.30)
        high = compute_attachment_char_budget(131_072, usage_ratio=0.90)
        assert high > low


class TestBudgetWithTruncation:
    def test_large_attachment_fits_1m(self):
        """1M 窗口下一封大附件文本（比如 10 万字符）无需截断即可装入预算"""
        template = "你是法律文书分析专家。\n## Input\n{body}\n## Output\njson"
        body = "邮件正文。" * 500  # ~2500 字符
        att_text = "合同条款。" * 20_000  # ~10 万字符
        # 用 compute 反推的上限作为提取上限
        cap = compute_attachment_char_budget(1_048_576)
        att_text_capped = att_text[:cap]

        parts = truncate_prompt_parts(
            {"template_with_body": template.format(body=body), "attachment_texts": att_text_capped},
            1_048_576, 0.50, 2000, "approximate",
        )
        # 1M 预算足够容纳，附件不应被截掉
        assert "合同条款" in parts["attachment_texts"]

    def test_small_window_drops_attachment(self):
        """小窗口下附件超预算应被截断（L1 附件丢弃），模板保留"""
        template = "你是法律文书分析专家。\n## Input\n{body}\n## Output\njson"
        body = "正文。"
        huge_att = "条款。" * 5_000  # ~1.5 万字符
        parts = truncate_prompt_parts(
            {"template_with_body": template.format(body=body), "attachment_texts": huge_att},
            8192, 0.50, 100, "approximate",
        )
        # 8K 窗口预算 ~4K token，装不下 1.5 万字符附件 + 模板 → 附件被丢弃
        assert parts["attachment_texts"] == ""
        assert "doc_type" not in parts["template_with_body"]  # 模板结构仍在（含 Output marker）
        assert "## Output" in parts["template_with_body"]


class TestBodyAndAttachmentBudget:
    """协同推导：正文 + 附件共享预算池"""

    def test_1m_body_gets_larger_limit(self):
        """1M 窗口下正文上限应远超旧固定值 8000，附件 cap 封顶"""
        body, attach = compute_body_and_attachment_char_budget(1_048_576)
        assert body > 50_000          # 大窗口下正文吃到合理上限（>>> 8000）
        assert attach == 500_000      # 附件 cap 封顶
        assert body < attach          # 正文（辅助）上限应小于附件（主体）

    def test_128k_body_and_attach(self):
        body, attach = compute_body_and_attachment_char_budget(131_072)
        assert 10_000 <= body <= 20_000
        assert 60_000 <= attach <= 100_000
        assert body < attach

    def test_legacy_when_window_unknown(self):
        """窗口未知 → 回退历史固定值 (8000, 6000)"""
        assert compute_body_and_attachment_char_budget(0) == (8000, 6000)

    def test_sum_within_budget(self):
        """正文 token + 附件 token + 预留 不超过输入预算（不超窗）"""
        cw, ratio, out = 131_072, 0.50, 2000
        template_tokens = 1500
        body_chars, attach_chars = compute_body_and_attachment_char_budget(
            cw, usage_ratio=ratio, output_tokens=out, template_tokens=template_tokens,
        )
        input_budget = int(cw * ratio) - out
        body_tok = body_chars / 1.5
        attach_tok = attach_chars / 1.5
        # 蓝本：正文(15%) + 附件(85%) = 100% 可用，预留(模板+盐)未计入字符侧
        assert (body_tok + attach_tok + template_tokens + 500) <= input_budget

    def test_body_ratio_reallocates(self):
        """body_ratio 越小，附件分得越多；总和基本不变（int 舍入容差 ≤2 字符）"""
        b_high, a_high = compute_body_and_attachment_char_budget(131_072, body_ratio=0.30)
        b_low, a_low = compute_body_and_attachment_char_budget(131_072, body_ratio=0.05)
        assert b_low < b_high
        assert a_low > a_high
        assert abs((b_low + a_low) - (b_high + a_high)) <= 2
