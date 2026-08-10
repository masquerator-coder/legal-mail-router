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
# 优先从项目根目录的 LLM提示词模板.md 读取，不存在时使用内嵌模板
_PROMPT_FILE = None  # 缓存文件路径

# ── 文书类型清单（文件驱动） ──
# 从项目根目录的 文书类型.md 读取；文件缺失时回退内置默认清单
_DOC_TYPES_FILE = None  # 缓存文件路径

# 内置默认文书类型（仅当 文书类型.md 缺失或为空时使用）
_DEFAULT_DOC_TYPES = [
    "合同协议", "起诉状", "判决书", "裁定书", "传票",
    "律师函", "证据材料", "通知书", "其他法律文书", "非法律文书",
]

# 系统兜底类型：无论配置文件如何，均强制包含
_FALLBACK_DOC_TYPES = ("其他法律文书", "非法律文书")


def _get_doc_types() -> list:
    """获取文书类型清单：从 文书类型.md 读取（每行一个类型），缺失时回退内置默认。

    系统兜底类型（其他法律文书/非法律文书）无论配置文件是否包含都会自动补齐。
    """
    global _DOC_TYPES_FILE
    if _DOC_TYPES_FILE is None:
        from app.config import BASE_DIR
        _DOC_TYPES_FILE = BASE_DIR / "文书类型.md"

    doc_types = []
    try:
        if _DOC_TYPES_FILE.exists():
            text = _DOC_TYPES_FILE.read_text(encoding="utf-8")
            for line in text.splitlines():
                name = line.strip()
                if name and not name.startswith("#"):
                    if name not in doc_types:
                        doc_types.append(name)
    except Exception as e:
        logger.error(f"读取 文书类型.md 失败: [{type(e).__name__}] {e}")

    if not doc_types:
        logger.error("文书类型.md 不存在或为空，使用内置默认文书类型清单")
        doc_types = list(_DEFAULT_DOC_TYPES)
    else:
        # 自动补齐系统兜底类型
        for fb in _FALLBACK_DOC_TYPES:
            if fb not in doc_types:
                doc_types.append(fb)
    return doc_types


def _get_doc_analysis_prompt(doc_type: str) -> str:
    """获取指定文书类型的分析流程提示词：读取 分析提示词/<类型>.md。

    文件不存在时回退到「其他法律文书」的提示词；目录缺失或读取失败返回空串。
    """
    from app.config import BASE_DIR
    name = (doc_type or "").strip()
    base = BASE_DIR / "分析提示词"
    if not base.is_dir():
        logger.error("分析提示词/ 目录不存在，无法加载文书分析流程提示词")
        return ""

    candidates = [name] if name else []
    if "其他法律文书" not in candidates:
        candidates.append("其他法律文书")
    for candidate in candidates:
        p = base / f"{candidate}.md"
        try:
            if p.exists():
                text = p.read_text(encoding="utf-8")
                if text.strip():
                    return text
        except Exception as e:
            logger.error(f"读取分析提示词 {candidate}.md 失败: [{type(e).__name__}] {e}")
    logger.warning(f"文书类型「{name}」无对应分析提示词且无兜底文件")
    return ""


def _get_default_prompt() -> str:
    """获取默认提示词模板：仅从 LLM提示词模板.md 读取，文件不存在时记录错误"""
    global _PROMPT_FILE
    if _PROMPT_FILE is None:
        from app.config import BASE_DIR
        _PROMPT_FILE = BASE_DIR / "LLM提示词模板.md"

    try:
        if _PROMPT_FILE.exists():
            text = _PROMPT_FILE.read_text(encoding="utf-8")
            if text.strip():
                return text
    except Exception as e:
        logger.error(f"读取 LLM提示词模板.md 失败: [{type(e).__name__}] {e}")
    logger.error("LLM提示词模板.md 不存在或为空，无法加载分析提示词")
    return ""


def _escape_json_string_newlines(text: str) -> str:
    """修复 JSON 字符串值中未转义的真实换行。

    等价于原 PCRE 写法 ``(?<=: )"(?:[^"\\\\]|\\\\.)*?\\K(?<!\\\\)\\n`` 的语义
    （Python re 不支持 \\K，Python 3.12+ 会抛 re.error）：
    匹配 `: "` 之后的 JSON 字符串字面量，把其中所有未转义的真实换行替换为 \\n。
    """
    def _fix(match: "re.Match[str]") -> str:
        return '"' + match.group(1).replace("\n", "\\n") + '"'

    # 贪婪匹配完整的字符串字面量（转义引号 \\" 由 \\. 消费，不会提前结束），
    # 然后统一转义内部真实换行；字面 "\\n"（反斜杠+n）不包含真实 LF，不受影响。
    return re.sub(r'(?<=: )"((?:[^"\\]|\\.)*)"', _fix, text)


def build_prompt(subject: str, sender: str, body: str, custom_prompt: str = "",
                  body_max_chars: int = 8000,
                  kb_context: str = "",
                  today_str: str = "",
                  analysis_instructions: str = "") -> str:
    """构建分析 prompt。body_max_chars 为邮件正文字符上限（0=不截断）。

    analysis_instructions 为按文书类型读取的分析流程提示词，嵌入主模板的
    {analysis_instructions} 占位符位置；为空时占位符被替换为空串。
    """
    if not today_str:
        from datetime import date
        today_str = date.today().isoformat()
    template = custom_prompt if custom_prompt.strip() else _get_default_prompt()
    # 模板是否引用 {analysis_instructions} 占位符（在哨兵替换前检查）
    template_has_analysis_placeholder = "{analysis_instructions}" in template

    # 截断过长的正文
    if body_max_chars > 0 and len(body) > body_max_chars:
        body_truncated = body[:body_max_chars]
    else:
        body_truncated = body

    # 分析流程提示词中可引用 {today}，此处单独替换（format 不递归处理参数值）
    if analysis_instructions and "{today}" in analysis_instructions:
        analysis_instructions = analysis_instructions.replace("{today}", today_str)

    # ⚠️ 用户自定义提示词中可能包含未转义的 { }（如 JSON 示例格式）
    # 需要先保护已知占位符，转义其余花括号，再恢复占位符后调用 .format()
    KNOWN_PLACEHOLDERS = {"{subject}", "{sender}", "{body}", "{today}", "{analysis_instructions}"}
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

    format_args = dict(subject=subject, sender=sender, body=body_truncated,
                       today=today_str, analysis_instructions=analysis_instructions)
    prompt = template.format(**format_args)
    # 自定义模板若未引用 {analysis_instructions} 占位符，将分析流程提示词追加到末尾，避免指令静默丢失
    if analysis_instructions and not template_has_analysis_placeholder:
        logger.warning("提示词模板未包含 {analysis_instructions} 占位符，已将文书分析流程提示词追加到提示词末尾")
        prompt += "\n\n" + analysis_instructions
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
    max_tokens: int = 4096,
    temperature: float = 0.3,
    attachment_texts: str = "",
    images: list = None,
    model_type: str = "unknown",
    kb_context: str = "",
    body_max_chars: int = 8000,
    timeout: int = 180,
    context_window: int = 0,
    usage_ratio: float = 0.50,
    token_method: str = "approximate",
    analysis_instructions: str = "",
) -> dict:
    """
    使用 LLM 分析邮件（含附件文本）—— 第二阶段文书分析

    analysis_instructions: 按文书类型读取的分析流程提示词，嵌入主模板 {analysis_instructions} 占位符。

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
                          analysis_instructions=analysis_instructions)

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
            {"role": "system", "content": f"你是一位资深法律文书分析专家。文书类型已由系统识别阶段确定，请按提示词中嵌入的类型专属分析流程进行解读与审核，无需重新判断文书类型。严格按JSON格式返回分析结果和修订文书，不要包含markdown代码块标记。禁止输出分析过程、思考步骤或推理说明，直接输出JSON。\n\n重要：当前真实日期是 {date.today().isoformat()}（这是今天的实际日期，你仅需据此计算时效和截止日等时间）"},
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
        repaired = _escape_json_string_newlines(repaired)
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

        # 诊断：输出是否被截断（如 max_tokens 不足导致 JSON 不完整）
        if content and not content.rstrip().endswith("}"):
            logger.error(
                f"LLM 输出疑似被截断（JSON 未闭合）: 末尾={content[-80:]!r}。"
                f"请检查 LLM 配置的 max_tokens 是否过小（合同/协议等文书需完整输出修改版正文，建议 ≥ 4096）"
            )
        logger.error(f"无法解析 LLM 响应: {content[:200]}")
        return _fallback_analysis(f"LLM 返回内容无法解析为 JSON: {content[:100]!r}")
    except (ValueError, TypeError) as e:
        logger.error(f"LLM 响应校验失败: {e}, content={content[:200]}")
        return _fallback_analysis(f"LLM 响应校验失败: {e}")


# ── 第一阶段：文书类型识别 ──


def _get_default_classify_prompt() -> str:
    """第一阶段（文书类型识别）默认提示词模板。

    支持占位符：{subject} {sender} {body} {doc_types} {today}。
    """
    return """你是法律文书类型识别专家。请判断以下邮件（含附件）属于哪一类文书，只返回 JSON，不要包含任何多余文字或 markdown 标记。

可选文书类型：
{doc_types}

判断规则：
- 从上述类型中选择最匹配的一种。
- 如果邮件明显不属于任何法律文书类型（如学术期刊、订阅推送、垃圾广告、个人通信等），选择「非法律文书」并设置 confidence >= 0.9。
- 如果对文书类型不确定，选择「其他法律文书」并适当降低 confidence。

返回格式：
{{"doc_type": "文书类型", "confidence": 0.0-1.0}}

邮件信息：
- 发件人：{sender}
- 主题：{subject}
- 正文：
{body}"""


def build_classify_prompt(subject: str, sender: str, body: str,
                          attachment_texts: str = "",
                          body_max_chars: int = 8000,
                          today_str: str = "",
                          custom_prompt: str = "") -> str:
    """构建文书类型识别 prompt（第一阶段）。

    候选类型来自 文书类型.md 配置清单；仅要求 LLM 返回类型与置信度。
    custom_prompt 为空时使用默认分类模板，支持 {subject}/{sender}/{body}/{doc_types}/{today} 占位符。
    """
    if not today_str:
        from datetime import date
        today_str = date.today().isoformat()
    if body_max_chars > 0 and len(body) > body_max_chars:
        body_truncated = body[:body_max_chars]
    else:
        body_truncated = body

    template = custom_prompt if custom_prompt.strip() else _get_default_classify_prompt()
    doc_types_str = " | ".join(_get_doc_types())

    # ⚠️ 自定义提示词中可能包含未转义的 { }（如 JSON 示例格式）
    # 先保护已知占位符，转义其余花括号，再恢复占位符后调用 .format()
    KNOWN_PLACEHOLDERS = {"{subject}", "{sender}", "{body}", "{doc_types}", "{today}"}
    sentinel_map = {}
    for i, ph in enumerate(KNOWN_PLACEHOLDERS):
        if ph in template:
            sentinel = f"\x00SENTINEL{i}\x00"
            sentinel_map[sentinel] = ph
            template = template.replace(ph, sentinel)
    template = template.replace("{", "{{").replace("}", "}}")
    for sentinel, ph in sentinel_map.items():
        template = template.replace(sentinel, ph)

    prompt = template.format(subject=subject, sender=sender, body=body_truncated,
                             doc_types=doc_types_str, today=today_str)
    if attachment_texts:
        prompt += f"\n\n## 附件内容\n{attachment_texts}"
    return prompt


def _get_default_group_prompt() -> str:
    """分组分析（多文书分组）默认提示词模板。

    支持占位符：{subject} {file_list}。用于判断哪些附件属于同一份文书。
    """
    return """你是法律文档分类助手。以下是邮件附件列表及内容预览，请判断这些附件：
A) 属于同一份法律文书的组成部分（如合同正文+附件表格+签章页）→ 归为一组
B) 包含多份独立的不同文书 → 各自成组

邮件主题：{subject}

附件列表：
{file_list}

请返回 JSON（只返回 JSON，不要多余文字）：
{{"groups": [[indices...], ...]}}

示例1（采购合同+报价单+保密协议）: {{"groups": [[0, 1], [2]]}}
示例2（起诉状+证据清单+证据材料+证据1）: {{"groups": [[0, 1, 2, 3]]}}
示例3（只有1个附件）: {{"groups": [[0]]}}"""


def build_group_prompt(subject: str, file_list: str, custom_prompt: str = "") -> str:
    """构建多附件分组 prompt（判断哪些附件属于同一份独立文书）。

    custom_prompt 为空时使用默认分组模板，支持 {subject}/{file_list} 占位符；
    自定义模板未含 {file_list} 占位符时，将附件列表追加到末尾，避免分组失去依据。
    """
    template = custom_prompt if custom_prompt.strip() else _get_default_group_prompt()
    has_file_list = "{file_list}" in template

    # ⚠️ 自定义提示词中可能包含未转义的 { }（如 JSON 示例格式）
    # 先保护已知占位符，转义其余花括号，再恢复占位符后调用 .format()
    KNOWN_PLACEHOLDERS = {"{subject}", "{file_list}"}
    sentinel_map = {}
    for i, ph in enumerate(KNOWN_PLACEHOLDERS):
        if ph in template:
            sentinel = f"\x00SENTINEL{i}\x00"
            sentinel_map[sentinel] = ph
            template = template.replace(ph, sentinel)
    template = template.replace("{", "{{").replace("}", "}}")
    for sentinel, ph in sentinel_map.items():
        template = template.replace(sentinel, ph)

    prompt = template.format(subject=subject, file_list=file_list)
    if not has_file_list and file_list:
        prompt += f"\n\n附件列表：\n{file_list}"
    return prompt


def _validate_classify_output(result: dict) -> dict:
    """校验并标准化类型识别结果；类型不在清单内时回退「其他法律文书」。"""
    if not isinstance(result, dict):
        raise ValueError("类型识别输出不是对象")

    doc_type = str(result.get("doc_type", "")).strip()
    if not doc_type or doc_type not in _get_doc_types():
        doc_type = "其他法律文书"

    confidence_raw = result.get("confidence", 0.5)
    try:
        confidence = float(confidence_raw)
        confidence = max(0.0, min(1.0, confidence))
    except (ValueError, TypeError):
        confidence = 0.5

    return {"doc_type": doc_type, "confidence": confidence}


async def classify_doc_type(
    api_url: str,
    api_key_encrypted: str,
    model_name: str,
    subject: str,
    sender: str,
    body: str,
    attachment_texts: str = "",
    body_max_chars: int = 8000,
    timeout: int = 60,
    custom_prompt: str = "",
) -> dict:
    """第一阶段：调用类型识别 LLM 判断文书类型。

    custom_prompt 为自定义分类提示词模板（LLM 配置 analysis_prompt），为空时使用默认分类模板。

    返回 {"doc_type": str, "confidence": float}；任何失败（含密钥解密失败）
    均回退 {"doc_type": "其他法律文书", "confidence": 0.5}，不中断主流程。
    """
    try:
        api_key = decrypt(api_key_encrypted)
        prompt = build_classify_prompt(subject, sender, body,
                                       attachment_texts=attachment_texts,
                                       body_max_chars=body_max_chars,
                                       custom_prompt=custom_prompt)

        if not api_url.endswith("/chat/completions"):
            api_url = api_url.rstrip("/") + "/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": "你是法律文书类型识别专家。只返回 JSON 结果，不要输出任何分析过程或多余文字。"},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 100,
            "temperature": 0,
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(api_url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"文书类型识别失败，回退「其他法律文书」: [{type(e).__name__}] {str(e)[:200]}")
        return {"doc_type": "其他法律文书", "confidence": 0.5}

    # 清理可能的 markdown 代码块标记
    if content.startswith("```"):
        content = content.split("\n", 1)[-1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
    content = content.strip()

    try:
        result = json.loads(content)
        return _validate_classify_output(result)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.error(f"类型识别响应解析失败，回退「其他法律文书」: [{type(e).__name__}] {str(e)[:200]}")
        return {"doc_type": "其他法律文书", "confidence": 0.5}


# ── 两阶段编排 ──


async def analyze_email_two_stage(
    api_url: str,
    api_key_encrypted: str,
    model_name: str,
    subject: str,
    sender: str,
    body: str,
    custom_prompt: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    attachment_texts: str = "",
    images: list = None,
    model_type: str = "unknown",
    kb_context: str = "",
    body_max_chars: int = 8000,
    timeout: int = 180,
    context_window: int = 0,
    usage_ratio: float = 0.50,
    token_method: str = "approximate",
    classifier_cfg: dict = None,
) -> dict:
    """两阶段邮件分析编排。

    第一阶段：类型识别（classifier_cfg 提供独立配置，缺省时复用 analyzer 配置）。
    - 判定为「非法律文书」→ 直接返回最小结果，跳过第二阶段分析。
    第二阶段：按类型读取 分析提示词/<类型>.md 嵌入主模板，进行完整分析。
    - 合并结果：doc_type/confidence 以第一阶段为准，其余字段取第二阶段。

    classifier_cfg: {"api_url", "api_key_encrypted", "model_name"} 或 None。
    """
    # ── 第一阶段：文书类型识别 ──
    c_url = classifier_cfg.get("api_url") if classifier_cfg else ""
    c_key = classifier_cfg.get("api_key_encrypted") if classifier_cfg else ""
    c_model = classifier_cfg.get("model_name") if classifier_cfg else ""
    c_prompt = classifier_cfg.get("analysis_prompt") if classifier_cfg else ""
    classify_result = await classify_doc_type(
        api_url=c_url or api_url,
        api_key_encrypted=c_key or api_key_encrypted,
        model_name=c_model or model_name,
        subject=subject,
        sender=sender,
        body=body,
        attachment_texts=attachment_texts,
        body_max_chars=body_max_chars,
        timeout=min(timeout, 60),
        custom_prompt=c_prompt or "",
    )
    doc_type = classify_result.get("doc_type", "其他法律文书")
    classifier_confidence = classify_result.get("confidence", 0.5)
    logger.info(f"第一阶段类型识别: {doc_type} (confidence={classifier_confidence})")

    # ── 非法律文书：跳过第二阶段 ──
    if doc_type == "非法律文书":
        return {
            "doc_type": "非法律文书",
            "case_summary": "非法律文书，无需法律分析",
            "ai_interpretation": "该邮件在文书类型识别阶段被判定为非法律文书，无需法律审核，仅归档即可。",
            "urgency": "low",
            "key_date": None,
            "case_number": None,
            "involved_parties": "",
            "confidence": classifier_confidence,
            "revised_document": None,
        }

    # ── 第二阶段：按类型完整分析 ──
    analysis_instructions = _get_doc_analysis_prompt(doc_type)
    analysis = await analyze_email(
        api_url=api_url,
        api_key_encrypted=api_key_encrypted,
        model_name=model_name,
        subject=subject,
        sender=sender,
        body=body,
        custom_prompt=custom_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        attachment_texts=attachment_texts,
        images=images,
        model_type=model_type,
        kb_context=kb_context,
        body_max_chars=body_max_chars,
        timeout=timeout,
        context_window=context_window,
        usage_ratio=usage_ratio,
        token_method=token_method,
        analysis_instructions=analysis_instructions,
    )

    # 合并：doc_type/confidence 以第一阶段识别结果为准（保证路由与日志稳定）
    if analysis and isinstance(analysis, dict):
        if analysis.get("doc_type") != doc_type:
            logger.warning(
                f"第二阶段返回文书类型「{analysis.get('doc_type')}」与第一阶段「{doc_type}」不一致，以第一阶段为准"
            )
        analysis["doc_type"] = doc_type
        analysis["confidence"] = classifier_confidence
    return analysis


_VALID_URGENCIES = frozenset({"high", "medium", "low"})


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


def _fallback_analysis(reason: str = "") -> dict:
    """LLM 调用失败时的兜底分析（携带失败原因，便于用户排查）"""
    summary = "LLM分析失败，请人工处理"
    interpretation = ""
    if reason:
        summary += f"（原因: {reason[:200]}）"
        interpretation = f"⚠️ LLM 分析失败: {reason[:300]}"
    return {
        "doc_type": "其他法律文书",
        "case_summary": summary,
        "ai_interpretation": interpretation,
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


# /v1/models 探测结果缓存（避免批量邮件时每封都发 HTTP 请求）
_CONTEXT_WINDOW_CACHE: dict[tuple[str, str], tuple[int, float]] = {}
_CONTEXT_WINDOW_CACHE_TTL = 3600.0  # 1 小时


def detect_context_window(api_url: str, api_key: str, model_name: str) -> int:
    """
    三层策略探测当前 LLM 的上下文窗口大小。

    1. 查 KNOWN_MODEL_WINDOWS 表
    2. 模型名关键词推断（如含 "128k"、"1m" 等）
    3. 尝试 /v1/models 接口（返回的模型列表可能含上下文信息）
    4. 回退 DEFAULT_CONTEXT_WINDOW
    """
    import time as _time
    cache_key = (api_url, model_name)
    cached = _CONTEXT_WINDOW_CACHE.get(cache_key)
    if cached and _time.monotonic() - cached[1] < _CONTEXT_WINDOW_CACHE_TTL:
        logger.debug(f"上下文窗口: {cached[0]} (缓存命中 {model_name})")
        return cached[0]

    def _finalize(window: int) -> int:
        _CONTEXT_WINDOW_CACHE[cache_key] = (window, _time.monotonic())
        return window

    # ── 策略1: 查表 ──
    model_lower = model_name.lower()
    for known, window in KNOWN_MODEL_WINDOWS.items():
        if known in model_lower:
            logger.info(f"上下文窗口: {window} ({known} 匹配 {model_name})")
            return _finalize(window)

    # ── 策略2: 关键词推断 ──
    m = re.search(r'(\d+)\s*[kK]', model_name)
    if m:
        window = int(m.group(1)) * 1024
        logger.info(f"上下文窗口: {window} (关键词推断: {m.group(0)})")
        return _finalize(window)
    m = re.search(r'(\d+)\s*[mM]', model_name)
    if m:
        window = int(m.group(1)) * 1_048_576
        logger.info(f"上下文窗口: {window} (关键词推断: {m.group(0)})")
        return _finalize(window)
    # 32k / 16k
    m = re.search(r'(\d+)\s*k', model_lower)
    if m:
        window = int(m.group(1)) * 1024
        logger.info(f"上下文窗口: {window} (关键词推断: {m.group(0)})")
        return _finalize(window)

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
                        return _finalize(window)
    except Exception as e:
        logger.debug(f"/v1/models 查询失败: {e}")

    # ── 策略4: 回退 ──
    logger.info(f"上下文窗口: {DEFAULT_CONTEXT_WINDOW} (未知模型 {model_name}，使用默认值)")
    return _finalize(DEFAULT_CONTEXT_WINDOW)


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
