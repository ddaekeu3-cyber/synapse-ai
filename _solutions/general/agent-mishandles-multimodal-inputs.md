---
layout: solution
title: "Agent Mishandles Multimodal Inputs — Images and PDFs Processed Incorrectly"
category: general
description: "An agent receives images or PDFs but passes them incorrectly to the API — using wrong content types, not resizing large images before sending, passing PDFs as raw bytes instead of base64, or ignoring the image entirely and responding from text context alone. The result is errors, truncated content, excessive token costs, or silent failures where the agent pretends to see an image it never received."
tags: [multimodal, vision, images, pdf, base64, content-types, token-cost, api-usage]
---

# Agent Mishandles Multimodal Inputs — Images and PDFs Processed Incorrectly

## Symptom
- API error: `"invalid content type"` or `"image too large"` when passing user-uploaded images
- Agent says "I can see the image shows..." but was never actually given the image data
- PDFs are passed as raw bytes instead of properly encoded base64 — API rejects them
- Large screenshots consume 2000+ tokens per image due to tile-based pricing
- JPEG uploaded by user is re-encoded as PNG, tripling the file size before sending
- Agent silently ignores an attached image and responds only from the text prompt
- Multiple images in one request: agent confuses which image is which

## Root Cause
The Claude API accepts images as base64-encoded data URIs with explicit media types, or as URLs. Common mistakes: passing raw bytes, using the wrong content block type, not specifying the media type, or building the message structure incorrectly. Large images are expensive because Claude uses a tile-based vision system — a 4096×4096 image costs ~4× more than a 1024×1024 one covering the same content. The fix is to normalize all image inputs at the boundary: resize to task-appropriate resolution, convert to the right format, and build the content block correctly.

## Fix

### Option 1: Correct image content block structure — base64 and URL

```python
import anthropic
import base64
import httpx
from pathlib import Path

client = anthropic.Anthropic()

def load_image_as_base64(path: str) -> tuple[str, str]:
    """
    Load an image from disk and return (base64_data, media_type).
    """
    path_obj = Path(path)
    suffix = path_obj.suffix.lower()
    media_type_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    media_type = media_type_map.get(suffix, "image/jpeg")
    raw = path_obj.read_bytes()
    return base64.standard_b64encode(raw).decode("utf-8"), media_type


def image_block_from_file(path: str) -> dict:
    """Build a valid image content block from a local file."""
    data, media_type = load_image_as_base64(path)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,   # REQUIRED — must be one of the 4 supported types
            "data": data
        }
    }


def image_block_from_url(url: str) -> dict:
    """Build a valid image content block from a URL."""
    return {
        "type": "image",
        "source": {
            "type": "url",
            "url": url   # Must be publicly accessible
        }
    }


def analyze_image(image_path_or_url: str, question: str = "Describe what you see.") -> str:
    """
    Send an image to Claude with a question.
    Handles both local files and URLs.
    """
    if image_path_or_url.startswith("http"):
        image_block = image_block_from_url(image_path_or_url)
    else:
        image_block = image_block_from_file(image_path_or_url)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                image_block,
                {"type": "text", "text": question}
            ]
        }]
    )
    return response.content[0].text


# Multi-image request — label each image so the model doesn't confuse them:
def compare_images(image_paths: list[str], comparison_question: str) -> str:
    content = []
    for i, path in enumerate(image_paths):
        content.append({"type": "text", "text": f"Image {i + 1}:"})
        content.append(image_block_from_file(path))

    content.append({"type": "text", "text": comparison_question})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": content}]
    )
    return response.content[0].text
```

### Option 2: Image preprocessing — resize before sending to reduce token cost

```python
import anthropic
import base64
import io
from pathlib import Path
from enum import Enum

client = anthropic.Anthropic()

class VisionTask(str, Enum):
    """Task types with appropriate resolution targets."""
    READ_TEXT = "read_text"         # OCR — needs high resolution
    IDENTIFY_OBJECTS = "identify"   # Object detection — medium resolution sufficient
    SCREENSHOT_UI = "screenshot"    # UI analysis — medium resolution
    CHART_GRAPH = "chart"           # Chart reading — medium-high
    THUMBNAIL = "thumbnail"         # Quick glance — low resolution

TASK_MAX_DIMENSION = {
    VisionTask.READ_TEXT: 2048,     # keep high for OCR accuracy
    VisionTask.IDENTIFY_OBJECTS: 1024,
    VisionTask.SCREENSHOT_UI: 1568,
    VisionTask.CHART_GRAPH: 1568,
    VisionTask.THUMBNAIL: 512,
}

# Claude tile-based pricing:
# Images are processed in 512×512 tiles (at base) or 1092×1092 (at high detail)
# Each tile costs ~170 tokens
def estimate_tokens(width: int, height: int) -> int:
    """Estimate token cost for an image at given dimensions."""
    tiles_w = (width + 511) // 512
    tiles_h = (height + 511) // 512
    return tiles_w * tiles_h * 170


def resize_image(
    image_data: bytes,
    max_dimension: int,
    output_format: str = "JPEG",
    quality: int = 85
) -> tuple[bytes, str]:
    """
    Resize image to fit within max_dimension on the longest side.
    Returns (resized_bytes, media_type).
    """
    try:
        from PIL import Image
    except ImportError:
        raise ImportError("pip install Pillow")

    img = Image.open(io.BytesIO(image_data))

    # Convert RGBA/P to RGB for JPEG compatibility:
    if img.mode in ("RGBA", "P") and output_format == "JPEG":
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > max_dimension:
        ratio = max_dimension / max(w, h)
        new_w = int(w * ratio)
        new_h = int(h * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        print(f"  [resize] {w}×{h} → {new_w}×{new_h} "
              f"(tokens: {estimate_tokens(w, h)} → {estimate_tokens(new_w, new_h)})")

    buf = io.BytesIO()
    img.save(buf, format=output_format, quality=quality)
    media_type = "image/jpeg" if output_format == "JPEG" else "image/png"
    return buf.getvalue(), media_type


def send_image_optimized(
    image_data: bytes,
    prompt: str,
    task: VisionTask = VisionTask.IDENTIFY_OBJECTS
) -> str:
    """
    Send an image to Claude after resizing to the task-appropriate resolution.
    """
    max_dim = TASK_MAX_DIMENSION[task]
    resized, media_type = resize_image(image_data, max_dimension=max_dim)
    encoded = base64.standard_b64encode(resized).decode("utf-8")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": encoded}
                },
                {"type": "text", "text": prompt}
            ]
        }]
    )
    return response.content[0].text


# Example: screenshot analysis with appropriate resolution
# raw = Path("screenshot.png").read_bytes()
# result = send_image_optimized(raw, "What buttons are visible?", task=VisionTask.SCREENSHOT_UI)
```

### Option 3: PDF handling — extract pages as images for Claude

```python
import anthropic
import base64
import io

client = anthropic.Anthropic()

def pdf_page_to_image(pdf_data: bytes, page_number: int = 0, dpi: int = 150) -> tuple[bytes, str]:
    """
    Convert a PDF page to a JPEG image for Claude.
    Uses pdf2image (requires poppler): pip install pdf2image
    """
    try:
        from pdf2image import convert_from_bytes
    except ImportError:
        raise ImportError("pip install pdf2image  # also requires poppler-utils")

    pages = convert_from_bytes(pdf_data, dpi=dpi, first_page=page_number + 1, last_page=page_number + 1)
    if not pages:
        raise ValueError(f"PDF page {page_number} not found")

    page = pages[0]
    # Resize to fit within 2048px if larger:
    w, h = page.size
    if max(w, h) > 2048:
        ratio = 2048 / max(w, h)
        page = page.resize((int(w * ratio), int(h * ratio)))

    buf = io.BytesIO()
    page.save(buf, format="JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


def analyze_pdf(pdf_data: bytes, prompt: str, max_pages: int = 5) -> str:
    """
    Analyze a PDF by converting pages to images.
    Processes up to max_pages pages.
    """
    try:
        from pdf2image import convert_from_bytes
        pages = convert_from_bytes(pdf_data, dpi=100)
        total_pages = len(pages)
    except ImportError:
        return "PDF processing requires: pip install pdf2image"

    print(f"PDF has {total_pages} pages; processing up to {max_pages}")
    pages_to_process = min(total_pages, max_pages)

    content = [{"type": "text", "text": f"This PDF has {total_pages} pages. Analyzing pages 1–{pages_to_process}."}]

    for i in range(pages_to_process):
        img_data, media_type = pdf_page_to_image(pdf_data, page_number=i, dpi=150)
        encoded = base64.standard_b64encode(img_data).decode("utf-8")
        content.append({"type": "text", "text": f"Page {i + 1}:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": encoded}
        })

    content.append({"type": "text", "text": prompt})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": content}]
    )
    return response.content[0].text
```

### Option 4: Image validation — reject bad inputs before sending to API

```python
import anthropic
import base64
import io
from pathlib import Path

client = anthropic.Anthropic()

SUPPORTED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024   # 20 MB (API limit)
MAX_IMAGE_DIMENSION = 8000                 # pixels per side

class ImageValidationError(ValueError):
    pass


def validate_and_prepare_image(image_data: bytes) -> tuple[str, str]:
    """
    Validate image and prepare for API submission.
    Returns (base64_data, media_type) or raises ImageValidationError.
    """
    if len(image_data) > MAX_IMAGE_SIZE_BYTES:
        raise ImageValidationError(
            f"Image too large: {len(image_data) / 1024 / 1024:.1f} MB "
            f"(max {MAX_IMAGE_SIZE_BYTES / 1024 / 1024:.0f} MB)"
        )

    # Detect actual media type from file signature:
    import imghdr
    detected = imghdr.what(None, h=image_data)
    media_type_map = {"jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
    media_type = media_type_map.get(detected)

    if not media_type:
        raise ImageValidationError(
            f"Unsupported image format: {detected!r}. "
            f"Supported: JPEG, PNG, GIF, WebP"
        )

    # Check dimensions:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_data))
        w, h = img.size
        if max(w, h) > MAX_IMAGE_DIMENSION:
            raise ImageValidationError(
                f"Image dimensions {w}×{h} exceed maximum {MAX_IMAGE_DIMENSION}px. "
                f"Please resize before submitting."
            )
    except ImportError:
        pass  # Skip dimension check if PIL not available

    return base64.standard_b64encode(image_data).decode("utf-8"), media_type


def safe_vision_call(image_data: bytes, prompt: str) -> dict:
    """
    Vision API call with full validation.
    Returns result or descriptive error — never an opaque API error.
    """
    try:
        encoded, media_type = validate_and_prepare_image(image_data)
    except ImageValidationError as e:
        return {"error": str(e), "success": False}

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}},
                    {"type": "text", "text": prompt}
                ]
            }]
        )
        return {
            "success": True,
            "result": response.content[0].text,
            "input_tokens": response.usage.input_tokens
        }
    except anthropic.BadRequestError as e:
        return {"error": f"API rejected image: {e}", "success": False}
```

### Option 5: Async image batch processing — multiple images efficiently

```python
import anthropic
import asyncio
import base64
from pathlib import Path

client = anthropic.AsyncAnthropic()

async def analyze_image_async(
    image_path: str,
    prompt: str,
    semaphore: asyncio.Semaphore
) -> dict:
    """Analyze one image with concurrency control."""
    async with semaphore:
        try:
            raw = Path(image_path).read_bytes()
            encoded = base64.standard_b64encode(raw).decode("utf-8")
            suffix = Path(image_path).suffix.lower()
            media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                          "png": "image/png", "webp": "image/webp"}.get(suffix[1:], "image/jpeg")

            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",  # Haiku for bulk image classification
                max_tokens=128,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}},
                        {"type": "text", "text": prompt}
                    ]
                }]
            )
            return {
                "image": image_path,
                "result": response.content[0].text,
                "input_tokens": response.usage.input_tokens,
                "success": True
            }
        except Exception as e:
            return {"image": image_path, "error": str(e), "success": False}


async def batch_analyze_images(
    image_paths: list[str],
    prompt: str,
    max_concurrent: int = 5
) -> list[dict]:
    """
    Analyze multiple images concurrently.
    max_concurrent limits simultaneous API calls.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = [
        analyze_image_async(path, prompt, semaphore)
        for path in image_paths
    ]
    results = await asyncio.gather(*tasks)
    successes = sum(1 for r in results if r["success"])
    total_tokens = sum(r.get("input_tokens", 0) for r in results)
    print(f"Processed {successes}/{len(image_paths)} images, {total_tokens} total tokens")
    return list(results)


# Usage:
# results = asyncio.run(batch_analyze_images(
#     ["img1.jpg", "img2.png", "img3.webp"],
#     prompt="What product is shown in this image? One word answer."
# ))
```

### Option 6: Image caching — avoid re-encoding and re-sending identical images

```python
import anthropic
import base64
import hashlib
import time
from pathlib import Path

client = anthropic.Anthropic()

class ImageCache:
    """
    Cache image base64 encoding by file hash.
    Avoids re-reading and re-encoding the same image across multiple calls.
    Also caches API responses to avoid re-analyzing identical images.
    """
    def __init__(self, response_ttl: int = 3600):
        self._encoded: dict[str, tuple[str, str]] = {}  # hash → (base64, media_type)
        self._responses: dict[str, tuple[str, float]] = {}  # (hash, prompt_hash) → (response, ts)
        self._response_ttl = response_ttl

    def _file_hash(self, path: str) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]

    def get_encoded(self, path: str) -> tuple[str, str]:
        """Get or compute base64 encoding for an image file."""
        h = self._file_hash(path)
        if h not in self._encoded:
            raw = Path(path).read_bytes()
            suffix = Path(path).suffix.lower()
            media_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                          ".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/jpeg")
            self._encoded[h] = (base64.standard_b64encode(raw).decode("utf-8"), media_type)
        return self._encoded[h]

    def get_cached_response(self, path: str, prompt: str) -> str | None:
        """Return cached API response if available and fresh."""
        img_hash = self._file_hash(path)
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        key = f"{img_hash}:{prompt_hash}"
        if key in self._responses:
            response, ts = self._responses[key]
            if time.time() - ts < self._response_ttl:
                return response
        return None

    def cache_response(self, path: str, prompt: str, response: str) -> None:
        img_hash = self._file_hash(path)
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        key = f"{img_hash}:{prompt_hash}"
        self._responses[key] = (response, time.time())

    def analyze(self, image_path: str, prompt: str) -> dict:
        """Analyze image with caching at both encoding and response levels."""
        cached = self.get_cached_response(image_path, prompt)
        if cached:
            return {"result": cached, "from_cache": True}

        encoded, media_type = self.get_encoded(image_path)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}},
                    {"type": "text", "text": prompt}
                ]
            }]
        )
        result = response.content[0].text
        self.cache_response(image_path, prompt, result)
        return {
            "result": result,
            "from_cache": False,
            "input_tokens": response.usage.input_tokens
        }


cache = ImageCache()
# First call: encodes + calls API
# r1 = cache.analyze("product.jpg", "What color is this product?")
# Second call with same image + prompt: returns cache, zero tokens
# r2 = cache.analyze("product.jpg", "What color is this product?")
```

## Common Multimodal Mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Wrong `source.type` | API error 400 | Use `"base64"` or `"url"`, not `"file"` or `"path"` |
| Missing `media_type` | API error | Always specify `"image/jpeg"`, `"image/png"`, etc. |
| Raw bytes not base64 | API rejects body | Use `base64.standard_b64encode(data).decode()` |
| Image URL not public | API can't fetch | Use base64 encoding for private images |
| 4000px image sent raw | 8000+ tokens per image | Resize to task-appropriate resolution first |
| RGBA PNG for JPEG | PIL error or wrong encoding | Convert to RGB before JPEG encoding |
| PDF as binary | API doesn't accept PDF directly | Convert pages to JPEG images first |
| No image label in multi-image | Model confuses images | Prefix each with "Image N:" text block |

## Expected Token Savings
Resizing a 3024×4032 phone photo to 1024×1367 before sending reduces vision tokens from ~5100 to ~580 — a 9× reduction with minimal quality loss for most tasks. For bulk image classification, this saves ~88% of vision token costs.

## Environment
- Any agent accepting user-uploaded images or PDFs; critical for document processing, UI automation, and visual QA pipelines; always resize before sending (Option 2) — the default phone camera image (3-12 MP) is 3–10× more expensive than needed for most tasks; validate before sending (Option 4) to prevent opaque API errors from reaching users; use Haiku for bulk classification tasks (Option 5) — vision quality is similar at 1/10th the cost
