"""OCR 识别模块 — 支持 PaddleOCR / OpenAI Vision / MinerU / 自定义"""
import base64
import logging
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



async def detect_model_type(api_url: str, api_key: str, model_name: str) -> str:
    """
    检测模型类型: 'multimodal' | 'text' | 'unknown'

    策略:
    1. 查 /v1/models 看模型名是否包含 vision/vl/multimodal 关键词
    2. 尝试发送一个极小的 vision 请求看是否报错
    """
    # 1. 关键词快速判断
    vl_keywords = ["vision", "vl", "multimodal", "gemini", "claude", "gpt-4o", "gpt-4v", "pixtral", "llava", "qwen-vl", "qwenvl", "cogvlm", "glm-4v", "yi-vision", "deepseek-vl"]
    model_lower = model_name.lower()
    for kw in vl_keywords:
        if kw in model_lower:
            logger.info(f"模型 {model_name} 关键词匹配: {kw} → multimodal")
            return "multimodal"

    # 2. 尝试查询 /v1/models
    try:
        if not api_url.endswith("/chat/completions"):
            base_url = api_url.rstrip("/")
        else:
            base_url = api_url.rsplit("/chat/completions", 1)[0]

        models_url = base_url + "/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(models_url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("id", "") for m in data.get("data", [])]
                for m_id in models:
                    for kw in vl_keywords:
                        if kw in m_id.lower():
                            logger.info(f"模型 {model_name} /v1/models 匹配: {kw} → multimodal")
                            return "multimodal"
    except Exception as e:
        logger.debug(f"/v1/models 查询失败: {e}")

    # 3. 尝试发送极小的 vision 请求
    try:
        # 1x1 透明 PNG base64
        tiny_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        test_payload = {
            "model": model_name,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "say OK"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tiny_png}"}}
                ]
            }],
            "max_tokens": 5,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(api_url, headers=headers, json=test_payload)
            if resp.status_code == 200:
                logger.info(f"模型 {model_name} 支持 vision → multimodal")
                return "multimodal"
            else:
                logger.info(f"模型 {model_name} 不支持 vision (HTTP {resp.status_code}) → text")
                return "text"
    except Exception as e:
        logger.info(f"模型 {model_name} vision 检测失败: {e} → text")
        return "text"

    return "text"


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
    api_url = ocr_cfg["api_url"].rstrip("/")
    if not api_url.endswith("/chat/completions"):
        api_url += "/chat/completions"

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
