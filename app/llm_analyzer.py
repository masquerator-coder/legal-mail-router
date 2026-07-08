"""
LLM 分析模块 — 调用 OpenAI 兼容 API 分析法律文书
"""
import json
import logging
import re
import httpx
from app.config import decrypt
from config.model_windows import KNOWN_MODEL_WINDOWS, DEFAULT_CONTEXT_WINDOW
from app.prompt_budget import estimate_tokens, truncate_prompt_parts

logger = logging.getLogger(__name__)

# ── 默认提示词模板 ──
# 优先从项目根目录的 LLM提示词.md 读取，不存在时使用内嵌模板
_PROMPT_FILE = None  # 缓存文件路径


def _get_default_prompt() -> str:
    """获取默认提示词模板：优先从 LLM提示词.md 读取，不存在时用内嵌模板"""
    global _PROMPT_FILE
    if _PROMPT_FILE is None:
        from app.config import BASE_DIR
        _PROMPT_FILE = BASE_DIR / "LLM提示词.md"

    try:
        if _PROMPT_FILE.exists():
            text = _PROMPT_FILE.read_text(encoding="utf-8")
            if text.strip():
                return text
    except Exception:
        logger.warning("读取 LLM提示词.md 失败，使用内嵌默认模板")
    return DEFAULT_ANALYSIS_PROMPT


DEFAULT_ANALYSIS_PROMPT = """你是一位资深法律文书分析专家。请按以下两阶段分析邮件及附件内容：

## 第一阶段：文书类型识别
根据邮件标题、正文和附件内容，从以下类型中选择最匹配的文书类型：
{doc_types}

## 第二阶段：详细解读与审核
无论文书类型，请进行以下全面分析：
1. **案情摘要**：概括案件核心内容和涉及方
2. **法律要点分析**：识别核心法律问题，分析适用的法律依据和裁判规则
3. **关键信息提取**：案号、关键日期（开庭/答辩/上诉截止等）、涉及金额、管辖法院/机关等
4. **风险提示**：潜在的法律风险、程序风险、时效风险、证据风险
5. **处理建议**：应采取的下一步行动、需要准备的材料、注意事项、是否建议委托专业律师

## 邮件信息
- 发件人：{sender}
- 主题：{subject}
- 内容：
{body}

注意：如果下方有"## 附件内容"段落，请以附件原文为主要分析依据。附件可能包含合同全文、判决书原文、起诉状、证据清单等。

## 输出格式
请严格返回 JSON 格式（不要包含 markdown 代码块标记）：
{{
  "doc_type": "文书类型（从上述类型中选择）",
  "case_summary": "一句话概括案件内容和涉及方",
  "ai_interpretation": "第二阶段完整分析结果，包含：法律要点分析、关键信息提取、风险提示（按重要性分级）、处理建议。300-600字。",
  "urgency": "high|medium|low",
  "key_date": "关键日期或null",
  "case_number": "案号或null",
  "involved_parties": "涉及方名称（逗号分隔）",
  "target_lawyer_type": "民事|刑事|行政|知识产权|劳动纠纷|公司商事|默认",
  "confidence": 0.0-1.0之间的置信度
}}"""


def build_prompt(subject: str, sender: str, body: str, custom_prompt: str = "",
                  body_max_chars: int = 8000,
                  kb_context: str = "",
                  routing_doc_types: list = None) -> str:
    """构建分析 prompt。body_max_chars 为邮件正文字符上限（0=不截断）。"""
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
    format_args = dict(subject=subject, sender=sender, body=body_truncated)
    if "{doc_types}" in template:
        format_args["doc_types"] = doc_types_str

    # ⚠️ 用户自定义提示词中可能包含未转义的 { }（如 JSON 示例格式）
    # 需要先保护已知占位符，转义其余花括号，再恢复占位符后调用 .format()
    KNOWN_PLACEHOLDERS = {"{subject}", "{sender}", "{body}", "{doc_types}"}
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
        "target_lawyer_type": str,
        "confidence": float,
    }
    """
    api_key = decrypt(api_key_encrypted)

    # 构建 base prompt（不含 kb_context，因为它需要单独跟踪用于截断）
    prompt = build_prompt(subject, sender, body, custom_prompt,
                          body_max_chars=body_max_chars,
                          routing_doc_types=None)

    # 预算检查（在提交前检测是否需要截断）
    if context_window > 0:
        parts = {
            "template_with_body": prompt,
            "kb_context": kb_context,
            "attachment_texts": attachment_texts,
        }
        parts = truncate_prompt_parts(parts, context_window, usage_ratio, max_tokens, token_method)
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
            {"role": "system", "content": "你是一位资深法律文书分析专家。请先识别文书类型，再进行详细解读与审核。严格按JSON格式返回两阶段分析结果，不要包含markdown代码块标记。"},
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
    except json.JSONDecodeError:
        # 尝试从内容中提取 JSON
        import re
        match = re.search(r"\{[\s\S]*\}", content)
        if match:
            result = json.loads(match.group())
        else:
            logger.error(f"无法解析 LLM 响应: {content[:200]}")
            return _fallback_analysis()

    # 标准化字段
    return {
        "doc_type": result.get("doc_type", "其他法律文书"),
        "case_summary": result.get("case_summary", ""),
        "ai_interpretation": result.get("ai_interpretation", ""),
        "urgency": result.get("urgency", "medium"),
        "key_date": result.get("key_date"),
        "case_number": result.get("case_number"),
        "involved_parties": result.get("involved_parties", ""),
        "target_lawyer_type": result.get("target_lawyer_type", "默认"),
        "confidence": float(result.get("confidence", 0.5)),
    }


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
        "target_lawyer_type": "默认",
        "confidence": 0.5,  # 非 0.0，避免被垃圾邮件过滤器误杀
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


DEFAULT_REVISION_PROMPT = """你是一位资深法律文书撰写专家。根据以下审核意见，对原始文书进行修订，输出完整的修改版文书。

## 原始文书类型
{doc_type}

## AI 审核意见
{ai_interpretation}

## 原始文书全文
{original_text}

## 修订要求
1. 修正审核意见中指出的法律错误、程序瑕疵和表述不当
2. 补充缺失的关键条款（违约责任、争议解决、送达地址、管辖约定等）
3. 调整明显不平衡的权利义务条款
4. 保持原文书的整体结构、段落顺序和行文风格

{template_section}

## 输出格式 — 改动标记规范
输出完整修改版文书全文。所有改动必须用以下标记标注：

- 【新增】补充的条款或内容【/新增】
- 【修改】改动后的表述【/修改】
- 【删除】建议删除的原文【/删除】

规则：
- 标记可以跨行，但不能嵌套
- 【新增】和【修改】标记内的文本是最终版本文书的一部分
- 【删除】标记内的文本仅为审阅参考（表示建议从文书中移除）
- 未改动的段落直接输出原文，不要加任何标记
- 直接输出文书全文，不要加「以下是修改版」等前言后语"""


async def generate_revision(
    api_url: str,
    api_key_encrypted: str,
    model_name: str,
    doc_type: str,
    original_text: str,
    ai_interpretation: str,
    custom_prompt: str = "",
    template: str | None = None,
    max_tokens: int = 4000,
    temperature: float = 0.3,
    timeout: int = 180,
) -> str | None:
    """
    根据 LLM 审核意见生成修改版文书。

    template: 可选的文书格式模板，传入后 LLM 将严格遵循模板格式修订。
    返回修改后的文书全文（含改动标记），失败时返回 None。
    """
    from app.config import decrypt

    api_key = decrypt(api_key_encrypted)
    prompt_template = custom_prompt.strip() if custom_prompt.strip() else DEFAULT_REVISION_PROMPT

    # ── 构建模板段 ──
    if template and template.strip():
        template_section = (
            "## 文书格式模板（必须遵循）\n\n"
            "以下是「{doc_type}」的标准格式模板。修订后的文书必须在以下方面与模板保持一致：\n"
            "1. **格式**：标题层级、段落编号方式、签章位置\n"
            "2. **内容结构**：各部分的名称、顺序和必备要素\n"
            "3. **写作逻辑**：论证方式、法言法语风格\n\n"
            f"模板如下：\n"
            "───────────────────────────────\n"
            f"{template.strip()[:3000]}\n"
            "───────────────────────────────\n\n"
            "请严格按照上述模板组织修订后的文书。模板中标注的 [xxx] 占位符号需从原始文书中提取实际信息填入。"
        )
    else:
        template_section = ""

    prompt = prompt_template.format(
        doc_type=doc_type,
        ai_interpretation=ai_interpretation,
        original_text=original_text[:8000],  # 截断过长的原文
        template_section=template_section,
    )

    # 确保 api_url 以 /chat/completions 结尾
    if not api_url.endswith("/chat/completions"):
        api_url = api_url.rstrip("/") + "/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "你是一位资深法律文书撰写专家。请根据审核意见和格式模板修订文书，用【新增】【修改】【删除】标记标注所有改动。直接输出完整文书，不加前言。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(api_url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        revision = data["choices"][0]["message"]["content"].strip()
        if revision:
            logger.info(f"修改版文书生成成功 ({len(revision)} 字符)")
            return revision
        return None
    except Exception as e:
        logger.error(f"修改版文书生成失败: [{type(e).__name__}] {e}")
        return None
