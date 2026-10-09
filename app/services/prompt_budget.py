"""
Prompt token budget estimation and priority-based truncation.

Provides pre-submission length checking against the LLM context window,
with priority-based truncation when the assembled prompt exceeds the budget.

Priority (lowest first, cut first):
  Attachment texts (at attachment boundaries) -> Email body (from end) -> Prompt template (never)

Usage:
  parts = {
      "template_with_body": str,   # prompt template with {body} substituted
      "attachment_texts": str,     # attachment text (without "## 附件内容" prefix)
  }

  budget = int(context_window * usage_ratio) - output_tokens
  parts = truncate_prompt_parts(parts, context_window, usage_ratio, output_tokens)
"""

import logging

logger = logging.getLogger(__name__)

# 中文字符/token 的标定系数（保守下限）。
# 取值依据（用仓库内真实中文语料 10000 字实测，tiktoken）：
#   approximate 旧系数 1.5  → 5383 token（1.86 字符/token）
#   cl100k_base            → 9824 token（0.87 字符/token）
#   o200k_base             → 7255 token（1.38 字符/token）
# 旧系数把中文 token 数低估约 1.83 倍，导致「按估算远未超预算」的请求实际超出
# 模型输入窗口（表现为 400 或被静默截断）。此系数用于把 token 预算换算成字符
# 上限（以及反向估算），宁可高估 token 也不能低估 → 取 cl100k 实测下限。
# 若后端改用对中文更省 token 的分词器（如 o200k），可上调至 1.3 左右。
CHARS_PER_TOKEN = 0.85


def estimate_tokens(text: str, method: str = "approximate") -> int:
    """Estimate token count for a text string.

    method:
      - "approximate": character-based (中文按 CHARS_PER_TOKEN 字符/token，
        英文按 4 字符/token)
      - "tiktoken": use tiktoken library if available (auto-select best encoder)

    注意：approximate 的取值刻意保守（宁高勿低），避免低估 token 数导致超窗。
    """
    if not text:
        return 0

    if method == "tiktoken":
        try:
            import tiktoken
            for enc_name in ("cl100k_base", "o200k_base", "p50k_base", "r50k_base"):
                try:
                    enc = tiktoken.get_encoding(enc_name)
                    return len(enc.encode(text, disallowed_special=()))
                except Exception:
                    continue
        except ImportError:
            pass

    # approximate: count Chinese vs non-Chinese characters
    cn_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    other_chars = max(0, len(text) - cn_chars)

    estimated = int(cn_chars / CHARS_PER_TOKEN + other_chars / 4.0)
    return max(estimated, len(text) // 10)


def compute_attachment_char_budget(
    context_window: int,
    usage_ratio: float = 0.50,
    output_tokens: int = 2000,
    template_tokens: int = 1200,
    body_chars: int = 8000,
    chars_per_token: float = CHARS_PER_TOKEN,
    cap: int | None = 500_000,
    legacy_default: int = 6000,
) -> int:
    """由上下文窗口预算反推「每附件提取文本的字符上限」。

    与 truncate_prompt_parts 共用同一个输入预算公式：
        input_budget_tokens = int(context_window * usage_ratio) - output_tokens
    从中预留出模板 + 正文 + 拼接开销后，按 chars_per_token（中文实测
    ~0.85 字符/token，见 CHARS_PER_TOKEN）换算回字符上限。cap 用于防极端（默认 50 万字符）。

    当 context_window 未知（<=0，表示未配置/探测失败）时回到 legacy_default，
    避免把硬编码 6000 的旧行为破坏掉。
    """
    if context_window <= 0:
        return legacy_default

    input_budget = int(context_window * usage_ratio) - output_tokens
    reserve = template_tokens + int(body_chars / chars_per_token) + 500
    budget_chars = int((input_budget - reserve) * chars_per_token)
    budget_chars = max(1024, budget_chars)
    if cap:
        budget_chars = min(budget_chars, cap)
    return budget_chars


def compute_body_and_attachment_char_budget(
    context_window: int,
    usage_ratio: float = 0.50,
    output_tokens: int = 2000,
    template_tokens: int = 1200,
    chars_per_token: float = CHARS_PER_TOKEN,
    body_ratio: float = 0.15,
    cap: int | None = 500_000,
    legacy_body: int = 8000,
    legacy_attach: int = 6000,
) -> tuple[int, int]:
    """由上下文窗口预算协同推导「正文 + 附件」的字符上限。

    正文与附件共享同一个输入预算池（扣除模板与拼接开销），按比例拆分：
      - 正文占 body_ratio（辅助信息，默认 0.15）
      - 附件占剩余（文书主体，默认 0.85，cap 封顶防极端）
    二者此消彼长、合计严格不超预算，因此第一阶段（仅丢附件、不截正文的
    预算模式）也不会因正文过长而超窗。

    返回 (body_max_chars, attachment_max_chars)。窗口未知（<=0）时回退
    legacy 值，保持旧行为。
    """
    if context_window <= 0:
        return legacy_body, legacy_attach

    input_budget = int(context_window * usage_ratio) - output_tokens
    reserve = template_tokens + 500  # 模板 + 拼接/系统提示开销
    available = max(1024, input_budget - reserve)
    body_tokens = int(available * body_ratio)
    attach_tokens = max(1024, available - body_tokens)

    body_chars = int(body_tokens * chars_per_token)
    attach_chars = int(attach_tokens * chars_per_token)
    if cap:
        attach_chars = min(attach_chars, cap)
    return body_chars, attach_chars


def truncate_prompt_parts(
    parts: dict[str, str],
    context_window_tokens: int,
    usage_ratio: float,
    output_tokens: int,
    token_method: str = "approximate",
) -> dict[str, str]:
    """Truncate prompt parts by priority until within context window budget.

    Returns modified parts dict with truncation applied.
    Priority (ascending, cut first): attachments -> body -> template (never cut)
    """
    raw_template = parts.get("template_with_body", "")
    raw_att = parts.get("attachment_texts", "")

    if not raw_att:
        return parts

    # Input budget = context_window * usage_ratio - output_tokens
    input_budget = int(context_window_tokens * usage_ratio) - output_tokens
    input_budget = max(input_budget, 1024)

    def _assemble(t, a):
        p = t
        if a:
            p += "\n\n## \u9644\u4ef6\u5185\u5bb9\n" + a  # 附件内容
        return p

    current_tokens = estimate_tokens(_assemble(raw_template, raw_att), token_method)
    if current_tokens <= input_budget:
        return parts

    logger.info(
        "Budget: %s tokens (ctx=%s x ratio=%s - output=%s), current: ~%s, truncating",
        input_budget, context_window_tokens, usage_ratio, output_tokens, current_tokens,
    )

    result = dict(parts)

    # Level 1: remove attachments individually (starting from the last one)
    if raw_att:
        att_segments = raw_att.split("=== ")
        att_parts = []
        for seg in att_segments:
            s = seg.strip()
            if s:
                prefix = "=== " if not seg.startswith("=== ") else ""
                att_parts.append(prefix + s)

        while att_parts and current_tokens > input_budget:
            removed = att_parts.pop()
            removed_tok = estimate_tokens(removed, token_method)
            logger.info("Truncation L1: removed attachment %s... (~%s tokens)", removed[:50], removed_tok)
            remaining_att = "\n\n".join(att_parts)
            current_tokens = estimate_tokens(
                _assemble(raw_template, remaining_att), token_method,
            )
            if current_tokens <= input_budget:
                result["attachment_texts"] = remaining_att
                return result
        result["attachment_texts"] = "\n\n".join(att_parts) if att_parts else ""

    # Level 2: truncate email body (from the end, in 15% chunks)
    if current_tokens > input_budget:
        body_marker = "## Input"
        output_marker = "## Output"
        body_start = raw_template.find(body_marker)
        output_start = raw_template.find(output_marker)

        if body_start >= 0 and output_start > body_start:
            template_head = raw_template[:body_start]
            body_region = raw_template[body_start:output_start]
            template_tail = raw_template[output_start:]

            while current_tokens > input_budget and len(body_region) > 100:
                cut = max(1, int(len(body_region) * 0.15))
                body_region = body_region[:-cut]
                current_tokens = estimate_tokens(
                    _assemble(template_head + body_region + template_tail, result.get("attachment_texts", "")),
                    token_method,
                )
            result["template_with_body"] = template_head + body_region + template_tail
            logger.info("Truncation L2: body truncated to ~%s tokens", estimate_tokens(body_region, token_method))

        if current_tokens > input_budget:
            # Final fallback: only keep the template structure
            fallback_body = "%s\n(body truncated due to length)\n%s\n" % (body_marker, output_marker)
            result["template_with_body"] = template_head + fallback_body + template_tail if all(
                x >= 0 for x in [body_start, output_start]
            ) else raw_template[:input_budget * 2]
            result["attachment_texts"] = ""
            logger.warning("Truncation fallback: only prompt template kept, body+attachments removed")

    return result
