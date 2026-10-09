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

# ── 文书类型清单（文件驱动：以 分析提示词/ 下的文件种类为准） ──
# 目录下每个 `.md` 文件即一个文书类型，**文件名（去扩展名）即类型名**：
# 增删提示词文件即增删文书类型，代码中不维护任何文书类型清单
# （避免清单多处维护、新增类型漏同步）。
_PROMPT_DIR = None  # 缓存目录路径

# 系统固定类型：不受提示词文件增删影响，始终存在
# - 其他法律文书：未匹配到专属流程时的兜底提示词锚点
# - 非法律文书：识别后跳过分析阶段的控制值，不需要提示词文件
_FALLBACK_DOC_TYPES = ("其他法律文书", "非法律文书")

# ── 类型能力标记（在提示词文件头部的 `caps:` 行声明，见 _split_prompt_header）──
# 是否生成修改版文书 / 审查意见，**只由提示词文件头部决定**。
CAP_REVISION = "修订"   # 生成「修改版文书.docx」
CAP_REVIEW = "审查"     # 生成审查意见.docx
CAP_CONTRACT = "合同"   # 审查意见使用「合同审核意见模板」（合同专用字段），否则用律师审查意见模板

_CAPABILITY_MARKERS = frozenset({CAP_REVISION, CAP_REVIEW, CAP_CONTRACT})

# 头部块分隔行
_PROMPT_HEADER_DELIM = "---"

# 类型 → 能力集合 的缓存。
# ⚠️ 缓存键必须覆盖**每个提示词文件的内容指纹**（名 + mtime + size）：
# 能力声明在文件内容里，而目录自身的 mtime 只在增删文件时才变，
# 仅凭目录 mtime 会导致「编辑 caps 后不生效」的静默问题。
_doc_type_caps_cache: tuple | None = None

# 第一阶段类型识别的输出预算下限。
# 推理型模型（DeepSeek-V4-Flash 等）把推理过程计入 max_tokens，预算过小时 JSON 还没输出就被截断，
# 表现为「解析失败 → 兜底其他法律文书」。此下限用于兜住模型配置里过小/未设置的值。
_CLASSIFY_MIN_MAX_TOKENS = 512

# MCP 工具使用要求（启用 MCP 时注入提示词，指导模型实时查询法规并附引用链接）
_MCP_USAGE_INSTRUCTION = """## 法律检索工具（MCP）
本次分析已提供法律检索工具，可实时查询「北大法宝」权威法律法规数据库。请遵循：
1. 当需要引用具体法律法规、司法解释或法条原文时，请调用相应工具检索（如 adjust_provisions 获取权威条文原文及司法解释、search_article 语义检索法规、get_article/get_law_item_content 按法规标题+条号精确取条）。
2. 必须以工具返回的权威条文原文为依据进行分析与引用，严禁仅凭训练数据编造法条内容或条号。
3. 每引用一条法规，须在 ai_interpretation 报告末尾附加「引用依据」小节，逐条列出：法规名称、对应条文、来源链接（取自工具返回结果中的 url 字段）。
4. 若生成了修改版文书 revised_document 且其中引用了法规，请在文书正文结束后另起一行附「引用依据」列表（法规名称 + 来源链接）。
5. 工具调用失败或未返回结果时，正常按现有知识与逻辑完成分析，不要中断，也不要编造引用链接。
"""


def _split_prompt_header(text: str) -> tuple[str, str]:
    """拆分提示词文件的头部元数据与正文，返回 (caps 原文, 正文)。

    头部格式（可选，须位于文件开头）::

        ---
        caps: 修订, 审查
        ---
        ### 某类文书专属分析流程
        ...

    头部用于声明该文书类型的能力（修订/审查/合同）；无头部时 caps 为空串。
    返回的正文**不含头部**——提示词会被原样嵌入 LLM 请求，元数据不得泄漏进去。
    未闭合的头部（缺结尾 ``---``）视为无头部，避免误吞正文。
    """
    lines = text.split("\n")
    # 跳过前置空行，头部必须以 --- 行开头
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != _PROMPT_HEADER_DELIM:
        return "", text

    j = idx + 1
    header_lines: list[str] = []
    while j < len(lines) and lines[j].strip() != _PROMPT_HEADER_DELIM:
        header_lines.append(lines[j])
        j += 1
    if j >= len(lines):
        return "", text  # 头部未闭合，按无头部处理

    caps_raw = ""
    for line in header_lines:
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "caps":
            caps_raw = value.strip()
            break
    return caps_raw, "\n".join(lines[j + 1:])


def _parse_caps(raw: str, doc_type: str = "") -> frozenset:
    """解析头部 caps 值，返回能力集合（CAP_REVISION / CAP_REVIEW / CAP_CONTRACT 的子集）。

    分隔符支持中英文逗号、顿号、分号与空白；未识别的标记记 WARNING 后忽略，
    以免笔误（如「修仃」）导致能力静默丢失。
    """
    caps = set()
    for token in re.split(r"[\s,，、;；#]+", (raw or "").strip()):
        token = token.strip()
        if not token:
            continue
        if token in _CAPABILITY_MARKERS:
            caps.add(token)
        else:
            logger.warning(
                f"文书类型「{doc_type or '未知'}」的 caps 含未识别标记「{token}」，已忽略"
            )
    return frozenset(caps)


def _prompt_files() -> list:
    """返回 分析提示词/ 下的 `.md` 文件路径列表（按文件名排序，保证类型顺序可复现）。"""
    from app.config import BASE_DIR
    global _PROMPT_DIR
    if _PROMPT_DIR is None:
        _PROMPT_DIR = BASE_DIR / "分析提示词"
    if not _PROMPT_DIR.is_dir():
        return []
    try:
        return sorted(_PROMPT_DIR.glob("*.md"), key=lambda p: p.name)
    except OSError as e:
        logger.error(f"扫描 分析提示词/ 目录失败: [{type(e).__name__}] {e}")
        return []


def _load_doc_type_caps() -> dict[str, frozenset]:
    """扫描 分析提示词/*.md，返回 {类型名: 能力集合}。

    类型名 = 文件名去扩展名；能力 = 文件头部 caps 声明（无头部即无能力）。
    顺序：非系统类型按文件名排序在前，系统固定类型（_FALLBACK_DOC_TYPES）置尾，
    保证候选清单顺序稳定可复现（不随文件系统枚举顺序变化）。
    目录缺失或没有 .md 文件时仅返回系统固定类型并记录错误——此时提示词也不存在，
    主模板的通用兜底说明会接管（见 LLM提示词模板.md 步骤一）。
    结果按各提示词文件的 (名, mtime_ns, size) 缓存：增删文件或修改内容均自动失效。
    """
    global _doc_type_caps_cache

    files = _prompt_files()
    key_parts = []
    for p in files:
        try:
            st = p.stat()
            key_parts.append((p.name, st.st_mtime_ns, st.st_size))
        except OSError:
            key_parts.append((p.name, None, None))
    cache_key = (str(_PROMPT_DIR), tuple(key_parts))

    if _doc_type_caps_cache and _doc_type_caps_cache[0] == cache_key:
        return _doc_type_caps_cache[1]

    scanned: dict[str, frozenset] = {}
    for p in files:
        name = p.stem
        try:
            text = p.read_text(encoding="utf-8").lstrip("\ufeff")
        except Exception as e:
            logger.error(f"读取分析提示词 {p.name} 失败: [{type(e).__name__}] {e}")
            scanned.setdefault(name, frozenset())
            continue
        caps_raw, _body = _split_prompt_header(text)
        scanned[name] = _parse_caps(caps_raw, name)

    if not scanned:
        logger.error("分析提示词/ 目录不存在或没有 .md 文件，文书类型清单仅含系统固定类型")
        caps_map: dict[str, frozenset] = {}
    else:
        caps_map = {name: scanned[name] for name in sorted(scanned)
                    if name not in _FALLBACK_DOC_TYPES}
    # 系统固定类型始终可用（补齐缺失者）
    for fb in _FALLBACK_DOC_TYPES:
        caps_map[fb] = scanned.get(fb, frozenset())

    _doc_type_caps_cache = (cache_key, caps_map)
    return caps_map


def _get_doc_types() -> list:
    """获取文书类型清单：以 分析提示词/ 下的 .md 文件种类为准。

    系统固定类型（其他法律文书/非法律文书）无论目录如何都会补齐。
    """
    return list(_load_doc_type_caps().keys())


def get_doc_type_capabilities(doc_type: str | None) -> frozenset:
    """获取某文书类型的能力集合（来自该类型提示词文件头部的 caps 声明）。

    返回值是 CAP_REVISION / CAP_REVIEW / CAP_CONTRACT 的子集。
    未知类型（无对应提示词文件）返回空集合——即不生成修改版，也不生成审查意见。
    """
    name = (doc_type or "").strip()
    if not name:
        return frozenset()
    return _load_doc_type_caps().get(name, frozenset())


def should_generate_revision(doc_type: str | None) -> bool:
    """该类型是否应生成修改版文书（由提示词文件头部的 caps: 修订 决定）"""
    return CAP_REVISION in get_doc_type_capabilities(doc_type)


def should_generate_review(doc_type: str | None) -> bool:
    """该类型是否应生成审查意见（由提示词文件头部的 caps: 审查 决定）"""
    return CAP_REVIEW in get_doc_type_capabilities(doc_type)


def is_contract_type(doc_type: str | None) -> bool:
    """该类型是否使用合同审核意见模板（由提示词文件头部的 caps: 合同 决定）"""
    return CAP_CONTRACT in get_doc_type_capabilities(doc_type)


def _get_doc_analysis_prompt(doc_type: str) -> str:
    """获取指定文书类型的分析流程提示词：读取 分析提示词/<类型>.md。

    返回**已剥离头部元数据**的正文（头部仅用于声明能力，不得进入 LLM 请求）。
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
                text = p.read_text(encoding="utf-8").lstrip("\ufeff")
                _caps, body = _split_prompt_header(text)
                if body.strip():
                    return body
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


def _build_revision_section(doc_type: str | None) -> str:
    """按文书类型能力生成主模板 {revision_instructions} 占位符内容。

    可修订类型（conf 中带 #修订）：输出完整的「生成修改版文书」章节与改动标记规范。
    其余类型：明确告知无需输出修订正文，避免模型生成代码随后会丢弃的内容。
    """
    if should_generate_revision(doc_type):
        return """### 步骤三：生成修改版文书（对应输出字段 revised_document）
基于以上审核发现，在 `revised_document` 字段输出完整的修改后文书正文。修订原则：
1. 逐条修正条款问题；补充缺失的关键条款（违约责任、争议解决、送达地址、管辖约定等），调整明显失衡的权利义务条款。
2. 保持原文的整体结构、段落顺序与行文风格。
3. 修订建议与现行法律法规强制性规定明显冲突时，原文保留并标注【待核实】。

**改动标记规范（颜色标注指令）：** 系统按以下标记渲染颜色（最终文档只显示颜色，不显示标记文字）：

| 标记（须成对包裹全部内容） | 渲染效果 |
| --- | --- |
| 【新增】…【/新增】 | 蓝色 |
| 【修改】…【/修改】 | 红色 |
| 【删除】…【/删除】 | 红色 + 删除线 |

**标记粒度要求（重要）：**
系统会把标记渲染成 Word 原生修订（删除线 / 插入内容），供律师逐条接受或拒绝。
标记圈得越大，审阅者越难看清到底改了什么。因此：

1. **【修改】只包裹真正改动的词句，不要包住整段。** 段落里没有变动的部分
   必须**原样输出在标记之外**。例如原文「甲方应在30日内支付价款」改为
   「甲方应在60日内支付价款」时，应写成
   `甲方应在【修改】60【/修改】日内支付价款`，
   **不要**写成 `【修改】甲方应在60日内支付价款【/修改】`。
2. 一个段落有多处不相邻的改动时，**分别**用多组标记包裹，不要合成一个
   覆盖整段的大标记。
3. 【新增】只包裹**新增**的文字；【删除】只包裹**要删除**的文字，
   两者都不得顺手把周围未改动的文字一起圈进去。
4. 整段新增（原文没有这一整段）时，才用【新增】包裹整个段落。
5. 段落的换行与分段的切割必须与原文保持一致：**不要**把原文中两个独立段落
   合并成一段输出，也**不要**把原文的一个段落拆成多段。原样保留未改动的
   段落分隔，否则渲染出的修订会出现「整段删除 + 整段新增」的大块标记。

使用要求：
1. 开标记与闭合标记必须成对（闭合格式固定为【/标记名】），内容无论多长都须完整包裹；**禁止只在段落行首加【新增】等开标记而不闭合**。
2. 标记不得嵌套；未改动的段落原样输出，不加任何标记。
3. 标记内容必须是原文中**逐字存在**的连续片段（用于精确定位）；不要为凑标记而改写未改动的文字。
4. 原文中的【待确认】等业务占位符不是修订标记，原样保留；不要用「（新增）」等文字代替标记。

**修订输出规则：**
- 只输出修订后的文书正文，不加「以下是修改版」等前言，不输出分析过程、思考步骤或推理说明。
- 输出较长时优先保证 JSON 结构完整（所有字段闭合、引号正确），可适当精简 ai_interpretation，但不得截断输出。"""

    return """### 步骤三：不生成修改版文书
本类型文书无需修订，`revised_document` 字段请直接输出 `null`，不要输出任何修订后的文书正文。"""


def _build_revision_field_desc(doc_type: str | None) -> str:
    """Output JSON schema 中 revised_document 字段的说明文字（按类型能力生成）"""
    if should_generate_revision(doc_type):
        return "完整的修改后文书正文（含【新增】【修改】【删除】标记）"
    return "固定为null（本类型无需修订）"


def build_prompt(subject: str, sender: str, body: str, custom_prompt: str = "",
                  body_max_chars: int = 8000,
                  today_str: str = "",
                  analysis_instructions: str = "",
                  doc_type: str | None = None) -> str:
    """构建分析 prompt。body_max_chars 为邮件正文字符上限（0=不截断）。

    analysis_instructions 为按文书类型读取的分析流程提示词，嵌入主模板的
    {analysis_instructions} 占位符位置；为空时占位符被替换为空串。

    doc_type 决定该类型是否输出修订内容（由提示词文件头部的 caps: 修订 决定）：
    可修订类型渲染「生成修改版文书」章节，其余类型渲染为「无需修订」的说明，
    避免提示词要求模型输出代码随后会丢弃的字段。
    """
    if not today_str:
        from datetime import date
        today_str = date.today().isoformat()
    template = custom_prompt if custom_prompt.strip() else _get_default_prompt()
    # 模板是否引用占位符（在哨兵替换前检查）
    template_has_analysis_placeholder = "{analysis_instructions}" in template
    template_has_revision_placeholder = "{revision_instructions}" in template

    # 截断过长的正文
    if body_max_chars > 0 and len(body) > body_max_chars:
        body_truncated = body[:body_max_chars]
    else:
        body_truncated = body

    # 分析流程提示词中可引用 {today}，此处单独替换（format 不递归处理参数值）
    if analysis_instructions and "{today}" in analysis_instructions:
        analysis_instructions = analysis_instructions.replace("{today}", today_str)

    revision_instructions = _build_revision_section(doc_type)
    revision_field_desc = _build_revision_field_desc(doc_type)

    # 分析流程提示词中可引用 {today}，此处单独替换（format 不递归处理参数值）
    if analysis_instructions and "{today}" in analysis_instructions:
        analysis_instructions = analysis_instructions.replace("{today}", today_str)

    # ⚠️ 用户自定义提示词中可能包含未转义的 { }（如 JSON 示例格式）
    # 需要先保护已知占位符，转义其余花括号，再恢复占位符后调用 .format()
    KNOWN_PLACEHOLDERS = {"{subject}", "{sender}", "{body}", "{today}",
                          "{analysis_instructions}", "{revision_instructions}",
                          "{revision_field_desc}"}
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
                       today=today_str, analysis_instructions=analysis_instructions,
                       revision_instructions=revision_instructions,
                       revision_field_desc=revision_field_desc)
    prompt = template.format(**format_args)
    # 自定义模板若未引用 {revision_instructions} 占位符，把修订要求追加到末尾，
    # 避免「模板未含占位符 → 模型仍被要求输出 revised_document」的静默不一致。
    if revision_instructions and not template_has_revision_placeholder:
        logger.warning("提示词模板未包含 {revision_instructions} 占位符，已将修订要求追加到提示词末尾")
        prompt += "\n\n" + revision_instructions
    if analysis_instructions and not template_has_analysis_placeholder:
        logger.warning("提示词模板未包含 {analysis_instructions} 占位符，已将文书分析流程提示词追加到提示词末尾")
        prompt += "\n\n" + analysis_instructions
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
    body_max_chars: int = 8000,
    timeout: int = 180,
    context_window: int = 0,
    usage_ratio: float = 0.50,
    token_method: str = "approximate",
    analysis_instructions: str = "",
    mcp_servers: list = None,
    mcp_max_turns: int = 5,
    doc_type: str | None = None,
) -> dict:
    """
    使用 LLM 分析邮件（含附件文本）—— 第二阶段文书分析

    analysis_instructions: 按文书类型读取的分析流程提示词，嵌入主模板 {analysis_instructions} 占位符。

    doc_type: 第一阶段识别出的文书类型，决定是否要求模型输出 revised_document
              （由提示词文件头部的 caps: 修订 决定）。

    mcp_servers: MCP 服务器配置（list of {name,url,headers} 或兼容形态）。非空时启用 MCP 工具
                 调用循环（如北大法宝法规检索），模型可实时查法规并在报告/修改版文书末尾附引用链接。
    mcp_max_turns: MCP 工具调用的最大轮次（每轮可含多个工具调用），超出则取当前内容。

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

    # 构建 base prompt
    prompt = build_prompt(subject, sender, body, custom_prompt,
                          body_max_chars=body_max_chars,
                          analysis_instructions=analysis_instructions,
                          doc_type=doc_type)

    # 预算检查（在提交前检测是否需要截断）
    # 注意：预算计算用的 expected_output_tokens 不等同于 max_tokens（API 输出上限），
    # max_tokens=102400 会把输入预算压到 1024 导致附件被全截掉。
    expected_output_tokens = min(max_tokens, 2000)
    if context_window > 0:
        parts = {
            "template_with_body": prompt,
            "attachment_texts": attachment_texts,
        }
        parts = truncate_prompt_parts(parts, context_window, usage_ratio, expected_output_tokens, token_method)
        prompt = parts["template_with_body"]
        attachment_texts = parts.get("attachment_texts", "")

    # 拼接附件内容
    if attachment_texts:
        prompt += f"\n\n## 附件内容\n{attachment_texts}"

    # 保存「不含 MCP 引导语」的原始提示词，供工具循环不收敛时回退用（见下方降级逻辑）
    prompt_base = prompt

    # ── MCP 工具连接（如北大法宝法规检索） ──
    mcp_client = None
    mcp_used = False
    if mcp_servers:
        from app.services.mcp_client import MCPClient, parse_server_configs
        parsed = parse_server_configs(mcp_servers)
        if parsed:
            try:
                mcp_client = await MCPClient(parsed).__aenter__()
                if mcp_client.has_tools():
                    prompt += "\n\n" + _MCP_USAGE_INSTRUCTION
                    logger.info(
                        "MCP 已启用：发现 %d 个工具（服务器：%s）",
                        len(mcp_client.tools), ", ".join(mcp_client.connected_servers),
                    )
                else:
                    logger.warning("MCP 服务器已连接但未发现工具，本次分析不使用工具")
                    mcp_client = None
            except Exception as e:
                logger.warning("MCP 初始化失败，本次分析不使用工具: [%s] %s", type(e).__name__, str(e)[:200])
                if mcp_client is not None:
                    try:
                        await mcp_client.__aexit__(None, None, None)
                    except Exception:
                        pass
                mcp_client = None

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

    messages = [
        {"role": "system", "content": f"你是一位资深法律文书分析专家。文书类型已由系统识别阶段确定，请按提示词中嵌入的类型专属分析流程进行解读与审核，无需重新判断文书类型。严格按JSON格式返回分析结果和修订文书，不要包含markdown代码块标记。禁止输出分析过程、思考步骤或推理说明，直接输出JSON。\n\n重要：当前真实日期是 {date.today().isoformat()}（这是今天的实际日期，你仅需据此计算时效和截止日等时间）"},
        user_message,
    ]

    payload = {
        "model": model_name,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    use_tools = mcp_client is not None and mcp_client.has_tools()
    if use_tools:
        payload["tools"] = mcp_client.list_openai_tools()

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            max_rounds = (mcp_max_turns or 5) + 1 if use_tools else 1
            for _ in range(max_rounds):
                response = await client.post(api_url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                msg = data["choices"][0]["message"]

                tool_calls = msg.get("tool_calls")
                if not tool_calls:
                    content = (msg.get("content") or "").strip()
                    break

                # 工具调用轮：执行 MCP 工具并把结果回填给模型
                mcp_used = True
                logger.info("LLM 请求调用 %d 个 MCP 工具", len(tool_calls))
                messages.append({
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": tool_calls,
                })
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    if not name:
                        continue
                    result_text = await mcp_client.call(name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": result_text,
                    })
                payload["messages"] = messages
            else:
                # 达到最大轮次仍无最终内容
                content = ""
                logger.warning("MCP 工具调用达到最大轮次 %d，未获得最终 JSON 内容", mcp_max_turns or 5)

            # MCP 工具循环未收敛（模型持续请求工具但始终不输出最终 JSON）的优雅降级：
            # 用「不含工具定义、不含 MCP 引导语」的原始消息重跑一次普通分析，
            # 保证 MCP 失败 / 模型不收敛不会让整篇文书分析失败（回归到 MCP 之前的可用行为）。
            if use_tools and not content:
                logger.warning(
                    "MCP 工具循环在 %d 轮内未收敛，回退为不使用工具的普通分析（避免整篇分析失败）",
                    (mcp_max_turns or 5),
                )
                if use_vision:
                    fallback_user = {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_base},
                            *[p for p in messages[1]["content"] if p.get("type") == "image_url"],
                        ],
                    }
                else:
                    fallback_user = {"role": "user", "content": prompt_base}
                payload = {
                    "model": model_name,
                    "messages": [messages[0], fallback_user],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                response = await client.post(api_url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                content = (data["choices"][0]["message"].get("content") or "").strip()
                mcp_used = False
    finally:
        if mcp_client is not None:
            try:
                await mcp_client.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("MCP 客户端关闭异常: %s", e)


    # 清理可能的 markdown 代码块标记
    if content.startswith("```"):
        # 移除 ```json 和结尾 ```
        content = content.split("\n", 1)[-1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
    content = content.strip()

    try:
        result = json.loads(content)
        return _finalize_with_citations(_validate_llm_output(result), mcp_used, mcp_client)
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
            return _finalize_with_citations(validated, mcp_used, mcp_client)
        except (json.JSONDecodeError, ValueError):
            pass

        # 尝试从内容中提取 JSON 对象
        import re
        match = re.search(r"\{[\s\S]*\}", content)
        if match:
            try:
                result = json.loads(match.group())
                return _finalize_with_citations(_validate_llm_output(result), mcp_used, mcp_client)
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

易混淆类型的区分规则（优先按以下边界判断，不要因为文书带有「通知书」「答复书」等字样就归入其他类型）：
- 信访件：与信访事项有关的一切文书，包括信访件基本情况登记表、信访事项受理告知书、信访事项处理意见书（答复书）、复查/复核意见书，以及文号含「访答」「信访」「信复」等信访专用字号的文书。凡属于信访渠道办理的告知、答复、意见类文书，一律归「信访件」，不要归入「通知书」。
- 政府信息公开：围绕「政府信息公开申请」作出的答复，包括政府信息公开申请答复书、政府信息公开告知书、不予公开决定书，以及文号含「信息公开」字样的文书。其申请人主张的是获取特定政府信息，而非信访诉求。
- 履职申请：申请人要求行政机关查处违法行为、履行保护人身权、财产权等法定职责的文书，包括履职申请书、要求履行法定职责的申请、查处违法行为的申请，以及机关作出的履职答复。凡主张指向「要求机关采取执法行动」，而非索取既存信息或反映情况，一律归「履职申请」。
- 咨询：要求解答政策疑问、进行判断分析的文书（不指向既存信息，也不要求执法），包括政策咨询件、12345 咨询类工单及其答复。其典型特征是请求机关对事实或法律问题作出解释、答疑。
- 投诉举报：反映违法行为要求查处的文书（投诉为维护自身合法权益，举报为公益性），包括投诉书、举报信、举报材料及查处结果告知，常见于劳动监察、安全生产、建设工程质量、招投标、自然资源违法等领域。
- 通知书：仅指诉讼、仲裁、行政执法等程序中的程序性告知文书，如应诉通知书、举证通知书、开庭通知书、行政处罚告知书、催告书等，文号通常为「XX通字」「XX告知字」。
- 上述五个入口类型（政府信息公开／履职申请／信访件／咨询／投诉举报）的界分以**申请人主张的实质内容**为准：要既存信息→政府信息公开；要执法行动→履职申请（自身权益受损的查处请求亦可归投诉举报）；要政策答疑→咨询；反映情况、提建议意见或已穷尽法定途径→信访件。不得因文书带有「答复书」「告知书」字样就归入「通知书」。
- 当一份文书同时具备多个类型特征时，以文书的**文书名称与文号**为第一判断依据，其次才是正文内容。

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

    候选类型来自 分析提示词/ 目录下的文件清单；仅要求 LLM 返回类型与置信度。
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
    context_window: int = 0,
    usage_ratio: float = 0.50,
    token_method: str = "approximate",
    max_tokens: int = _CLASSIFY_MIN_MAX_TOKENS,
) -> dict:
    """第一阶段：调用类型识别 LLM 判断文书类型。

    custom_prompt 为自定义分类提示词模板（LLM 配置 analysis_prompt），为空时使用默认分类模板。

    max_tokens: 分类请求的输出上限。注意「先推理后回答」的模型（如 DeepSeek-V4-Flash、
    o 系列）会把推理过程也计入该预算，预算过小会导致 JSON 尚未输出即被截断，
    因此这里强制不低于 _CLASSIFY_MIN_MAX_TOKENS。

    返回 {"doc_type": str, "confidence": float}；任何失败（含密钥解密失败）
    均回退 {"doc_type": "其他法律文书", "confidence": 0.5, "failed": True}，不中断主流程。
    failed=True 表示这是识别失败而非模型判断，调用方不得据此路由。
    """
    try:
        api_key = decrypt(api_key_encrypted)
        template_prompt = build_classify_prompt(subject, sender, body,
                                                attachment_texts="",
                                                body_max_chars=body_max_chars,
                                                custom_prompt=custom_prompt)
        # 分类阶段预算：分类模板无 ## Output marker，truncate 只会触发 L1 附件丢弃，
        # 保证大附件下类型识别请求不超窗（analyze 阶段已有预算兜底）。
        if context_window > 0 and attachment_texts:
            parts = truncate_prompt_parts(
                {"template_with_body": template_prompt, "attachment_texts": attachment_texts},
                context_window, usage_ratio, 100, token_method,
            )
            template_prompt = parts["template_with_body"]
            attachment_texts = parts.get("attachment_texts", "")
        prompt = template_prompt
        if attachment_texts:
            prompt += f"\n\n## 附件内容\n{attachment_texts}"

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
            "max_tokens": max(_CLASSIFY_MIN_MAX_TOKENS, int(max_tokens or 0)),
            "temperature": 0,
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(api_url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
        finish_reason = (data["choices"][0].get("finish_reason") or "").strip()
        if finish_reason == "length":
            logger.error(
                f"类型识别响应被截断（finish_reason=length，max_tokens={payload['max_tokens']}）："
                f"推理型模型的推理过程占满了输出预算，JSON 未输出。"
            )
    except Exception as e:
        logger.error(
            f"文书类型识别失败，回退「其他法律文书」（失败兜底值，非模型判断）: "
            f"[{type(e).__name__}] {str(e)[:200]}"
        )
        return {"doc_type": "其他法律文书", "confidence": 0.5, "failed": True}

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
        logger.error(
            f"类型识别响应解析失败，回退「其他法律文书」（失败兜底值，非模型判断）: "
            f"[{type(e).__name__}] {str(e)[:200]}"
        )
        return {"doc_type": "其他法律文书", "confidence": 0.5, "failed": True}


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
    body_max_chars: int = 8000,
    timeout: int = 180,
    context_window: int = 0,
    usage_ratio: float = 0.50,
    token_method: str = "approximate",
    classifier_cfg: dict = None,
    mcp_servers: list = None,
    mcp_max_turns: int = 5,
    classifier_max_tokens: int = _CLASSIFY_MIN_MAX_TOKENS,
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
        context_window=context_window,
        usage_ratio=usage_ratio,
        token_method=token_method,
        max_tokens=classifier_max_tokens,
    )
    doc_type = classify_result.get("doc_type", "其他法律文书")
    classifier_confidence = classify_result.get("confidence", 0.5)
    classify_failed = bool(classify_result.get("failed"))
    if classify_failed:
        logger.warning(
            f"第一阶段类型识别失败，本次 doc_type=「{doc_type}」为兜底值而非模型判断，"
            f"该结果不可用于路由决策"
        )
    else:
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
            "classify_failed": classify_failed,
            # 审查意见模板填充字段（非法律文书不出具审查意见，留空）
            "contract_party_a": "",
            "contract_party_b": "",
            "contract_name": "",
            "contract_content": "",
            "contract_amount": "",
            "agency_name": "",
            "document_title_no": "",
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
        body_max_chars=body_max_chars,
        timeout=timeout,
        context_window=context_window,
        usage_ratio=usage_ratio,
        token_method=token_method,
        analysis_instructions=analysis_instructions,
        mcp_servers=mcp_servers,
        mcp_max_turns=mcp_max_turns,
        doc_type=doc_type,
    )

    # 合并：doc_type/confidence 以第一阶段识别结果为准（保证路由与日志稳定）
    if analysis and isinstance(analysis, dict):
        if classify_failed:
            # 第一阶段失败时 doc_type 是兜底值，第二阶段给出的类型反而更有信息量，
            # 但不能用它替代识别结果（它是按兜底类型选提示词得出的，不具备独立识别意义）。
            if analysis.get("doc_type") != doc_type:
                logger.warning(
                    f"第一阶段类型识别失败（兜底「{doc_type}」），第二阶段返回「{analysis.get('doc_type')}」"
                    f"仅供参考，不作为路由依据"
                )
        elif analysis.get("doc_type") != doc_type:
            logger.warning(
                f"第二阶段返回文书类型「{analysis.get('doc_type')}」与第一阶段「{doc_type}」不一致，以第一阶段为准"
            )
        analysis["doc_type"] = doc_type
        analysis["confidence"] = classifier_confidence
        analysis["classify_failed"] = classify_failed
    return analysis


_VALID_URGENCIES = frozenset({"high", "medium", "low"})


def _finalize_with_citations(validated: dict, mcp_used: bool, mcp_client) -> dict:
    """MCP 使用后，若报告/修改版文书尚未含来源链接，把工具返回的法规链接兜底追加到末尾。

    主要靠提示词让模型自行附加「引用依据」；此处作为兜底，仅当输出中完全没有 pkulaw 链接时补上，
    避免重复，也保证「报告/修改版文书末尾附引用链接」的需求始终达成。
    """
    if not (mcp_used and mcp_client and getattr(mcp_client, "citation_links", None)):
        return validated
    links = list(dict.fromkeys(mcp_client.citation_links[:10]))
    if not links:
        return validated

    ai = validated.get("ai_interpretation") or ""
    if ai and "pkulaw.com" not in ai:
        validated["ai_interpretation"] = (
            ai + "\n\n【引用依据】\n" + "\n".join(f"- {u}" for u in links)
        ).strip()

    rd = validated.get("revised_document")
    if rd and "pkulaw.com" not in str(rd):
        validated["revised_document"] = (
            str(rd) + "\n\n【引用依据】\n" + "\n".join(f"- {u}" for u in links)
        ).strip()
    return validated


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
        # 审查意见模板填充字段：均取自**送审文书正文**，不取邮件标题/附件文件名
        "contract_party_a": str(result.get("contract_party_a", "") or ""),
        "contract_party_b": str(result.get("contract_party_b", "") or ""),
        "contract_name": str(result.get("contract_name", "") or ""),
        "contract_content": str(result.get("contract_content", "") or ""),
        "contract_amount": str(result.get("contract_amount", "") or ""),
        "agency_name": str(result.get("agency_name", "") or ""),
        "document_title_no": str(result.get("document_title_no", "") or ""),
    }


_LLM_ANALYSIS_SCHEMA = frozenset({
    "doc_type", "case_summary", "ai_interpretation", "urgency",
    "key_date", "case_number", "involved_parties", "confidence",
    "revised_document",
    "contract_party_a", "contract_party_b", "contract_name",
    "contract_content", "contract_amount", "agency_name",
    "document_title_no",
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
        # 审查意见模板填充字段（兜底为空，填充时统一显示「（待确认）」）
        "contract_party_a": "",
        "contract_party_b": "",
        "contract_name": "",
        "contract_content": "",
        "contract_amount": "",
        "agency_name": "",
        "document_title_no": "",
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
