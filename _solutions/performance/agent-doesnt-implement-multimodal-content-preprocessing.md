---
title: "Agent Doesn't Implement Multimodal Content Preprocessing Before API Submission"
description: "Agents that send raw images, audio, or documents to the API without preprocessing pay for unnecessary tokens — resizing, compressing, and format-converting multimodal content before submission cuts cost and latency by 50-90%."
difficulty: intermediate
category: performance
tags: [performance, multimodal, images, preprocessing, cost-optimization, vision, tokens]
---

# Agent Doesn't Implement Multimodal Content Preprocessing Before API Submission

## Problem

A 4K screenshot sent as a raw PNG to a vision model uses 1,600+ image tokens and takes seconds to upload. The same screenshot resized to 1024px wide uses 85 tokens and uploads in 100ms. Agents that forward user-uploaded images, PDFs, and audio without preprocessing waste budget on resolution the model can't use, hit API payload size limits, and degrade concurrent user experience as bandwidth-heavy uploads serialize.

**Symptoms:**
- Vision model calls cost 10x more than expected due to oversized images
- Image upload latency dominates total request time (3-5 seconds per image)
- API returns 413 Payload Too Large for uncompressed PNGs from screenshots
- JPEG screenshots uploaded as PNG waste 3x bandwidth with no quality benefit
- Multi-page PDFs send all pages when only the first two are relevant

---

## Solution 1: Image Resize and Compression Pipeline

Resize images to the model's optimal resolution and convert to JPEG before base64 encoding.

```python
import asyncio
import base64
import io
from dataclasses import dataclass
from typing import Optional
import anthropic

# pip install Pillow
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# claude-opus-4-6 vision optimal sizes
# 1568px on the long edge → 1600 tokens
# 1024px on the long edge → ~340 tokens
# 768px on the long edge → ~170 tokens
OPTIMAL_LONG_EDGE = 1024  # Good balance: detail vs cost
MAX_LONG_EDGE = 1568       # Model's native max


@dataclass
class ProcessedImage:
    data: bytes               # Compressed image bytes
    media_type: str           # "image/jpeg" or "image/png"
    width: int
    height: int
    original_size_kb: float
    compressed_size_kb: float
    estimated_tokens: int

    def to_api_block(self) -> dict:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": base64.standard_b64encode(self.data).decode(),
            },
        }


def estimate_image_tokens(width: int, height: int) -> int:
    """
    Anthropic token formula: ceil(w/32) * ceil(h/32) + 1
    Source: Anthropic vision pricing docs.
    """
    import math
    return math.ceil(width / 32) * math.ceil(height / 32) + 1


def preprocess_image(
    raw_bytes: bytes,
    max_long_edge: int = OPTIMAL_LONG_EDGE,
    jpeg_quality: int = 85,
    force_jpeg: bool = True,
) -> ProcessedImage:
    """Resize, convert, and compress an image for optimal API submission."""
    if not HAS_PIL:
        raise ImportError("pip install Pillow")

    original_size_kb = len(raw_bytes) / 1024
    img = Image.open(io.BytesIO(raw_bytes))

    # Convert RGBA/P to RGB for JPEG compatibility
    if img.mode in ("RGBA", "P", "LA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        if img.mode in ("RGBA", "LA"):
            background.paste(img, mask=img.split()[-1])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # Resize if needed
    w, h = img.size
    long_edge = max(w, h)
    if long_edge > max_long_edge:
        scale = max_long_edge / long_edge
        new_w, new_h = int(w * scale), int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        w, h = new_w, new_h

    # Encode
    buf = io.BytesIO()
    fmt = "JPEG" if force_jpeg else img.format or "JPEG"
    media_type = "image/jpeg" if fmt == "JPEG" else "image/png"
    img.save(buf, format=fmt, quality=jpeg_quality, optimize=True)
    compressed = buf.getvalue()

    return ProcessedImage(
        data=compressed,
        media_type=media_type,
        width=w,
        height=h,
        original_size_kb=original_size_kb,
        compressed_size_kb=len(compressed) / 1024,
        estimated_tokens=estimate_image_tokens(w, h),
    )


class VisionAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def describe(self, raw_image_bytes: bytes, prompt: str = "Describe this image.") -> str:
        processed = preprocess_image(raw_image_bytes, max_long_edge=OPTIMAL_LONG_EDGE)
        print(
            f"[preprocess] {processed.original_size_kb:.0f}KB → {processed.compressed_size_kb:.0f}KB "
            f"({processed.width}×{processed.height}) ~{processed.estimated_tokens} tokens"
        )

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    processed.to_api_block(),
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return response.content[0].text


async def demo():
    # Load a test image
    import urllib.request
    img_bytes = open("test.png", "rb").read() if False else b""  # Replace with actual image
    if img_bytes:
        agent = VisionAgent(api_key="sk-...")
        desc = await agent.describe(img_bytes, "What do you see?")
        print(desc[:200])

# asyncio.run(demo())
```

---

## Solution 2: Adaptive Quality Based on Token Budget

Choose JPEG quality and resolution dynamically based on remaining token budget for the request.

```python
import asyncio
import base64
import io
import math
from dataclasses import dataclass
from typing import Optional
import anthropic

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


@dataclass
class ImageBudgetPolicy:
    token_budget: int          # Max tokens this image can consume
    max_file_size_kb: int = 1024  # Max payload size

    def compute_target_long_edge(self) -> int:
        """Work backwards from token budget to determine max image dimension."""
        # token_budget ≈ ceil(w/32) * ceil(h/32) + 1 for square image
        # For square: (long_edge/32)^2 ≈ budget → long_edge ≈ 32 * sqrt(budget)
        long_edge = int(32 * math.sqrt(self.token_budget))
        return min(long_edge, 1568)

    def compute_jpeg_quality(self) -> int:
        if self.token_budget > 1000:
            return 90
        elif self.token_budget > 500:
            return 80
        else:
            return 70


def adaptive_preprocess(
    raw_bytes: bytes,
    policy: ImageBudgetPolicy,
) -> tuple[bytes, str, int]:
    """Returns (compressed_bytes, media_type, estimated_tokens)."""
    if not HAS_PIL:
        raise ImportError("pip install Pillow")

    img = Image.open(io.BytesIO(raw_bytes))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    target_edge = policy.compute_target_long_edge()
    quality = policy.compute_jpeg_quality()

    w, h = img.size
    long_edge = max(w, h)
    if long_edge > target_edge:
        scale = target_edge / long_edge
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    w, h = img.size
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    compressed = buf.getvalue()

    # Check file size limit
    if len(compressed) > policy.max_file_size_kb * 1024:
        # Reduce quality further
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=50, optimize=True)
        compressed = buf.getvalue()

    tokens = math.ceil(w / 32) * math.ceil(h / 32) + 1
    return compressed, "image/jpeg", tokens


class AdaptiveVisionAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def analyze(
        self,
        raw_image_bytes: bytes,
        prompt: str,
        token_budget_for_image: int = 340,  # Default: 1024px equivalent
    ) -> str:
        policy = ImageBudgetPolicy(token_budget=token_budget_for_image)
        compressed, media_type, tokens = adaptive_preprocess(raw_image_bytes, policy)

        print(
            f"[adaptive] budget={token_budget_for_image} actual_tokens~={tokens} "
            f"size={len(compressed)//1024}KB"
        )

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.standard_b64encode(compressed).decode(),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return response.content[0].text
```

---

## Solution 3: Batch Image Preprocessing with Concurrency

Preprocess multiple images concurrently using asyncio to avoid blocking the event loop.

```python
import asyncio
import base64
import io
import math
from dataclasses import dataclass
from typing import Optional
import anthropic

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


async def preprocess_image_async(
    raw_bytes: bytes,
    max_long_edge: int = 1024,
    quality: int = 85,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> tuple[bytes, str, int]:
    """Run PIL processing in thread pool to avoid blocking event loop."""

    def _process() -> tuple[bytes, str, int]:
        img = Image.open(io.BytesIO(raw_bytes))
        if img.mode not in ("RGB",):
            img = img.convert("RGB")
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > max_long_edge:
            scale = max_long_edge / long_edge
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        w, h = img.size
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        tokens = math.ceil(w / 32) * math.ceil(h / 32) + 1
        return buf.getvalue(), "image/jpeg", tokens

    loop = asyncio.get_event_loop()
    if semaphore:
        async with semaphore:
            return await loop.run_in_executor(None, _process)
    return await loop.run_in_executor(None, _process)


@dataclass
class ImageAnalysisResult:
    index: int
    description: str
    tokens_used: int
    compressed_size_kb: float


class BatchVisionAgent:
    def __init__(self, api_key: str, max_concurrent_preprocess: int = 4):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self._sem = asyncio.Semaphore(max_concurrent_preprocess)

    async def _preprocess_and_describe(
        self,
        index: int,
        raw_bytes: bytes,
        prompt: str,
    ) -> ImageAnalysisResult:
        compressed, media_type, tokens = await preprocess_image_async(
            raw_bytes, max_long_edge=1024, semaphore=self._sem
        )

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.standard_b64encode(compressed).decode(),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return ImageAnalysisResult(
            index=index,
            description=response.content[0].text,
            tokens_used=tokens,
            compressed_size_kb=len(compressed) / 1024,
        )

    async def analyze_batch(
        self,
        images: list[bytes],
        prompt: str = "Describe this image briefly.",
    ) -> list[ImageAnalysisResult]:
        tasks = [
            self._preprocess_and_describe(i, img, prompt)
            for i, img in enumerate(images)
        ]
        results = await asyncio.gather(*tasks)
        total_tokens = sum(r.tokens_used for r in results)
        print(f"[batch] Processed {len(images)} images, ~{total_tokens} total image tokens")
        return list(results)
```

---

## Solution 4: PDF Page Selection and Image Extraction

For PDF documents, extract only the relevant pages as images rather than sending the entire document.

```python
import asyncio
import base64
import io
from dataclasses import dataclass
from typing import Optional
import anthropic

# pip install pymupdf (fitz)
try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


@dataclass
class PDFPageImage:
    page_number: int    # 0-indexed
    image_bytes: bytes
    media_type: str = "image/jpeg"
    width: int = 0
    height: int = 0

    def to_content_block(self) -> dict:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": base64.standard_b64encode(self.image_bytes).decode(),
            },
        }


def extract_pdf_pages(
    pdf_bytes: bytes,
    pages: Optional[list[int]] = None,  # None = first 3 pages
    dpi: int = 150,  # 150 DPI is usually enough for OCR/description
    max_long_edge: int = 1024,
) -> list[PDFPageImage]:
    if not HAS_FITZ:
        raise ImportError("pip install pymupdf")
    if not HAS_PIL:
        raise ImportError("pip install Pillow")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = len(doc)

    if pages is None:
        pages = list(range(min(3, total_pages)))  # Default: first 3 pages

    results = []
    for page_num in pages:
        if page_num >= total_pages:
            break
        page = doc[page_num]
        mat = fitz.Matrix(dpi / 72, dpi / 72)  # Scale factor
        pix = page.get_pixmap(matrix=mat)
        img_data = pix.tobytes("jpeg")

        # Resize via PIL if needed
        img = Image.open(io.BytesIO(img_data))
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > max_long_edge:
            scale = max_long_edge / long_edge
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            img_data = buf.getvalue()
            w, h = img.size

        results.append(PDFPageImage(
            page_number=page_num,
            image_bytes=img_data,
            width=w,
            height=h,
        ))
        print(f"[pdf] Page {page_num+1}: {len(img_data)//1024}KB ({w}×{h})")

    doc.close()
    return results


class PDFVisionAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def analyze_pdf(
        self,
        pdf_bytes: bytes,
        question: str,
        pages: Optional[list[int]] = None,
    ) -> str:
        page_images = extract_pdf_pages(pdf_bytes, pages=pages)

        content = []
        for pg in page_images:
            content.append({"type": "text", "text": f"[Page {pg.page_number + 1}]"})
            content.append(pg.to_content_block())
        content.append({"type": "text", "text": question})

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": content}],
        )
        return response.content[0].text
```

---

## Solution 5: URL-Based Image Submission with Server-Side Resize

For images already hosted on the web, pass the URL directly to the API instead of downloading, resizing, and re-uploading.

```python
import asyncio
from dataclasses import dataclass
from typing import Optional
import aiohttp
import anthropic


@dataclass
class ImageSource:
    mode: str   # "url" or "base64"
    url: Optional[str] = None
    data: Optional[bytes] = None
    media_type: str = "image/jpeg"

    def to_content_block(self) -> dict:
        if self.mode == "url":
            return {
                "type": "image",
                "source": {"type": "url", "url": self.url},
            }
        import base64
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": base64.standard_b64encode(self.data).decode(),
            },
        }


async def resolve_image_source(
    url_or_bytes,
    prefer_url: bool = True,
    max_size_kb: int = 5120,
) -> ImageSource:
    """
    Prefer URL mode (no upload, API fetches directly).
    Fall back to base64 if URL is not accessible or payload is within limits.
    """
    if isinstance(url_or_bytes, str) and prefer_url:
        # Validate the URL is reachable
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(url_or_bytes, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                    if resp.status == 200:
                        content_length = int(resp.headers.get("content-length", 0))
                        if content_length < max_size_kb * 1024:
                            return ImageSource(mode="url", url=url_or_bytes)
        except Exception:
            pass

    # Download and base64 encode
    if isinstance(url_or_bytes, str):
        async with aiohttp.ClientSession() as session:
            async with session.get(url_or_bytes) as resp:
                data = await resp.read()
                ct = resp.headers.get("content-type", "image/jpeg")
    else:
        data = url_or_bytes
        ct = "image/jpeg"

    return ImageSource(mode="base64", data=data, media_type=ct)


class UrlAwareVisionAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def describe(self, image_url: str, prompt: str) -> str:
        source = await resolve_image_source(image_url, prefer_url=True)
        print(f"[source] mode={source.mode}")

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": [source.to_content_block(), {"type": "text", "text": prompt}],
            }],
        )
        return response.content[0].text
```

---

## Solution 6: Image Deduplication Cache

Cache preprocessed image results by content hash — identical images sent in multiple turns (e.g., a shared dashboard screenshot) are processed only once.

```python
import asyncio
import base64
import hashlib
import io
import math
import time
from dataclasses import dataclass, field
from typing import Optional
import anthropic

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


@dataclass
class CachedImage:
    content_hash: str
    compressed_bytes: bytes
    media_type: str
    width: int
    height: int
    estimated_tokens: int
    cached_at: float = field(default_factory=time.time)

    def age_seconds(self) -> float:
        return time.time() - self.cached_at

    def to_content_block(self) -> dict:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": base64.standard_b64encode(self.compressed_bytes).decode(),
            },
        }


class ImagePreprocessCache:
    def __init__(self, max_size: int = 50, ttl_seconds: float = 300.0):
        self._cache: dict[str, CachedImage] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0

    def _hash(self, raw_bytes: bytes) -> str:
        return hashlib.sha256(raw_bytes).hexdigest()[:16]

    def _preprocess(self, raw_bytes: bytes) -> CachedImage:
        img = Image.open(io.BytesIO(raw_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > 1024:
            scale = 1024 / long_edge
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        w, h = img.size
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        compressed = buf.getvalue()
        tokens = math.ceil(w / 32) * math.ceil(h / 32) + 1
        return CachedImage(
            content_hash=self._hash(raw_bytes),
            compressed_bytes=compressed,
            media_type="image/jpeg",
            width=w, height=h,
            estimated_tokens=tokens,
        )

    def get_or_process(self, raw_bytes: bytes) -> tuple[CachedImage, bool]:
        key = self._hash(raw_bytes)
        if key in self._cache:
            entry = self._cache[key]
            if entry.age_seconds() < self._ttl:
                self._hits += 1
                return entry, True
            del self._cache[key]

        self._misses += 1
        if len(self._cache) >= self._max_size:
            oldest = min(self._cache, key=lambda k: self._cache[k].cached_at)
            del self._cache[oldest]

        entry = self._preprocess(raw_bytes)
        self._cache[key] = entry
        return entry, False

    def stats(self) -> dict:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self._hits / (self._hits + self._misses + 1e-9),
            "cached_images": len(self._cache),
        }


class CachingVisionAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.cache = ImagePreprocessCache()

    async def analyze(self, raw_image_bytes: bytes, prompt: str) -> str:
        cached_img, from_cache = self.cache.get_or_process(raw_image_bytes)
        print(
            f"[cache] from_cache={from_cache} tokens~={cached_img.estimated_tokens} "
            f"stats={self.cache.stats()}"
        )

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": [
                    cached_img.to_content_block(),
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return response.content[0].text
```

---

## Comparison

| Solution | Tokens Saved | Bandwidth Saved | PDF Support | Concurrency | Complexity |
|---|---|---|---|---|---|
| Resize + JPEG conversion | 60-90% | 70-95% | No | No | Low |
| Adaptive quality by budget | Variable | Variable | No | No | Low |
| Concurrent batch preprocessing | Same as #1 | Same as #1 | No | Yes | Medium |
| PDF page extraction | Very high | Very high | Yes | No | Medium |
| URL-based submission | 100% (no upload) | 100% | No | Yes | Low |
| Preprocessing cache | Depends on repetition | Depends on repetition | No | No | Medium |

**Recommendation:** Always apply Solution 1 (resize + JPEG) for every user-uploaded image — it's four lines of PIL code that cut typical costs by 70%. Add Solution 6 (preprocessing cache) for agents that receive shared screenshots across multiple sessions (monitoring dashboards, shared documents). Use Solution 4 (PDF extraction) for any agent that processes document uploads, limiting to 2-3 pages by default.
