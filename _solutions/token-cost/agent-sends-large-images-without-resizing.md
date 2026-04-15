---
layout: solution
title: "Agent Sends Full-Size Images to the API — Wastes Tokens on Unnecessary Resolution"
category: token-cost
description: "The agent sends a 4K screenshot (8MB, ~6,000 tokens) when a 800×600 thumbnail (200KB, ~500 tokens) would answer the same question. It forwards raw camera images, PDFs rendered at 300 DPI, or uncompressed PNGs directly to Claude. Vision tokens are expensive — a 2048×2048 image costs ~1,600 tokens regardless of what it contains."
tags: [token-cost, vision, images, resize, compression, cost-optimization, multimodal, pillow]
---

# Agent Sends Full-Size Images to the API — Wastes Tokens on Unnecessary Resolution

## Symptom
- Vision API calls cost 5–10× more than expected
- Screenshots from 4K monitors use ~6,000 tokens; answers don't require that detail
- Agent sends raw PDF page renders at 300 DPI when Claude only needs to read text
- Image preprocessing step is absent — images are forwarded as-is
- Token usage spikes for vision-heavy tasks, making them uneconomical
- PDF-to-image pipeline sends full A4 pages at maximum resolution

## Root Cause
Claude charges for vision tokens based on image dimensions. A 2048×2048 image costs ~1,600 tokens (Claude's internal tile system). A 4096×4096 image costs ~6,400 tokens. Most vision tasks — reading text, identifying objects, extracting structured data, UI analysis — don't require full resolution. Resizing to the minimum resolution that preserves task-relevant detail, then re-encoding as JPEG at 85% quality, typically reduces vision token cost by 70–90% with no quality loss for the task.

## Fix

### Option 1: Auto-resize before sending — apply max dimension constraint

```python
import anthropic
import base64
import io
from PIL import Image  # pip install pillow
from pathlib import Path

def resize_image_for_claude(
    image_input: bytes | str | Path,
    max_dimension: int = 1568,     # Claude's recommended max for most tasks
    quality: int = 85,             # JPEG quality (85 = excellent, much smaller)
    output_format: str = "JPEG"
) -> tuple[bytes, str]:
    """
    Resize and compress an image to minimize vision tokens.
    Returns (image_bytes, media_type).

    Claude's vision token costs by size:
    - Up to 1092×1092: ~340 tokens (1 tile)
    - Up to 1568×1568: ~1,360 tokens (4 tiles)
    - Up to 2048×2048: ~1,360 tokens (4 tiles) — same as 1568 but larger file
    - 4096×4096: ~6,400 tokens — avoid unless detail is essential
    """
    if isinstance(image_input, (str, Path)):
        img = Image.open(image_input)
    else:
        img = Image.open(io.BytesIO(image_input))

    # Convert to RGB for JPEG (JPEG doesn't support alpha)
    if output_format == "JPEG" and img.mode in ("RGBA", "P", "LA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        img = background
    elif img.mode != "RGB" and output_format == "JPEG":
        img = img.convert("RGB")

    # Resize if larger than max_dimension on either axis
    w, h = img.size
    if max(w, h) > max_dimension:
        if w >= h:
            new_w = max_dimension
            new_h = int(h * max_dimension / w)
        else:
            new_h = max_dimension
            new_w = int(w * max_dimension / h)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    # Compress to bytes
    buf = io.BytesIO()
    if output_format == "JPEG":
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        media_type = "image/jpeg"
    else:
        img.save(buf, format="PNG", optimize=True)
        media_type = "image/png"

    return buf.getvalue(), media_type

def send_image_to_claude(
    image_path: str,
    question: str,
    max_dimension: int = 1568,
    model: str = "claude-sonnet-4-6"
) -> str:
    """Send an image to Claude after resizing for cost efficiency."""
    client = anthropic.Anthropic()

    image_bytes, media_type = resize_image_for_claude(
        image_path,
        max_dimension=max_dimension
    )
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    # Log size reduction
    original_size = Path(image_path).stat().st_size if Path(image_path).exists() else 0
    print(f"Image: {original_size:,}B original → {len(image_bytes):,}B resized "
          f"({100*(1-len(image_bytes)/max(original_size,1)):.0f}% smaller)")

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_b64
                    }
                },
                {"type": "text", "text": question}
            ]
        }]
    )
    return response.content[0].text
```

### Option 2: Task-based resolution presets — different sizes for different tasks

```python
import anthropic
import base64
import io
from PIL import Image
from enum import Enum

class VisionTask(Enum):
    OCR = "ocr"                    # Reading text → needs moderate resolution
    UI_ANALYSIS = "ui_analysis"    # Analyzing UI layout → moderate
    OBJECT_DETECTION = "objects"   # Finding objects → can be lower res
    CHART_READING = "charts"       # Reading chart data → needs some detail
    PHOTO_DESCRIPTION = "photo"    # Describing photos → low res usually fine
    DOCUMENT_ANALYSIS = "document" # Structured docs → moderate res
    DIAGRAM_READING = "diagram"    # Technical diagrams → higher res

# Resolution presets per task type (max dimension):
TASK_RESOLUTION = {
    VisionTask.OCR: 1568,            # Text needs some resolution
    VisionTask.UI_ANALYSIS: 1568,    # UI elements need to be readable
    VisionTask.OBJECT_DETECTION: 800, # Object recognition works at low res
    VisionTask.CHART_READING: 1092,  # Charts need detail but not full res
    VisionTask.PHOTO_DESCRIPTION: 800, # Description doesn't need full detail
    VisionTask.DOCUMENT_ANALYSIS: 1568, # Documents need readable text
    VisionTask.DIAGRAM_READING: 1568,   # Diagrams benefit from resolution
}

# Approximate token costs at each resolution (for reference):
APPROX_TOKENS = {
    800: 170,    # ~170 tokens for 800×600-ish image
    1092: 340,   # ~340 tokens (1 tile)
    1568: 1360,  # ~1360 tokens (4 tiles)
    2048: 1360,  # Same tile count as 1568 — no benefit to going bigger
}

def prepare_image_for_task(
    image_bytes: bytes,
    task: VisionTask,
    jpeg_quality: int = 85
) -> tuple[bytes, str, int]:
    """
    Resize image according to task requirements.
    Returns (optimized_bytes, media_type, approx_tokens).
    """
    max_dim = TASK_RESOLUTION[task]
    img = Image.open(io.BytesIO(image_bytes))

    if img.mode in ("RGBA", "P", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        if img.mode in ("RGBA", "LA"):
            bg.paste(img, mask=img.split()[-1])
        else:
            bg.paste(img)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
    optimized = buf.getvalue()

    # Estimate token cost based on resized dimensions:
    rw, rh = img.size
    max_resized = max(rw, rh)
    approx_tokens = APPROX_TOKENS.get(
        min(APPROX_TOKENS.keys(), key=lambda k: abs(k - max_resized)),
        1360
    )

    return optimized, "image/jpeg", approx_tokens

def analyze_image(
    image_bytes: bytes,
    question: str,
    task: VisionTask = VisionTask.PHOTO_DESCRIPTION,
    model: str = "claude-sonnet-4-6"
) -> dict:
    client = anthropic.Anthropic()
    optimized, media_type, est_tokens = prepare_image_for_task(image_bytes, task)
    img_b64 = base64.standard_b64encode(optimized).decode()

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text", "text": question}
            ]
        }]
    )
    return {
        "answer": response.content[0].text,
        "estimated_image_tokens": est_tokens,
        "actual_input_tokens": response.usage.input_tokens,
        "image_size_bytes": len(optimized)
    }

# Usage examples:
result = analyze_image(screenshot_bytes, "What error is shown?", task=VisionTask.OCR)
result = analyze_image(photo_bytes, "Describe this image", task=VisionTask.PHOTO_DESCRIPTION)
# Object detection → 800px max (170 tokens vs 6400 for 4K = 97% savings)
```

### Option 3: URL-referenced images — skip base64 overhead entirely

```python
import anthropic
from urllib.parse import urlparse

client = anthropic.Anthropic()

def analyze_image_by_url(
    image_url: str,
    question: str,
    model: str = "claude-sonnet-4-6"
) -> str:
    """
    Pass an image URL instead of base64.
    Claude fetches it directly — no base64 encoding overhead in the API request.
    Saves bandwidth and request size. Claude still processes the full image,
    so pair with a CDN that serves resized images for full cost control.
    """
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": image_url
                    }
                },
                {"type": "text", "text": question}
            ]
        }]
    )
    return response.content[0].text

# For CDN-hosted images, use image transformation URLs to resize server-side:
def build_resized_cdn_url(
    original_url: str,
    max_width: int = 1568,
    quality: int = 85,
    cdn: str = "cloudinary"
) -> str:
    """
    Build a CDN URL that serves a resized version of the image.
    This offloads resizing to the CDN — no local PIL processing needed.
    """
    if cdn == "cloudinary":
        # Cloudinary URL transformation: insert /w_1568,q_85/ into URL
        parts = original_url.split("/upload/")
        if len(parts) == 2:
            return f"{parts[0]}/upload/w_{max_width},q_{quality},f_auto/{parts[1]}"

    if cdn == "imgix":
        sep = "&" if "?" in original_url else "?"
        return f"{original_url}{sep}w={max_width}&q={quality}&auto=format"

    if cdn == "bunny":
        sep = "&" if "?" in original_url else "?"
        return f"{original_url}{sep}width={max_width}&quality={quality}"

    return original_url  # Fallback: return original

# Example with Cloudinary:
resized_url = build_resized_cdn_url(
    "https://res.cloudinary.com/myapp/image/upload/screenshots/page1.png",
    max_width=1568,
    cdn="cloudinary"
)
answer = analyze_image_by_url(resized_url, "What is shown on this page?")
```

### Option 4: Multi-image cost estimator — report cost before sending

```python
import io
import math
import anthropic
from PIL import Image

CLAUDE_SONNET_VISION_COST_PER_1K_INPUT = 0.003  # $ per 1K input tokens (Sonnet)

def estimate_image_tokens(width: int, height: int) -> int:
    """
    Estimate Claude vision token cost for an image of given dimensions.
    Based on Claude's tile-based vision pricing.
    """
    # Claude resizes internally: fits within 1568×1568, then tiles at 512px
    max_dim = max(width, height)
    if max_dim <= 1092:
        # Fits in 1 tile
        return 1 * 1334 // 4  # ~334 tokens per tile
    elif max_dim <= 1568:
        # Up to 4 tiles
        tiles_w = math.ceil(width / 512)
        tiles_h = math.ceil(height / 512)
        return tiles_w * tiles_h * 334
    else:
        # Larger images: Claude resizes to fit 1568×1568 first
        scale = 1568 / max_dim
        rw = int(width * scale)
        rh = int(height * scale)
        tiles_w = math.ceil(rw / 512)
        tiles_h = math.ceil(rh / 512)
        return tiles_w * tiles_h * 334

def audit_images(image_paths: list[str]) -> dict:
    """
    Audit a set of images for vision token cost.
    Returns recommendations for which to resize.
    """
    total_tokens = 0
    recommendations = []

    for path in image_paths:
        img = Image.open(path)
        w, h = img.size
        tokens_full = estimate_image_tokens(w, h)
        cost_full = tokens_full * CLAUDE_SONNET_VISION_COST_PER_1K_INPUT / 1000

        # Calculate tokens at recommended sizes:
        tokens_1568 = estimate_image_tokens(min(w, 1568), min(h, 1568))
        tokens_1092 = estimate_image_tokens(min(w, 1092), min(h, 1092))
        tokens_800 = estimate_image_tokens(min(w, 800), min(h, 800))

        total_tokens += tokens_full
        recommendations.append({
            "path": path,
            "original_size": f"{w}×{h}",
            "tokens_as_is": tokens_full,
            "cost_as_is_usd": round(cost_full, 5),
            "tokens_at_1568": tokens_1568,
            "tokens_at_1092": tokens_1092,
            "tokens_at_800": tokens_800,
            "savings_at_800": f"{100*(1-tokens_800/max(tokens_full,1)):.0f}%",
            "recommendation": (
                "resize to 800px" if max(w, h) > 2000 and tokens_full > 2000 else
                "resize to 1092px" if max(w, h) > 1568 else
                "ok"
            )
        })

    total_cost = total_tokens * CLAUDE_SONNET_VISION_COST_PER_1K_INPUT / 1000
    return {
        "images": len(image_paths),
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 4),
        "recommendations": recommendations
    }
```

### Option 5: PDF page rendering at optimal DPI — avoid over-rendering

```python
import anthropic
import base64
import io
from pathlib import Path

def pdf_page_to_image(
    pdf_path: str,
    page_number: int = 0,
    dpi: int = 100,          # 72-150 DPI is enough for Claude; 300 is excessive
    max_dimension: int = 1568
) -> bytes:
    """
    Render a PDF page as an image at the right DPI for Claude.

    DPI guide for Claude vision tasks:
    - 72 DPI: Rough layout, large text only
    - 100 DPI: Standard text, form fields — good default
    - 150 DPI: Small text, tables with fine borders
    - 300 DPI: Highly detailed graphics, microscopic text — rarely needed
    """
    try:
        import pymupdf  # pip install pymupdf (formerly fitz)
        doc = pymupdf.open(pdf_path)
        page = doc[page_number]
        mat = pymupdf.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, colorspace=pymupdf.csRGB)
        img_bytes = pix.tobytes("jpeg", jpg_quality=85)
        doc.close()
    except ImportError:
        raise RuntimeError("Install pymupdf: pip install pymupdf")

    # Apply max_dimension constraint via PIL
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes))
    w, h = img.size
    if max(w, h) > max_dimension:
        scale = max_dimension / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        img_bytes = buf.getvalue()

    return img_bytes

def analyze_pdf_page(
    pdf_path: str,
    question: str,
    page_number: int = 0,
    dpi: int = 100,
    model: str = "claude-sonnet-4-6"
) -> str:
    """
    Extract information from a PDF page efficiently.
    Uses 100 DPI (vs typical 300 DPI) — 9× fewer pixels, ~80% token savings.
    """
    client = anthropic.Anthropic()
    img_bytes = pdf_page_to_image(pdf_path, page_number, dpi=dpi)
    img_b64 = base64.standard_b64encode(img_bytes).decode()

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": question}
            ]
        }]
    )
    return response.content[0].text
```

### Option 6: Image caching with hash key — skip re-encoding the same image

```python
import anthropic
import base64
import hashlib
import json
from pathlib import Path
from PIL import Image
import io

class CachedImageAnalyzer:
    """
    Cache Claude's analysis of images by content hash.
    Avoids sending the same image multiple times across sessions.
    Pair with image resizing for maximum cost efficiency.
    """

    def __init__(
        self,
        cache_dir: str = "/tmp/image_analysis_cache",
        max_dimension: int = 1568
    ):
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._max_dim = max_dimension
        self._client = anthropic.Anthropic()

    def _image_hash(self, image_bytes: bytes) -> str:
        return hashlib.sha256(image_bytes).hexdigest()[:16]

    def _resize(self, image_bytes: bytes) -> tuple[bytes, str]:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGB",):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > self._max_dim:
            scale = self._max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue(), "image/jpeg"

    def analyze(self, image_bytes: bytes, question: str, model: str = "claude-sonnet-4-6") -> str:
        cache_key = f"{self._image_hash(image_bytes)}-{hashlib.md5(question.encode()).hexdigest()[:8]}"
        cache_path = self._cache_dir / f"{cache_key}.json"

        if cache_path.exists():
            cached = json.loads(cache_path.read_text())
            return cached["answer"]

        # Resize before sending
        resized, media_type = self._resize(image_bytes)
        img_b64 = base64.standard_b64encode(resized).decode()

        response = self._client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                    {"type": "text", "text": question}
                ]
            }]
        )
        answer = response.content[0].text

        cache_path.write_text(json.dumps({
            "answer": answer,
            "tokens": response.usage.input_tokens + response.usage.output_tokens
        }))
        return answer

analyzer = CachedImageAnalyzer(max_dimension=1092)
# First call: resize + API call
answer1 = analyzer.analyze(screenshot_bytes, "What button is highlighted?")
# Second call with same image + question: instant cache hit, zero API cost
answer2 = analyzer.analyze(screenshot_bytes, "What button is highlighted?")
```

## Image Size vs Token Cost

| Original Size | Tokens (as-is) | Tokens at 1092px | Tokens at 800px | Savings at 800px |
|---|---|---|---|---|
| 4096×4096 (4K) | ~6,400 | ~340 | ~170 | 97% |
| 2560×1440 (1440p) | ~1,700 | ~340 | ~170 | 90% |
| 1920×1080 (1080p) | ~680 | ~340 | ~170 | 75% |
| 1280×720 (720p) | ~340 | ~340 | ~170 | 50% |
| 800×600 | ~170 | ~170 | ~170 | 0% (already small) |

## Expected Token Savings
4K screenshot in a screenshot-analysis agent at 100 calls/day: 6,400 × 100 = 640,000 vision tokens/day
Same screenshots resized to 1092px: 340 × 100 = 34,000 vision tokens/day
Savings: 606,000 tokens/day × $0.003/1K = ~$1.82/day = ~$55/month per agent
At scale (1,000 calls/day): ~$550/month savings from image resizing alone

## Environment
- Any agent processing screenshots, document images, photos, or PDF pages; image token costs dominate vision-heavy agents and are the easiest cost to reduce (pure preprocessing, no behavior change); apply max_dimension=1092 by default and only increase to 1568 for tasks that genuinely need the extra resolution (fine text, small UI elements)
- Source: direct experience; unresized 4K screenshots are the single largest token waste in production vision agents — typically 10–30× more expensive than necessary with zero quality benefit for the task
