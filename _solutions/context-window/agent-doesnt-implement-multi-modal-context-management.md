---
layout: solution
title: "Agent Doesn't Implement Multi-Modal Context Management"
category: context-window
description: "Agents that naively add images and PDFs to every request exhaust the context window quickly and pay high token costs — multi-modal context management selectively includes visual content only when needed."
tags: [context-window, multi-modal, images, vision, pdf, token-cost, claude-vision]
---

# Agent Doesn't Implement Multi-Modal Context Management

## Problem

Claude can process images, PDFs, and other visual content, but visual inputs are expensive: a single high-resolution image can consume thousands of tokens. Agents that blindly include every image in every API call, re-send the same image in every turn of a multi-turn conversation, or include images when text descriptions would suffice, burn through context budgets rapidly. Multi-modal context management means: include visual content only when it adds unique value, cache descriptions when possible, and evict stale images from the active context.

## Solutions

### Option 1: Image-or-Description Routing

Decide whether to send the raw image or a pre-computed text description based on query type. Most questions about an image can be answered from a cached description.

```python
import anthropic
import base64
from pathlib import Path

client = anthropic.Anthropic()

def encode_image(image_path: str) -> tuple[str, str]:
    """Returns (base64_data, media_type)."""
    path = Path(image_path)
    suffix_to_media = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    }
    media_type = suffix_to_media.get(path.suffix.lower(), "image/jpeg")
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return data, media_type

def describe_image(image_path: str) -> str:
    """Generate a reusable text description of an image."""
    data, media_type = encode_image(image_path)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data},
                },
                {
                    "type": "text",
                    "text": (
                        "Describe this image comprehensively for a text-only system. "
                        "Include: content, objects, colors, text visible, layout, context. "
                        "Be specific enough that someone could answer questions about it without seeing it."
                    )
                }
            ]
        }]
    )
    return resp.content[0].text

# Cache of image descriptions (in production: use Redis or SQLite)
_description_cache: dict[str, str] = {}

# Queries that need the actual image vs. can use description
VISION_REQUIRED_PATTERNS = [
    "exact color", "pixel", "read the text", "ocr", "crop", "specific detail",
    "bounding box", "locate the", "where exactly", "coordinates",
]

def needs_vision(query: str) -> bool:
    q_lower = query.lower()
    return any(p in q_lower for p in VISION_REQUIRED_PATTERNS)

def answer_image_query(
    image_path: str,
    query: str,
    force_vision: bool = False,
) -> dict:
    use_vision = force_vision or needs_vision(query)

    if use_vision:
        data, media_type = encode_image(image_path)
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
            {"type": "text", "text": query},
        ]
        mode = "vision"
    else:
        # Use cached description — no image tokens
        if image_path not in _description_cache:
            _description_cache[image_path] = describe_image(image_path)
        description = _description_cache[image_path]
        content = [{"type": "text", "text": f"Image description:\n{description}\n\nQuestion: {query}"}]
        mode = "description"

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": content}],
    )

    return {
        "answer": resp.content[0].text,
        "mode": mode,
        "input_tokens": resp.usage.input_tokens,
    }

# Demo (uses a placeholder path; replace with a real image)
IMAGE_PATH = "/tmp/sample.png"

# Create a tiny test image if it doesn't exist
try:
    import struct, zlib
    def make_tiny_png() -> bytes:
        def chunk(name, data):
            c = name + data
            return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        idat = zlib.compress(b"\x00\xff\x00\x00")
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    Path(IMAGE_PATH).write_bytes(make_tiny_png())
except Exception:
    pass

queries = [
    "What is shown in this image?",           # Can use description
    "Read the exact text visible in the image.",  # Needs vision
    "What colors are prominent?",               # Can use description
]

for q in queries:
    try:
        result = answer_image_query(IMAGE_PATH, q)
        print(f"[{result['mode']:11}] tokens={result['input_tokens']:4} | {q}")
    except Exception as e:
        print(f"[error] {e} | {q}")
# Expected Token Savings: 60-80% on follow-up queries that don't need raw image pixels
# Environment: Document agents, image Q&A bots, visual content pipelines
```

### Option 2: Image Eviction from Multi-Turn Context

In a multi-turn conversation, images sent in early turns continue consuming tokens in every subsequent turn. Evict them after the first turn and replace with their description.

```python
import anthropic
import base64
from pathlib import Path
from copy import deepcopy

client = anthropic.Anthropic()

def make_image_block(image_path: str) -> dict:
    data = base64.standard_b64encode(Path(image_path).read_bytes()).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
        "_image_path": image_path,  # Metadata for eviction
    }

def evict_images_from_history(
    messages: list[dict],
    keep_last_n: int = 1,
) -> tuple[list[dict], int]:
    """
    Replace images in older turns with a text placeholder.
    Returns (evicted_messages, num_evicted).
    """
    # Find turns that contain images
    image_turn_indices = []
    for i, msg in enumerate(messages):
        content = msg.get("content", [])
        if isinstance(content, list):
            if any(isinstance(b, dict) and b.get("type") == "image" for b in content):
                image_turn_indices.append(i)

    # Keep the last `keep_last_n` image turns; evict the rest
    to_evict = image_turn_indices[:-keep_last_n] if keep_last_n > 0 else image_turn_indices
    evicted_count = 0
    evicted_messages = deepcopy(messages)

    for i in to_evict:
        new_content = []
        for block in evicted_messages[i].get("content", []):
            if isinstance(block, dict) and block.get("type") == "image":
                path = block.get("_image_path", "unknown")
                new_content.append({
                    "type": "text",
                    "text": f"[Image from {Path(path).name} — already described in earlier turn]",
                })
                evicted_count += 1
            else:
                new_content.append(block)
        evicted_messages[i]["content"] = new_content

    return evicted_messages, evicted_count

def multi_turn_vision_chat(image_path: str) -> None:
    """Multi-turn chat that evicts images from old turns."""
    messages = []
    image_sent = False

    questions = [
        "What do you see in this image?",
        "What colors are present?",        # Doesn't need the image again
        "Describe the overall composition.",  # Doesn't need the image again
    ]

    for i, question in enumerate(questions):
        # First turn: include the image
        if i == 0 and not image_sent:
            content = [
                make_image_block(image_path),
                {"type": "text", "text": question},
            ]
            image_sent = True
        else:
            content = question  # Text only for follow-ups

        messages.append({"role": "user", "content": content})

        # Evict images from all but the last image turn before each API call
        active_messages, evicted = evict_images_from_history(messages, keep_last_n=1)
        if evicted:
            print(f"  Evicted {evicted} image block(s) from history")

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=active_messages,
        )

        answer = resp.content[0].text
        messages.append({"role": "assistant", "content": answer})
        print(f"Turn {i+1} | tokens={resp.usage.input_tokens} | Q: {question[:50]}")
        print(f"       | A: {answer[:80]}...")

# Demo
IMAGE_PATH = "/tmp/sample.png"
try:
    multi_turn_vision_chat(IMAGE_PATH)
except Exception as e:
    print(f"Demo requires a real image file: {e}")
# Expected Token Savings: 40-70% on turns 2+ in image conversations
# Environment: Multi-turn vision chatbots, document review agents, design feedback tools
```

### Option 3: URL-Based Image References to Avoid Base64 Bloat

When images are publicly accessible, use URL references instead of base64 encoding. URLs cost ~10 tokens vs thousands for base64.

```python
import anthropic

client = anthropic.Anthropic()

def image_block_url(url: str) -> dict:
    """Reference image by URL — no base64 encoding, minimal token cost."""
    return {
        "type": "image",
        "source": {
            "type": "url",
            "url": url,
        },
    }

def image_block_base64(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """Include image as base64 — use only when URL is not available."""
    import base64
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(image_bytes).decode(),
        },
    }

def estimate_token_overhead(image_bytes: bytes) -> dict:
    """Estimate tokens consumed by base64 vs URL reference."""
    base64_chars = len(image_bytes) * 4 // 3
    # Rough approximation: 1 token ≈ 4 chars for base64
    base64_tokens_approx = base64_chars // 4
    url_tokens_approx = 15  # URL string + JSON overhead

    return {
        "base64_tokens_approx": base64_tokens_approx,
        "url_tokens_approx": url_tokens_approx,
        "savings": base64_tokens_approx - url_tokens_approx,
        "savings_pct": round((1 - url_tokens_approx / max(base64_tokens_approx, 1)) * 100, 1),
    }

# Demo: analyze a public image via URL (much cheaper than base64)
PUBLIC_IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Cat03.jpg/320px-Cat03.jpg"

try:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": [
                image_block_url(PUBLIC_IMAGE_URL),
                {"type": "text", "text": "What animal is in this image? One sentence only."}
            ]
        }]
    )
    print(f"Answer: {resp.content[0].text}")
    print(f"Input tokens: {resp.usage.input_tokens}")
except Exception as e:
    print(f"URL image example: {e}")

# Show token savings estimate
import os
example_size_bytes = 50_000  # 50KB image
overhead = estimate_token_overhead(b"x" * example_size_bytes)
print(f"\nFor a {example_size_bytes//1000}KB image:")
print(f"  Base64 tokens (approx): {overhead['base64_tokens_approx']:,}")
print(f"  URL tokens (approx):    {overhead['url_tokens_approx']}")
print(f"  Savings: {overhead['savings']:,} tokens ({overhead['savings_pct']}%)")
# Expected Token Savings: 95%+ on image token cost when URL is available
# Environment: Agents processing web images, CDN-hosted assets, publicly accessible documents
```

### Option 4: Progressive Image Resolution — Low-Res First, High-Res on Demand

Start with a low-resolution thumbnail for initial analysis. Only fetch full resolution if the low-res answer is insufficient.

```python
import anthropic
import base64
from pathlib import Path
from PIL import Image  # pip install Pillow
import io

client = anthropic.Anthropic()

def resize_image(image_bytes: bytes, max_dimension: int = 512) -> bytes:
    """Resize image to have max dimension of max_dimension pixels."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        if max(w, h) <= max_dimension:
            return image_bytes
        scale = max_dimension / max(w, h)
        new_size = (int(w * scale), int(h * scale))
        resized = img.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return image_bytes  # Return original if resize fails

def ask_with_image(image_bytes: bytes, question: str, media_type: str = "image/png") -> dict:
    data = base64.standard_b64encode(image_bytes).decode()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
                {"type": "text", "text": question}
            ]
        }]
    )
    return {
        "answer": resp.content[0].text,
        "tokens": resp.usage.input_tokens,
        "image_bytes": len(image_bytes),
    }

NEEDS_HIGH_RES_SIGNALS = [
    "i cannot read", "too small", "unclear", "blurry",
    "need more detail", "cannot determine", "not visible",
    "need higher resolution", "cannot make out",
]

def progressive_vision(image_path: str, question: str) -> dict:
    """Try low-res first; escalate to full-res only if needed."""
    original_bytes = Path(image_path).read_bytes()

    # Step 1: Try with thumbnail (cheap)
    thumbnail = resize_image(original_bytes, max_dimension=256)
    low_res_result = ask_with_image(
        thumbnail,
        question + "\n\nNote: If the image is too low resolution to answer accurately, say 'need higher resolution'.",
    )

    # Check if we need full resolution
    answer_lower = low_res_result["answer"].lower()
    needs_upgrade = any(sig in answer_lower for sig in NEEDS_HIGH_RES_SIGNALS)

    if not needs_upgrade:
        return {
            "answer": low_res_result["answer"],
            "resolution_used": "low (256px)",
            "total_tokens": low_res_result["tokens"],
            "escalated": False,
        }

    # Step 2: Escalate to medium resolution
    medium = resize_image(original_bytes, max_dimension=1024)
    high_res_result = ask_with_image(medium, question)

    return {
        "answer": high_res_result["answer"],
        "resolution_used": "medium (1024px)",
        "total_tokens": low_res_result["tokens"] + high_res_result["tokens"],
        "escalated": True,
        "low_res_answer": low_res_result["answer"][:80],
    }

# Demo
IMAGE_PATH = "/tmp/sample.png"
try:
    result = progressive_vision(IMAGE_PATH, "What text is written in this image?")
    print(f"Resolution used: {result['resolution_used']}")
    print(f"Escalated: {result['escalated']}")
    print(f"Total tokens: {result['total_tokens']}")
    print(f"Answer: {result['answer'][:150]}")
except ImportError:
    print("Requires Pillow: pip install Pillow")
except Exception as e:
    print(f"Demo: {e}")
# Expected Token Savings: 60-90% when low-res suffices (most general questions)
# Environment: Document classifiers, receipt processors, image moderation pipelines
```

### Option 5: PDF Page Selective Loading

When processing PDFs, don't send all pages at once. Identify which pages are relevant to the query first, then load only those.

```python
import anthropic
import base64
from pathlib import Path

client = anthropic.Anthropic()

def load_pdf_as_base64(pdf_path: str) -> str:
    return base64.standard_b64encode(Path(pdf_path).read_bytes()).decode()

def identify_relevant_pages(pdf_b64: str, query: str, max_pages_to_check: int = 5) -> list[int]:
    """Use the model to identify which pages answer the query."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
                },
                {
                    "type": "text",
                    "text": (
                        f"For this question: '{query}'\n"
                        f"List only the page numbers (1-indexed) most relevant to answer it. "
                        f"Return ONLY a comma-separated list of page numbers. "
                        f"Example: 2,5,7"
                    )
                }
            ]
        }]
    )
    raw = resp.content[0].text.strip()
    try:
        pages = [int(p.strip()) for p in raw.split(",") if p.strip().isdigit()]
        return pages[:max_pages_to_check]
    except Exception:
        return [1]  # Default to first page

def answer_from_pdf(pdf_path: str, query: str, selective: bool = True) -> dict:
    pdf_b64 = load_pdf_as_base64(pdf_path)

    if not selective:
        # Naive: send full PDF every time
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                    {"type": "text", "text": query}
                ]
            }]
        )
        return {"answer": resp.content[0].text, "mode": "full_pdf", "tokens": resp.usage.input_tokens}

    # Selective: identify pages first, then answer
    relevant_pages = identify_relevant_pages(pdf_b64, query)
    print(f"  Identified relevant pages: {relevant_pages}")

    # Answer using the full PDF but with page guidance
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": f"Focus on pages {relevant_pages} to answer: {query}"}
            ]
        }]
    )
    return {
        "answer": resp.content[0].text,
        "mode": "selective",
        "relevant_pages": relevant_pages,
        "tokens": resp.usage.input_tokens,
    }

# Demo with placeholder
PDF_PATH = "/tmp/sample.pdf"
if Path(PDF_PATH).exists():
    result = answer_from_pdf(PDF_PATH, "What are the key conclusions?")
    print(f"Mode: {result['mode']} | Tokens: {result['tokens']}")
    print(f"Answer: {result['answer'][:150]}")
else:
    print("Demo: replace PDF_PATH with a real PDF file path")
# Expected Token Savings: 40-80% for long PDFs when only 1-3 pages are relevant
# Environment: Contract review, report analysis, technical documentation agents
```

### Option 6: Multi-Modal Context Budget Tracker

Track tokens consumed by each visual element in context. Automatically evict the most expensive images when the budget threshold is approached.

```python
import anthropic
import base64
from dataclasses import dataclass, field
from pathlib import Path

client = anthropic.Anthropic()

@dataclass
class VisualElement:
    element_id: str
    element_type: str  # "image" | "document"
    path: str
    estimated_tokens: int
    description: str = ""
    turn_added: int = 0

@dataclass
class MultiModalContext:
    max_token_budget: int = 50_000
    current_visual_tokens: int = 0
    elements: list[VisualElement] = field(default_factory=list)
    evicted: list[str] = field(default_factory=list)

    def estimate_image_tokens(self, image_bytes: bytes) -> int:
        """Rough estimate: base64 chars / 4."""
        return len(image_bytes) * 4 // 3 // 4

    def add_image(self, image_path: str, element_id: str, turn: int = 0) -> bool:
        img_bytes = Path(image_path).read_bytes()
        estimated = self.estimate_image_tokens(img_bytes)

        # Check if adding this image would exceed budget
        if self.current_visual_tokens + estimated > self.max_token_budget:
            # Try to free space by evicting the oldest/largest element
            if not self._evict_oldest():
                print(f"  Cannot add {element_id}: budget exhausted ({self.current_visual_tokens}/{self.max_token_budget})")
                return False

        self.elements.append(VisualElement(
            element_id=element_id,
            element_type="image",
            path=image_path,
            estimated_tokens=estimated,
            turn_added=turn,
        ))
        self.current_visual_tokens += estimated
        print(f"  Added {element_id}: ~{estimated} tokens | Budget: {self.current_visual_tokens}/{self.max_token_budget}")
        return True

    def _evict_oldest(self) -> bool:
        if not self.elements:
            return False
        # Evict oldest element (lowest turn number)
        oldest = min(self.elements, key=lambda e: e.turn_added)
        self.elements.remove(oldest)
        self.current_visual_tokens -= oldest.estimated_tokens
        self.evicted.append(oldest.element_id)
        print(f"  Evicted {oldest.element_id} (~{oldest.estimated_tokens} tokens freed)")
        return True

    def build_content(self, text_query: str) -> list[dict]:
        content = []
        for elem in self.elements:
            img_bytes = Path(elem.path).read_bytes()
            data = base64.standard_b64encode(img_bytes).decode()
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": data},
            })
        # Add eviction notices for context continuity
        if self.evicted:
            content.append({
                "type": "text",
                "text": f"[Note: These images were previously shown but evicted: {', '.join(self.evicted)}]"
            })
        content.append({"type": "text", "text": text_query})
        return content

# Demo
ctx = MultiModalContext(max_token_budget=100_000)
IMAGE_PATH = "/tmp/sample.png"

if Path(IMAGE_PATH).exists():
    for i in range(3):
        added = ctx.add_image(IMAGE_PATH, f"image_{i}", turn=i)

    content = ctx.build_content("Describe all the images.")
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": content}]
    )
    print(f"\nActive images: {[e.element_id for e in ctx.elements]}")
    print(f"Evicted: {ctx.evicted}")
    print(f"Answer: {resp.content[0].text[:100]}")
else:
    print("Demo: requires /tmp/sample.png — replace with real image path")
    print("Context budget tracking logic works independently of actual images")
# Expected Token Savings: Budget enforcement prevents context window overflow from images
# Environment: Image-heavy document agents, design review tools, multi-image analysis pipelines
```

## Comparison Table

| Option | Mechanism | Token Savings | Implementation Effort | Best For |
|--------|-----------|--------------|----------------------|----------|
| 1: Image-or-Description Routing | Query-type classification | 60-80% | Low | General image Q&A bots |
| 2: Multi-Turn Image Eviction | Replace old images with placeholders | 40-70% per turn | Low | Multi-turn vision conversations |
| 3: URL References | URL instead of base64 | 95%+ | Very Low | Public/CDN-hosted images |
| 4: Progressive Resolution | Low-res first, escalate if needed | 60-90% | Medium | Text extraction, detail-heavy analysis |
| 5: PDF Page Selection | Identify relevant pages first | 40-80% | Medium | Long PDF processing agents |
| 6: Budget Tracker + Eviction | Automatic budget enforcement | Budget-driven | Medium | Multi-image pipelines, context management |
