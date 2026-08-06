"""OCR 识别模块 — 支持 PaddleOCR / OpenAI Vision / MinerU / 自定义"""
import base64
import logging
import random
import re
import struct
import zlib
import httpx

logger = logging.getLogger(__name__)


async def ocr_image(
    image_bytes: bytes,
    ocr_cfg: dict,
    filename: str = "image.png",
) -> str:
    """
    对单张图片执行 OCR

    ocr_cfg = {
        "provider_type": "paddleocr" | "openai-vision" | "custom",
        "api_url": str,
        "api_key": str (decrypted),
        "model_name": str (for vision),
    }
    返回识别的文本
    """
    provider = ocr_cfg.get("provider_type", "paddleocr")

    if provider == "paddleocr":
        return await _ocr_paddleocr(image_bytes, ocr_cfg["api_url"])
    elif provider == "openai-vision":
        return await _ocr_openai_vision(image_bytes, ocr_cfg, filename)
    elif provider == "mineru":
        return await _ocr_mineru(image_bytes, ocr_cfg.get("api_url", ""), filename)
    else:
        logger.warning(f"Unknown OCR provider: {provider}")
        return ""



# ============================================================
# 模型类型检测
# ============================================================
#
# 设计原则：判定的不是"这个模型权重理论上是否多模态"，而是
# "这个部署端到端能不能真的看见图片"。
#
# 误判为 multimodal 的后果最严重：图片会被塞进请求、被后端静默
# 丢弃，模型看不见却照常输出结论（无声的错误）。因此证据不足时
# 一律返回 unknown（走 OCR 路径），绝不乐观推断。
#
# 名称/元数据关键词只作为**参考信息**写入 detail 供人工判断，
# 不参与判定 —— 这样根除了"平台上装了别的 VL 模型就把整台机器
# 上所有配置都判成多模态"的缺陷。

VISION_PROBE_ROUNDS = 2  # 辨色探测轮数；每多一轮，瞎猜蒙混概率降 1/4

_VL_RE = re.compile(
    r"(?<![a-z0-9])("
    r"vl|vlm|vision|multimodal|omni|"
    r"gpt-4o|gpt-4v|glm-4v|yi-vision|"
    r"pixtral|llava|cogvlm|internvl|minicpm-v|"
    r"qwen-?vl|deepseek-vl|claude-3|gemini|"
    # OCR/文档解析类模型（oMLX 平台上常见命名，本身即多模态读图模型）
    r"ocr|mineru(?:\d[\w.]*)?|markitdown|docling|paddleocr|textin|gpt-?ocr|"
    r"qwen-?ocr|ds-?ocr|deepseek-?ocr|unlimited-?ocr"
    r")(?![a-z0-9])"
)

# 纯色探测图：RGB + 可接受的答案别名（中英文）
_PROBE_COLORS = {
    "red": ((220, 20, 20), ("red", "红")),
    "green": ((20, 170, 60), ("green", "绿")),
    "blue": ((20, 60, 220), ("blue", "蓝")),
    "yellow": ((240, 210, 20), ("yellow", "黄")),
}

_PROBE_PROMPT = (
    "这是一张纯色图片。它是什么颜色？"
    "只回答一个英文单词：red、green、blue 或 yellow。"
)

# 后端明确拒绝图片输入时的典型报错关键词
_REJECT_HINTS = (
    "image", "vision", "multimodal", "multi-modal", "image_url",
    "does not support", "not support", "unsupported", "只支持文本", "不支持图",
)


def _api_base(api_url: str) -> str:
    """归一化为 API base（去尾斜杠、剥掉 /chat/completions）"""
    u = (api_url or "").strip().rstrip("/")
    if u.endswith("/chat/completions"):
        u = u[: -len("/chat/completions")]
    return u


def _chat_endpoint(api_url: str) -> str:
    """补全为 /chat/completions 端点。

    历史 bug：探测请求直接 POST 到配置里的 api_url，而库里存的多是
    base 形式（.../v1），导致探测恒定拿到 404/405 并被判成 text。
    """
    return _api_base(api_url) + "/chat/completions"


def _models_endpoint(api_url: str) -> str:
    return _api_base(api_url) + "/models"


def _solid_png(rgb: tuple, size: int = 224) -> bytes:
    """生成纯色 PNG（纯标准库，无需 Pillow）"""
    row = b"\x00" + bytes(rgb) * size
    raw = row * size

    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def _parse_color_answer(text: str) -> str | None:
    """从回答中解析颜色。命中多个不同颜色视为无效（模型在列举/瞎猜）"""
    low = (text or "").lower()
    hits = []
    for name, (_rgb, aliases) in _PROBE_COLORS.items():
        for a in aliases:
            idx = low.find(a)
            if idx >= 0:
                hits.append((idx, name))
                break
    if not hits:
        return None
    if len({n for _i, n in hits}) > 1:
        return None
    return hits[0][1]


def _find_model_entry(models: list, model_name: str) -> dict | None:
    """在 /v1/models 结果中精确定位**当前配置的**那个模型条目"""
    target = (model_name or "").strip().lower()
    if not target:
        return None
    for m in models:
        if str(m.get("id", "")).strip().lower() == target:
            return m
    # 兼容 org/model 前缀差异（如 deepseek-ai/xxx 与 xxx）
    tail = target.rsplit("/", 1)[-1]
    for m in models:
        if str(m.get("id", "")).strip().lower().rsplit("/", 1)[-1] == tail:
            return m
    return None


def _entry_vision_hint(entry: dict) -> bool:
    """检查模型条目自身声明的视觉能力（仅作参考信息）"""
    if _VL_RE.search(str(entry.get("id", "")).lower()):
        return True
    for key in ("capabilities", "modalities", "input_modalities", "architectures", "type"):
        val = entry.get(key)
        if val is None:
            continue
        blob = str(val).lower()
        if "image" in blob or "vision" in blob or "multimodal" in blob:
            return True
    return False


async def _probe_vision_once(
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict,
    model_name: str,
    color_key: str,
) -> dict:
    """单轮辨色探测。

    outcome: 'correct' | 'wrong' | 'unreadable' | 'rejected' | 'error'
    """
    rgb, _aliases = _PROBE_COLORS[color_key]
    b64 = base64.b64encode(_solid_png(rgb)).decode()
    payload = {
        "model": model_name,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROBE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
        "max_tokens": 64,
        "temperature": 0,
    }
    result = {"asked": color_key, "answered": None, "raw": "", "prompt_tokens": None}

    try:
        resp = await client.post(endpoint, headers=headers, json=payload)
    except Exception as e:
        result.update(outcome="error", raw=f"{type(e).__name__}: {e}")
        return result

    if resp.status_code != 200:
        body = resp.text[:300]
        result["raw"] = f"HTTP {resp.status_code}: {body}"
        low = body.lower()
        # 只有 400/422（客户端请求参数校验）且含拒图关键词才确证「拒绝图片」；
        # 409/404/5xx 等属于服务端状态错误（如模型加载失败），不算拒绝图片，
        # 归为 error，避免把「加载失败的模型」误钉死为纯文本。
        if resp.status_code in (400, 422) and any(h in low for h in _REJECT_HINTS):
            result["outcome"] = "rejected"  # 后端明确拒绝图片 → 确认纯文本
        else:
            result["outcome"] = "error"     # 鉴权/路径/服务端错误 → 无法判定
        return result

    try:
        data = resp.json()
        msg = data["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        if not content:
            content = (msg.get("reasoning_content") or "").strip()
        result["prompt_tokens"] = (data.get("usage") or {}).get("prompt_tokens")
    except Exception as e:
        result.update(outcome="error", raw=f"响应解析失败: {type(e).__name__}: {e}")
        return result

    result["raw"] = content[:200]
    answered = _parse_color_answer(content)
    result["answered"] = answered
    if answered is None:
        result["outcome"] = "unreadable"
    elif answered == color_key:
        result["outcome"] = "correct"
    else:
        result["outcome"] = "wrong"
    return result


async def _probe_baseline_tokens(
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict,
    model_name: str,
) -> int | None:
    """发一个**不含图片**的同类请求，记录 prompt_tokens 基线。

    用途：把有图请求的 prompt_tokens 与无图基线对比。两者一致即说明
    图片根本没进入模型上下文——这是"后端静默丢弃图片"的铁证，比"各轮
    token 数相同"可靠（同一张图两轮 token 数本来就该相同）。
    """
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "say ok"}],
        "max_tokens": 5,
        "temperature": 0,
    }
    try:
        resp = await client.post(endpoint, headers=headers, json=payload)
        if resp.status_code == 200:
            return (resp.json().get("usage") or {}).get("prompt_tokens")
    except Exception:
        pass
    return None


def _classify_model_type(
    entry_vision: bool | None,
    name_hint: bool,
    probes: list,
    rounds: int,
    colors: list,
    baseline_tokens: int | None = None,
) -> tuple[str, str]:
    """综合「模型自身元数据 + 端到端辨色探测」给出判定（纯函数，便于单测）。

    判定优先级：
      1. 后端明确拒绝图片                  → text（确证纯文本）
      2. 端到端确认看见图（多轮全对且互异）→ multimodal
      3. 探测连不通                       → 信任模型元数据（有视觉声明则 multimodal，否则 unknown）
      4. 元数据确认多模态、但探测没看见    → multimodal（oMLX 常静默丢弃图片）
      5. 其余（探测没看见且无视觉信号）    → unknown（证据不足，一律不谎报）

    关键点：第 4 条修复了「纯端到端探测在 oMLX 上对所有模型都失效」的回归——
    oMLX 对图片静默忽略，导致真多模态模型的辨色探测也答非所问，必须回退到模型
    自身条目声明的视觉能力，否则会把真多模态误杀成纯文本。扩展后的 _VL_RE
    覆盖 OCR/文档解析类命名（DeepSeek-OCR-2、MinerU2.5 等），使这类模型能
    通过第 4 条正确识别。

    第 5 条：oMLX 这类后端对图片**静默丢弃**且 /v1/models 不暴露视觉能力时，
    真多模态与纯文本模型在行为上完全一样，自动检测区分不了；此时若谎报 text
    会误导用户。正确做法是返回 unknown（消费端与 text 一样走 OCR 路径，安全），
    引导用户手动指定。绝不乐观推断。
    """
    outcomes = [p["outcome"] for p in probes]
    answers = [p["answered"] for p in probes]
    token_counts = [p["prompt_tokens"] for p in probes if p["prompt_tokens"] is not None]

    saw_image = all(o == "correct" for o in outcomes) and len(set(answers)) == len(answers)
    rejected = any(o == "rejected" for o in outcomes)
    all_error = all(o == "error" for o in outcomes)
    # 图片是否真正进入上下文：有图请求与无图基线 token 数一致 → 后端静默丢弃
    image_dropped = (
        baseline_tokens is not None
        and len(token_counts) > 0
        and all(t == baseline_tokens for t in token_counts)
    )

    if rejected:
        return "text", "后端明确拒绝图片输入，确认为纯文本模型。"
    if saw_image:
        # 双保险：若"全对"但 token 数与无图基线一致，说明图片没进上下文，
        # 答案为盲猜概率性命中（约 1/16），不能判 multimodal。
        if image_dropped:
            return "unknown", (
                f"{rounds} 轮辨色虽全对，但有图请求 prompt_tokens 与无图基线一致（均为 {baseline_tokens}），"
                f"图片未真正进入上下文，答案为盲猜概率性命中，判定为未知；如需启用原生视觉请在编辑表单手动指定。"
            )
        return "multimodal", (
            f"{rounds} 轮随机辨色全部答对且答案互异（{'、'.join(colors)}），确认可读取图片内容。"
        )
    if all_error:
        if entry_vision or name_hint:
            tail = (probes[-1]["raw"] or "无响应")[:80]
            return "multimodal", (
                f"视觉探测未能完成（{tail}），但模型条目/名称声明支持视觉，按多模态处理；"
                "建议运维确认后端是否已启用视觉通路。"
            )
        return "unknown", f"视觉探测未能完成（{probes[-1]['raw'][:120]}），无法判定类型。"
    if entry_vision:
        detail = (
            "模型条目声明支持视觉，按多模态处理；端到端辨色未确认（后端可能未启用视觉或静默忽略图片），"
            "已向请求塞入图片，实际能否识别请以运行效果为准。"
        )
        if image_dropped:
            detail += (
                f" 佐证：有图请求 prompt_tokens 与无图基线一致（均为 {baseline_tokens}），"
                "图片可能未真正进入上下文。"
            )
        return "multimodal", detail
    # ── 走到这里：探测既没确认看见图、后端也没声明视觉能力 → 证据不足 ──
    # 绝不谎报纯文本，返回 unknown（消费端同样走 OCR 路径），并给出明确指引。
    if image_dropped:
        return "unknown", (
            f"无法确认视觉能力：有图请求的 prompt_tokens 与无图基线一致（均为 {baseline_tokens}），"
            f"图片未真正进入模型上下文；且模型条目/名称均未声明视觉能力。后端很可能静默丢弃图片——"
            f"如需启用原生视觉，请在编辑表单手动指定类型，并确认后端已启用视觉通路。"
        )
    wrong = [f"{p['asked']}→{p['answered'] or '无法识别'}" for p in probes if p["outcome"] != "correct"]
    return "unknown", (
        f"无法确认视觉能力：辨色探测未通过（{'、'.join(wrong)}），且模型条目/名称均未声明视觉能力。"
        f"请通过编辑表单手动指定类型。"
    )


async def detect_model_type_detailed(
    api_url: str,
    api_key: str,
    model_name: str,
    rounds: int = VISION_PROBE_ROUNDS,
) -> dict:
    """检测模型类型，返回 {'model_type', 'detail', 'evidence'}

    model_type: 'multimodal' | 'text' | 'unknown'
      multimodal — 多轮随机辨色全部答对且答案互异（确认真能看见图）
      text       — 答错/雷同/无法辨认，或后端明确拒绝图片输入
      unknown    — 网络不可达、鉴权失败、服务端错误等，无法判定
    """
    evidence = []

    # ── 参考信息 1：模型名称关键词（词边界匹配，避免 vllm/openvla 误命中）──
    name_hint = bool(_VL_RE.search((model_name or "").lower()))
    evidence.append(f"名称关键词：{'命中' if name_hint else '未命中'}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ── 参考信息 2：只查**本模型**在 /v1/models 里的条目，不扫全平台 ──
    entry_hint = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_models_endpoint(api_url), headers=headers)
            if resp.status_code == 200:
                models = (resp.json() or {}).get("data", []) or []
                entry = _find_model_entry(models, model_name)
                if entry is None:
                    evidence.append(f"模型列表：共 {len(models)} 个模型，未找到同名条目")
                else:
                    entry_hint = _entry_vision_hint(entry)
                    evidence.append(
                        f"模型条目：已定位，视觉能力声明{'存在' if entry_hint else '缺失'}"
                    )
            else:
                evidence.append(f"模型列表：查询失败 HTTP {resp.status_code}")
    except Exception as e:
        evidence.append(f"模型列表：查询异常 {type(e).__name__}")

    # ── 判定依据：多轮随机辨色，验证"图片内容真的被读到" ──
    endpoint = _chat_endpoint(api_url)
    rounds = max(1, min(rounds, len(_PROBE_COLORS)))
    colors = random.sample(list(_PROBE_COLORS.keys()), rounds)
    probes = []
    async with httpx.AsyncClient(timeout=40.0) as client:
        for c in colors:
            probes.append(await _probe_vision_once(client, endpoint, headers, model_name, c))
            if probes[-1]["outcome"] in ("rejected", "error"):
                break  # 明确拒绝或无法连通，无需再试

    for p in probes:
        pt = f"，prompt_tokens={p['prompt_tokens']}" if p["prompt_tokens"] is not None else ""
        evidence.append(
            f"辨色探测 {p['asked']} → {p['answered'] or '无法识别'}（{p['outcome']}{pt}）"
        )

    # ── 无图基线探测：仅在结论不确定时发，用于判断图片是否被后端静默丢弃 ──
    outcomes = [p["outcome"] for p in probes]
    baseline_tokens = None
    if not (any(o == "rejected" for o in outcomes) or all(o == "correct" for o in outcomes) or all(o == "error" for o in outcomes)):
        try:
            async with httpx.AsyncClient(timeout=40.0) as client:
                baseline_tokens = await _probe_baseline_tokens(client, endpoint, headers, model_name)
        except Exception:
            baseline_tokens = None
    if baseline_tokens is not None:
        evidence.append(f"基线探测（无图）：prompt_tokens={baseline_tokens}")

    model_type, detail = _classify_model_type(entry_hint, name_hint, probes, rounds, colors, baseline_tokens)

    logger.info(f"模型 {model_name} 类型检测 → {model_type}｜{detail}")
    return {"model_type": model_type, "detail": detail, "evidence": evidence}


async def detect_model_type(api_url: str, api_key: str, model_name: str) -> str:
    """向后兼容封装：只返回类型字符串"""
    result = await detect_model_type_detailed(api_url, api_key, model_name)
    return result["model_type"]


# ============================================================
# Internal OCR implementations
# ============================================================

async def _ocr_paddleocr(image_bytes: bytes, api_url: str) -> str:
    """调用 PaddleOCR API (AIStudio JSON 格式)"""
    b64 = base64.b64encode(image_bytes).decode()
    payload = {"file": b64, "fileType": 1}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(api_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        # AIStudio 格式: result.ocrResults[].prunedResult.rec_texts
        ocr_results = data.get("result", {}).get("ocrResults", [])
        texts = []
        for ocr in ocr_results:
            pruned = ocr.get("prunedResult", {})
            rec_texts = pruned.get("rec_texts", [])
            rec_scores = pruned.get("rec_scores", [])
            for i, t in enumerate(rec_texts):
                score = rec_scores[i] if i < len(rec_scores) else 0
                if t.strip() and score >= 0.3:
                    texts.append(t.strip())
        return "\n".join(texts)


async def _ocr_openai_vision(image_bytes: bytes, ocr_cfg: dict, filename: str) -> str:
    """调用 OpenAI Vision API 做 OCR"""
    b64 = base64.b64encode(image_bytes).decode()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    mime = f"image/{ext}" if ext in ("png", "jpg", "jpeg", "gif", "webp") else "image/png"

    # 自动补全 /chat/completions
    api_url = _chat_endpoint(ocr_cfg["api_url"])

    payload = {
        "model": ocr_cfg["model_name"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "请识别图片中的所有文字，保持原有格式和段落，直接输出文字内容，不要加任何解释。"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            ]
        }],
        "max_tokens": 2000,
    }
    headers = {
        "Authorization": f"Bearer {ocr_cfg['api_key']}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(api_url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raw = str(data)[:200] if data else "(empty)"
            raise ValueError(f"Vision API 响应格式异常: {raw}") from e


async def _ocr_mineru(image_bytes: bytes, api_url: str, filename: str = "image.png") -> str:
    """调用 MinerU 文档解析服务 (POST /file_parse)"""
    import httpx
    import re
    if not api_url:
        logger.warning("MinerU: api_url 为空")
        return ""

    # 归一化 URL：去除路径中的重复斜杠
    api_url = re.sub(r'(?<!:)//+', '/', api_url)
    base_url = api_url.rstrip("/")
    if not base_url.endswith("/file_parse"):
        base_url += "/file_parse"

    files = {"files": (filename, image_bytes, "image/png")}
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(base_url, files=files)
        resp.raise_for_status()
        data = resp.json()

    if data.get("status") != "completed":
        logger.warning(f"MinerU 解析未完成: status={data.get('status')}")
        return ""

    # 按原始文件名查找结果
    result = data.get("results", {}).get(filename, {})
    md = result.get("md_content", "")
    if not md:
        # MinerU 可能去掉扩展名作为 key
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        for key, val in data.get("results", {}).items():
            if key == stem or key == filename:
                md = val.get("md_content", "")
                break

    logger.info(f"MinerU 解析完成: {filename} ({len(md)} 字符)")
    return md.strip()


async def ocr_pdf(pdf_bytes: bytes, ocr_cfg: dict, filename: str = "document.pdf") -> str:
    """直接提交 PDF 字节给 OCR 服务，返回识别文本。

    适用于支持 PDF 直读的服务（如 MinerU + custom）。
    对 PaddleOCR/OpenAI Vision 等不支持 PDF 的服务，预期会抛异常。

    发送策略按 provider_type:
      - mineru:   multipart POST /file_parse
      - paddleocr: JSON {file: base64(pdf), fileType: 1}（预期失败）
      - openai-vision: data:application/pdf 塞 image_url（预期失败）
      - custom:   同 paddleocr 的 JSON 格式（约定格式）
    """
    provider = ocr_cfg.get("provider_type", "custom")
    api_url = ocr_cfg.get("api_url", "")
    if not api_url:
        logger.warning("ocr_pdf: api_url 为空")
        return ""

    if provider == "mineru":
        return await _ocr_mineru(pdf_bytes, api_url, filename)
    elif provider in ("paddleocr", "custom"):
        # paddleocr 格式: JSON + base64
        b64 = base64.b64encode(pdf_bytes).decode()
        payload = {"file": b64, "fileType": 1}
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(api_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        # 尝试解析返回文本（不同版本 PaddleOCR 格式可能不同）
        try:
            ocr_results = data.get("result", {}).get("ocrResults", [])
            texts = []
            for ocr in ocr_results:
                pruned = ocr.get("prunedResult", {})
                rec_texts = pruned.get("rec_texts", [])
                for t in rec_texts:
                    if t.strip():
                        texts.append(t.strip())
            if texts:
                return "\n".join(texts)
        except Exception:
            pass
        # 兜底：尝试直接读 result.text
        try:
            return data.get("result", {}).get("text", "") or ""
        except Exception:
            return ""
    elif provider == "openai-vision":
        # 作为 data:application/pdf 塞 image_url（预期失败）
        b64 = base64.b64encode(pdf_bytes).decode()
        base_url = _chat_endpoint(api_url)
        payload = {
            "model": ocr_cfg.get("model_name", "gpt-4o"),
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "请识别文档中的所有文字，直接输出内容。"},
                    {"type": "image_url", "image_url": {"url": f"data:application/pdf;base64,{b64}"}},
                ]
            }],
            "max_tokens": 2000,
        }
        headers = {
            "Authorization": f"Bearer {ocr_cfg.get('api_key', '')}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(base_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError):
            return ""
    else:
        logger.warning(f"ocr_pdf: 未知 provider_type {provider}")
        return ""
