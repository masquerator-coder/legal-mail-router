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


def estimate_tokens(text: str, method: str = "approximate") -> int:
    """Estimate token count for a text string.

    method:
      - "approximate": character-based (Chinese ~1.5c/tok, English ~4c/tok, mixed ~2.5c/tok)
      - "tiktoken": use tiktoken library if available (auto-select best encoder)
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

    estimated = int(cn_chars / 1.5 + other_chars / 4.0)
    return max(estimated, len(text) // 10)


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
