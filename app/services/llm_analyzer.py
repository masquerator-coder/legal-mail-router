"""
LLM 分析模块 — 调用 OpenAI 兼容 API 分析法律文书
"""
import json
import logging
import re
from datetime import date
import httpx
from app.config import decrypt
from app.services.model_windows import KNOWN_MODEL_WINDOWS, DEFAULT_CONTEXT_WINDOW
from app.services.prompt_budget import estimate_tokens, truncate_prompt_parts

logger = logging.getLogger(__name__)

# ── 默认提示词模板 ──
# 优先从项目根目录的 LLM提示词.md 读取，不存在时使用内嵌模板
_PROMPT_FILE = None  # 缓存文件路径


def _get_default_prompt() -> str:
    """获取默认提示词模板：仅从 LLM提示词.md 读取，文件不存在时记录错误"""
    global _PROMPT_FILE
    if _PROMPT_FILE is None:
        from app.config import BASE_DIR
        _PROMPT_FILE = BASE_DIR / "LLM提示词.md"

    try:
        if _PROMPT_FILE.exists():
            text = _PROMPT_FILE.read_text(encoding="utf-8")
            if text.strip():
                return text
    except Exception as e:
        logger.error(f"读取 LLM提示词.md 失败: [{type(e).__name__}] {e}")
    logger.error("LLM提示词.md 不存在或为空，无法加载分析提示词")
    return ""


def build_prompt(subject: str, sender: str, body: str, custom_prompt: str = "",
                  body_max_chars: int = 8000,
                  kb_context: str = "",
                  routing_doc_types: list = None,
                  today_str: str = "") -> str:
    """构建分析 prompt。body_max_chars 为邮件正文字符上限（0=不截断）。"""
    if not today_str:
        from datetime import date
        today_str = date.today().isoformat()
    template = custom_prompt if custom_prompt.strip() else _get_default_prompt()

    # 截断过长的正文
    if body_max_chars > 0 and len(body) > body_max_chars:
        body_truncated = body[:body_max_chars]
    else:
        body_truncated = body

    # 构建文书类型列表
    if routing_doc_types:
        # 如果用户已配置「非法律文书」类型，则用「非法律文书」替代兜底的「其他法律文书」
        has_non_legal = "非法律文书" in routing_doc_types
        if has_non_legal:
            # 去掉「非法律文书」和「其他法律文书」，统一用「非法律文书」兜底
            filtered = [t for t in routing_doc_types if t not in ("非法律文书", "其他法律文书")]
            doc_types_str = " | ".join(filtered) + " | 非法律文书"
        else:
            doc_types_str = " | ".join(routing_doc_types) + " | 其他法律文书"
    else:
        doc_types_str = "合同协议 | 起诉状 | 判决书 | 裁定书 | 传票 | 律师函 | 证据材料 | 通知书 | 其他法律文书"

    # 如果使用了「非法律文书」兜底，追加分类指引
    classifier_hint = ""
    if routing_doc_types and "非法律文书" in routing_doc_types:
        classifier_hint = (
            "\n\n## 重要分类说明\n"
            "- 如果邮件明显不属于任何法律文书类型（如学术期刊、订阅推送、垃圾广告、个人通信等），"
            "请选择「非法律文书」并设置置信度 >= 0.9。\n"
            "- 「非法律文书」表示该邮件不需要法律处理，仅归档即可。"
        )

    # 自定义提示词可能不含 {doc_types}，仅默认模板使用
    format_args = dict(subject=subject, sender=sender, body=body_truncated, today=today_str)
    if "{doc_types}" in template:
        format_args["doc_types"] = doc_types_str

    # ⚠️ 用户自定义提示词中可能包含未转义的 { }（如 JSON 示例格式）
    # 需要先保护已知占位符，转义其余花括号，再恢复占位符后调用 .format()
    KNOWN_PLACEHOLDERS = {"{subject}", "{sender}", "{body}", "{doc_types}", "{today}"}
    # 保护阶段：替换已知占位符为唯一哨兵
    sentinel_map = {}
    for i, ph in enumerate(KNOWN_PLACEHOLDERS):
        if ph in template:
            sentinel = f"\x00SENTINEL{i}\x00"
            sentinel_map[sentinel] = ph
            template = template.replace(ph, sentinel)
    # 转义剩余花括号
    template = template.replace("{", "{{").replace("}", "}}")
    # 恢复已知占位符
    for sentinel, ph in sentinel_map.items():
        template = template.replace(sentinel, ph)

    prompt = template.format(**format_args)
    if classifier_hint:
        prompt += classifier_hint
    # 注入知识库检索结果
    if kb_context:
        prompt += "\n\n" + kb_context
    return prompt


async def analyze_email(
    api_url: str,
    api_key_encrypted: str,
    model_name: str,
    subject: str,
    sender: str,
    body: str,
    custom_prompt: str = "",
    max_tokens: int = 2000,
    temperature: float = 0.3,
    attachment_texts: str = "",
    routing_doc_types: list = None,
    images: list = None,
    model_type: str = "unknown",
    kb_context: str = "",
    body_max_chars: int = 8000,
    timeout: int = 180,
    context_window: int = 0,
    usage_ratio: float = 0.50,
    token_method: str = "approximate",
) -> dict:
    """
    使用 LLM 分析邮件（含附件文本）

    返回:
    {
        "doc_type": str,
        "case_summary": str,
        "ai_interpretation": str,
        "urgency": str,
        "key_date": str | None,
        "case_number": str | None,
        "involved_parties": str,
        "confidence": float,
    }
    """
    api_key = decrypt(api_key_encrypted)

    # 构建 base prompt（不含 kb_context，因为它需要单独跟踪用于截断）
    prompt = build_prompt(subject, sender, body, custom_prompt,
                          body_max_chars=body_max_chars,
                          routing_doc_types=None)

    # 预算检查（在提交前检测是否需要截断）
    # 注意：预算计算用的 expected_output_tokens 不等同于 max_tokens（API 输出上限），
    # max_tokens=102400 会把输入预算压到 1024 导致附件被全截掉。
    expected_output_tokens = min(max_tokens, 2000)
    if context_window > 0:
        parts = {
            "template_with_body": prompt,
            "kb_context": kb_context,
            "attachment_texts": attachment_texts,
        }
        parts = truncate_prompt_parts(parts, context_window, usage_ratio, expected_output_tokens, token_method)
        prompt = parts["template_with_body"]
        kb_context = parts.get("kb_context", "")
        attachment_texts = parts.get("attachment_texts", "")

    # 拼接 kb_context
    if kb_context:
        prompt += "\n\n" + kb_context

    # 拼接附件内容
    if attachment_texts:
        prompt += f"\n\n## 附件内容\n{attachment_texts}"

    # 确保 api_url 以 /v1 结尾的格式
    if not api_url.endswith("/chat/completions"):
        api_url = api_url.rstrip("/") + "/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 构建用户消息 — 支持多模态图片输入
    use_vision = images and len(images) > 0 and model_type == "multimodal"

    if use_vision:
        # 多模态模式：文本 + 图片
        content_parts = [{"type": "text", "text": prompt}]
        for img in images[:5]:  # 最多5张图片
            import base64
            b64 = base64.b64encode(img["content"]).decode()
            content_parts.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{img['mime_type']};base64,{b64}",
                    "detail": "high",
                }
            })
        user_message = {"role": "user", "content": content_parts}
        logger.info(f"使用多模态模式，附带 {len(images[:5])} 张图片")
    else:
        user_message = {"role": "user", "content": prompt}

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": f"你是一位资深法律文书分析专家。请先识别文书类型，再进行详细解读与审核。严格按JSON格式返回分析结果和修订文书，不要包含markdown代码块标记。禁止输出分析过程、思考步骤或推理说明，直接输出JSON。\n\n重要：当前真实日期是 {date.today().isoformat()}（这是今天的实际日期，你仅需据此计算时效和截止日等时间）"},
            user_message,
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(api_url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    content = data["choices"][0]["message"]["content"].strip()

    # 清理可能的 markdown 代码块标记
    if content.startswith("```"):
        # 移除 ```json 和结尾 ```
        content = content.split("\n", 1)[-1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
    content = content.strip()

    try:
        result = json.loads(content)
        validated = _validate_llm_output(result)
        return validated
    except json.JSONDecodeError:
        # 尝试修复常见 JSON 问题（revised_document 中未转义的换行符等）
        import re
        repaired = content
        repaired = re.sub(r'(?<=: )"(?:[^"\\]|\\.)*?\K(?<!\\)\n', '\\n', repaired)
        repaired = re.sub(r',\s*}', '}', repaired)
        repaired = re.sub(r',\s*]', ']', repaired)
        try:
            result = json.loads(repaired)
            validated = _validate_llm_output(result)
            logger.info("JSON 修复后解析成功")
            return validated
        except (json.JSONDecodeError, ValueError):
            pass

        # 尝试从内容中提取 JSON 对象
        import re
        match = re.search(r"\{[\s\S]*\}", content)
        if match:
            try:
                result = json.loads(match.group())
                return _validate_llm_output(result)
            except (json.JSONDecodeError, ValueError):
                pass
        logger.error(f"无法解析 LLM 响应: {content[:200]}")
        return _fallback_analysis()
    except (ValueError, TypeError) as e:
        logger.error(f"LLM 响应校验失败: {e}, content={content[:200]}")
        return _fallback_analysis()


_VALID_URGENCIES = frozenset({"high", "medium", "low"})
_ALLOWED_DOC_TYPE_SUFFIXES = ("书", "函", "状", "协议", "合同", "证明", "单", "令", "通知", "文书")


def _validate_llm_output(result: dict) -> dict:
    """
    对 LLM 返回的 JSON 做 schema 校验和字段标准化。
    如果关键字段缺失或类型错误，抛出 ValueError。
    """
    if not isinstance(result, dict):
        raise ValueError("LLM 输出不是对象")

    doc_type = str(result.get("doc_type", "")).strip()
    if not doc_type:
        doc_type = "其他法律文书"

    urgency = str(result.get("urgency", "medium")).strip().lower()
    if urgency not in _VALID_URGENCIES:
        urgency = "medium"

    confidence_raw = result.get("confidence", 0.5)
    try:
        confidence = float(confidence_raw)
        confidence = max(0.0, min(1.0, confidence))
    except (ValueError, TypeError):
        confidence = 0.5

    return {
        "doc_type": doc_type,
        "case_summary": str(result.get("case_summary", "") or ""),
        "ai_interpretation": str(result.get("ai_interpretation", "") or ""),
        "urgency": urgency,
        "key_date": str(result.get("key_date")) if result.get("key_date") else None,
        "case_number": str(result.get("case_number")) if result.get("case_number") else None,
        "involved_parties": str(result.get("involved_parties", "") or ""),
        "confidence": confidence,
        "revised_document": str(result.get("revised_document")) if result.get("revised_document") else None,
    }


_LLM_ANALYSIS_SCHEMA = frozenset({
    "doc_type", "case_summary", "ai_interpretation", "urgency",
    "key_date", "case_number", "involved_parties", "confidence",
    "revised_document",
})


def _fallback_analysis() -> dict:
    """LLM 调用失败时的兜底分析"""
    return {
        "doc_type": "其他法律文书",
        "case_summary": "LLM分析失败，请人工处理",
        "ai_interpretation": "",
        "urgency": "medium",
        "key_date": None,
        "case_number": None,
        "involved_parties": "",
        "confidence": 0.5,  # 非 0.0，避免被垃圾邮件过滤器误杀
        "revised_document": None,
    }


# ── 修改版文书生成 ──

# ════════════════════════════════════════════
# 已知模型上下文窗口（tokens）
# 已移至 config/model_windows.py
# ════════════════════════════════════════════

# 已知多模态模型关键词（用于探测补充）
_VISION_KEYWORDS = [
    "vision", "vl", "multimodal", "gemini", "claude", "gpt-4o", "gpt-4v",
    "pixtral", "llava", "qwen-vl", "qwenvl", "cogvlm", "glm-4v", "yi-vision",
    "deepseek-vl",
]


def detect_context_window(api_url: str, api_key: str, model_name: str) -> int:
    """
    三层策略探测当前 LLM 的上下文窗口大小。

    1. 查 KNOWN_MODEL_WINDOWS 表
    2. 模型名关键词推断（如含 "128k"、"1m" 等）
    3. 尝试 /v1/models 接口（返回的模型列表可能含上下文信息）
    4. 回退 DEFAULT_CONTEXT_WINDOW
    """
    # ── 策略1: 查表 ──
    model_lower = model_name.lower()
    for known, window in KNOWN_MODEL_WINDOWS.items():
        if known in model_lower:
            logger.info(f"上下文窗口: {window} ({known} 匹配 {model_name})")
            return window

    # ── 策略2: 关键词推断 ──
    # 128k / 200k / 1M 等显式标注
    m = re.search(r'(\d+)\s*[kK]', model_name)
    if m:
        window = int(m.group(1)) * 1024
        logger.info(f"上下文窗口: {window} (关键词推断: {m.group(0)})")
        return window
    m = re.search(r'(\d+)\s*[mM]', model_name)
    if m:
        window = int(m.group(1)) * 1_048_576
        logger.info(f"上下文窗口: {window} (关键词推断: {m.group(0)})")
        return window
    # 32k / 16k
    m = re.search(r'(\d+)\s*k', model_lower)
    if m:
        window = int(m.group(1)) * 1024
        logger.info(f"上下文窗口: {window} (关键词推断: {m.group(0)})")
        return window

    # ── 策略3: 查 /v1/models ──
    try:
        if not api_url.endswith("/chat/completions"):
            base_url = api_url.rstrip("/")
        else:
            base_url = api_url.rsplit("/chat/completions", 1)[0]
        models_url = base_url + "/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        # 同步查询（探测时通常已在线程中）
        resp = httpx.get(models_url, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            for m_info in data.get("data", []):
                m_id = m_info.get("id", "").lower()
                for known, window in KNOWN_MODEL_WINDOWS.items():
                    if known in m_id:
                        logger.info(f"上下文窗口: {window} (/v1/models 匹配: {known})")
                        return window
    except Exception as e:
        logger.debug(f"/v1/models 查询失败: {e}")

    # ── 策略4: 回退 ──
    logger.info(f"上下文窗口: {DEFAULT_CONTEXT_WINDOW} (未知模型 {model_name}，使用默认值)")
    return DEFAULT_CONTEXT_WINDOW


def get_effective_context_window(
    db_setting: str | None, api_url: str, api_key: str, model_name: str
) -> int:
    """
    获取有效的上下文窗口：
    - 用户设置 >0 → 直接使用
    - 用户设置 =0 或未设置 → 自动探测
    """
    if db_setting and db_setting.strip():
        try:
            user_val = int(db_setting)
            if user_val > 0:
                logger.info(f"上下文窗口: {user_val} (用户设置)")
                return user_val
        except ValueError:
            pass
    return detect_context_window(api_url, api_key, model_name)
