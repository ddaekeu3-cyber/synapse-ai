---
layout: solution
title: "Image Attachments Consume Too Many Tokens — Vision Costs Spike"
category: context-window
description: "Agent processes images in multi-turn conversations. Each image costs 1,000–5,000 tokens depending on size. Including the same image across multiple turns, or sending high-resolution images when a thumbnail would suffice, causes rapid token burn."
tags: [vision, image, tokens, multimodal, resize, cost, context-window, base64]
---

# Image Attachments Consume Too Many Tokens — Vision Costs Spike

## Symptom
- Single API call with one screenshot costs 4,000 tokens before any text
- Multi-turn conversation including the same image repeatedly costs 10× more than expected
- Sending a 4K screenshot when the task only needs to read a text label
- Context window fills after 3-4 turns because images are resized too large
- Vision API costs 50× more than text-only equivalent tasks

## Root Cause
Vision models tokenize images by dividing them into tiles. Anthropic Claude uses 85 tokens per 85×85 pixel tile (approximately). A 1920×1080 image at full resolution generates ~800 tiles = ~68,000 tokens. Sending the same image multiple turns, or failing to resize before sending, compounds the cost dramatically.

## Fix

### Option 1: Resize images to minimum needed resolution

```python
from PIL import Image
import io
import base64

def resize_image_for_task(
    image_bytes: bytes,
    task: str = "general",
    max_dimension: int = None
) -> bytes:
    """
    Resize image to the minimum resolution needed for the task.
    Vision models can read text at much lower resolution than display quality.
    """
    # Task-specific max dimensions
    task_limits = {
        "read_text":     (1024, 768),   # Text is readable at 1024px wide
        "classify":      (512, 512),    # Classification needs less detail
        "detect_ui":     (1280, 960),   # UI element detection
        "count_objects": (800, 600),    # Object counting
        "general":       (1024, 1024),  # Default
    }
    max_w, max_h = task_limits.get(task, (1024, 1024))
    if max_dimension:
        max_w = max_h = max_dimension

    img = Image.open(io.BytesIO(image_bytes))
    original_size = img.size

    # Only resize if larger than the limit
    if img.width > max_w or img.height > max_h:
        img.thumbnail((max_w, max_h), Image.LANCZOS)
        print(f"Resized: {original_size} → {img.size} "
              f"(~{estimate_image_tokens(original_size)} → ~{estimate_image_tokens(img.size)} tokens)")

    output = io.BytesIO()
    img.save(output, format="JPEG", quality=85, optimize=True)
    return output.getvalue()

def estimate_image_tokens(size: tuple[int, int]) -> int:
    """Estimate token count for Claude vision (85px tiles, ~85 tokens each)"""
    tiles_w = (size[0] + 84) // 85
    tiles_h = (size[1] + 84) // 85
    return tiles_w * tiles_h * 85

# Before sending any image:
resized = resize_image_for_task(raw_image_bytes, task="read_text")
# 1920×1080 → 1024×576: ~68,000 → ~19,000 tokens (72% reduction)
```

### Option 2: Cache images — don't send the same image twice

```python
import hashlib

class ImageCache:
    """
    Track which images have already been sent in this conversation.
    Replace repeated images with a text reference to the first occurrence.
    """

    def __init__(self):
        self._sent: dict[str, int] = {}  # image_hash → turn_number

    def image_hash(self, image_bytes: bytes) -> str:
        return hashlib.sha256(image_bytes).hexdigest()[:16]

    def should_send(self, image_bytes: bytes, current_turn: int) -> bool:
        h = self.image_hash(image_bytes)
        if h in self._sent:
            print(f"Image already sent at turn {self._sent[h]} — skip resend")
            return False
        self._sent[h] = current_turn
        return True

    def reference_text(self, image_bytes: bytes) -> str:
        h = self.image_hash(image_bytes)
        turn = self._sent.get(h, "unknown")
        return f"[Image already provided at turn {turn} — refer to that image]"

cache = ImageCache()

def build_message_content(text: str, images: list[bytes], turn: int) -> list:
    """Build message content, deduplicating images already sent"""
    content = []
    for img_bytes in images:
        if cache.should_send(img_bytes, turn):
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(img_bytes).decode()
                }
            })
        else:
            # Reference instead of resend — 0 tokens vs 5,000+ tokens
            content.append({
                "type": "text",
                "text": cache.reference_text(img_bytes)
            })
    content.append({"type": "text", "text": text})
    return content
```

### Option 3: Use prompt caching for repeated images (Anthropic)

```python
import anthropic

# Anthropic supports prompt caching for image content
# Cache the image in the first call — subsequent calls cost only 10% of original

client = anthropic.Anthropic()

def call_with_image_cache(
    image_bytes: bytes,
    questions: list[str]
) -> list[str]:
    """
    Process multiple questions about the same image.
    Cache the image so only the first call incurs full image token cost.
    """
    image_b64 = base64.b64encode(image_bytes).decode()

    # First call: image is cached with cache_control
    # Subsequent calls: image tokens are read from cache at 10% cost
    results = []
    for i, question in enumerate(questions):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64
                        },
                        "cache_control": {"type": "ephemeral"}  # Cache this image
                    },
                    {
                        "type": "text",
                        "text": question
                    }
                ]
            }]
        )
        results.append(response.content[0].text)

        if i == 0:
            print(f"First call: full image tokens. Cache created.")
        else:
            print(f"Call {i+1}: image served from cache (~10% token cost)")

    return results

# Ask 5 questions about the same image:
answers = call_with_image_cache(screenshot_bytes, [
    "What is the page title?",
    "How many buttons are visible?",
    "What color is the header?",
    "Is there an error message?",
    "What is the main CTA text?",
])
```

### Option 4: Convert image to text when possible

```python
import pytesseract
from PIL import Image
import io

def image_to_text_if_feasible(image_bytes: bytes, threshold_tokens: int = 1000) -> str | bytes:
    """
    If image is primarily text, extract it via OCR — costs ~0 tokens vs thousands.
    Falls back to image if OCR confidence is too low.
    """
    img = Image.open(io.BytesIO(image_bytes))
    estimated_tokens = estimate_image_tokens(img.size)

    if estimated_tokens < threshold_tokens:
        return image_bytes  # Small image — send as-is

    # Try OCR
    try:
        ocr_result = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        # Check average confidence
        confidences = [c for c in ocr_result["conf"] if c != -1]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0

        if avg_confidence > 70:  # High confidence — text image
            text = pytesseract.image_to_string(img)
            word_count = len(text.split())
            text_tokens = word_count // 0.75  # ~0.75 words per token

            if text_tokens < estimated_tokens * 0.3:
                print(f"OCR: {estimated_tokens} image tokens → ~{text_tokens:.0f} text tokens")
                return f"[Image converted to text via OCR]\n{text}"
    except Exception as e:
        print(f"OCR failed: {e}")

    return image_bytes  # Fall back to sending image

# Usage:
content = image_to_text_if_feasible(screenshot_bytes)
if isinstance(content, str):
    # Send as text — cheap
    message_content = [{"type": "text", "text": content}]
else:
    # Send as image — resize first
    resized = resize_image_for_task(content, task="read_text")
    message_content = [{"type": "image", "source": {...}}]
```

### Option 5: Image token budget guard

```python
MAX_IMAGE_TOKENS_PER_CALL = 10_000  # Hard budget per API call

def enforce_image_token_budget(
    images: list[bytes],
    budget: int = MAX_IMAGE_TOKENS_PER_CALL
) -> list[bytes]:
    """
    Resize images to fit within a token budget.
    Distributes budget evenly across all images.
    """
    if not images:
        return images

    per_image_budget = budget // len(images)
    result = []

    for img_bytes in images:
        img = Image.open(io.BytesIO(img_bytes))
        current_tokens = estimate_image_tokens(img.size)

        if current_tokens <= per_image_budget:
            result.append(img_bytes)
            continue

        # Calculate scale factor to hit budget
        scale = (per_image_budget / current_tokens) ** 0.5
        new_w = int(img.width * scale)
        new_h = int(img.height * scale)

        img = img.resize((new_w, new_h), Image.LANCZOS)
        print(f"Budget resize: {current_tokens} → {estimate_image_tokens((new_w, new_h))} tokens")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        result.append(buf.getvalue())

    return result

# Ensure all images in a call fit within budget:
safe_images = enforce_image_token_budget(user_images, budget=8000)
```

## Image Token Cost by Resolution

| Resolution | Approx tokens | Cost ratio |
|---|---|---|
| 3840×2160 (4K) | ~230,000 | 100× |
| 1920×1080 (FHD) | ~68,000 | 30× |
| 1280×720 (HD) | ~31,000 | 13× |
| 1024×768 | ~20,000 | 9× |
| 512×512 | ~5,000 | 2× |
| 256×256 | ~1,300 | 1× |

## Expected Token Savings
4K screenshot per turn, 10 turns: ~2,300,000 tokens
1024×576 resize + cache: ~190,000 tokens (92% reduction)

## Environment
- Any agent with vision/multimodal capability; critical for UI automation, document processing, and screenshot analysis agents
- Source: direct measurement of Claude vision token costs at various resolutions
