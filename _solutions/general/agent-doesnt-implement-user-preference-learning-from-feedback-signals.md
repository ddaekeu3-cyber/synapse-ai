---
layout: solution
title: "Agent Doesn't Implement User Preference Learning from Feedback Signals"
category: general
description: "Agents that ignore thumbs-up/down signals, corrections, and rephrasing patterns repeat the same stylistic mistakes session after session. These patterns show how to learn user preferences from implicit and explicit feedback."
tags: [general, preferences, feedback, personalization, learning, anthropic]
---

## Problem

A user consistently asks the agent to be more concise, prefers bullet points over prose, and always corrects technical jargon to plain English — but the agent starts fresh each session with no memory of these preferences. Preference learning captures feedback signals (explicit ratings, corrections, rephrasing patterns) and converts them into persistent prompt adjustments that personalize future responses.

---

### Option 1: Explicit Rating Store with Preference Injection

Store thumbs-up/down ratings per response style, then inject the top-rated preferences into each session's system prompt.

```python
import json
from pathlib import Path
from dataclasses import dataclass, asdict, field
import anthropic

client = anthropic.Anthropic()
PREFS_FILE = Path("/tmp/user_prefs.json")

@dataclass
class StylePreference:
    dimension: str    # "length", "format", "tone", "detail_level"
    preferred: str    # e.g., "concise", "bullet_points", "casual", "high"
    score: float      # cumulative score (positive = preferred)
    samples: int      # how many ratings contributed

def load_prefs() -> dict[str, StylePreference]:
    if not PREFS_FILE.exists():
        return {}
    data = json.loads(PREFS_FILE.read_text())
    return {k: StylePreference(**v) for k, v in data.items()}

def save_prefs(prefs: dict[str, StylePreference]) -> None:
    PREFS_FILE.write_text(json.dumps({k: asdict(v) for k, v in prefs.items()}, indent=2))

def record_rating(response: str, rating: int, prefs: dict[str, StylePreference]) -> None:
    """Infer style dimensions from response and update scores."""
    words = len(response.split())
    has_bullets = "•" in response or response.count("\n-") > 2
    is_technical = sum(1 for w in response.split() if len(w) > 10) / max(words, 1) > 0.15

    signals = {
        "length": "concise" if words < 100 else "verbose",
        "format": "bullets" if has_bullets else "prose",
        "tone": "technical" if is_technical else "plain",
    }

    for dim, value in signals.items():
        key = f"{dim}:{value}"
        if key not in prefs:
            prefs[key] = StylePreference(dim, value, 0, 0)
        prefs[key].score += rating
        prefs[key].samples += 1

def build_preference_system(prefs: dict[str, StylePreference]) -> str:
    if not prefs:
        return "You are a helpful assistant."

    # Get top positive preferences
    positive = [p for p in prefs.values() if p.score > 0]
    positive.sort(key=lambda x: x.score, reverse=True)
    top = positive[:4]

    if not top:
        return "You are a helpful assistant."

    pref_lines = []
    for p in top:
        if p.dimension == "length" and p.preferred == "concise":
            pref_lines.append("- Be concise. Aim for under 150 words unless asked for detail.")
        elif p.dimension == "format" and p.preferred == "bullets":
            pref_lines.append("- Use bullet points over prose paragraphs when listing items.")
        elif p.dimension == "tone" and p.preferred == "plain":
            pref_lines.append("- Use plain English. Avoid jargon and technical terms.")
        elif p.dimension == "tone" and p.preferred == "technical":
            pref_lines.append("- Use precise technical terminology. Assume expert audience.")

    if not pref_lines:
        return "You are a helpful assistant."

    return "You are a helpful assistant. User preferences learned from past sessions:\n" + "\n".join(pref_lines)

def chat_with_preferences(user_message: str) -> tuple[str, dict]:
    prefs = load_prefs()
    system = build_preference_system(prefs)
    print(f"[system] {system[:100]}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    reply = response.content[0].text
    return reply, prefs

if __name__ == "__main__":
    # Simulate: user rates responses and prefs accumulate over sessions
    prefs = load_prefs()

    # Session 1: verbose response, user dislikes it (-1)
    verbose_sample = "The concept of containerization is indeed a fascinating topic that deserves careful exploration. In essence, Docker provides a way to package applications and their dependencies into containers..." + " word" * 80
    record_rating(verbose_sample, -1, prefs)

    # Session 2: concise response, user likes it (+1)
    concise_sample = "Docker packages apps + dependencies into containers. Runs consistently across environments. Key commands: build, run, push."
    record_rating(concise_sample, 1, prefs)
    record_rating(concise_sample, 1, prefs)  # multiple likes reinforce

    save_prefs(prefs)
    print(f"[prefs] {len(prefs)} preference dimensions tracked")

    # Session 3: agent now uses learned preferences
    reply, _ = chat_with_preferences("Explain what Kubernetes does.")
    print(f"\nResponse:\n{reply}")

# Expected Token Savings: Preference-aware system prompts prevent repeat corrections; saves clarification round-trips
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Correction Pattern Mining from Edit History

Detect patterns in how users rephrase or correct agent responses and convert them to style rules.

```python
import re
import json
from pathlib import Path
from difflib import SequenceMatcher
import anthropic

client = anthropic.Anthropic()
CORRECTIONS_FILE = Path("/tmp/correction_history.json")

def load_corrections() -> list[dict]:
    if not CORRECTIONS_FILE.exists():
        return []
    return json.loads(CORRECTIONS_FILE.read_text())

def save_correction(original: str, corrected: str) -> None:
    history = load_corrections()
    history.append({"original": original, "corrected": corrected})
    CORRECTIONS_FILE.write_text(json.dumps(history[-50:], indent=2))  # keep last 50

def detect_style_shift(original: str, corrected: str) -> list[str]:
    """Identify what changed in the correction."""
    signals = []
    orig_words = len(original.split())
    corr_words = len(corrected.split())

    if corr_words < orig_words * 0.6:
        signals.append("user prefers shorter responses")
    if corr_words > orig_words * 1.4:
        signals.append("user prefers more detailed responses")

    orig_bullets = original.count("•") + original.count("\n-")
    corr_bullets = corrected.count("•") + corrected.count("\n-")
    if corr_bullets > orig_bullets + 2:
        signals.append("user prefers bullet-point format")
    if corr_bullets == 0 and orig_bullets > 2:
        signals.append("user prefers prose over bullets")

    # Passive → active voice detection
    if re.search(r'\bis\s+\w+ed\b', original) and not re.search(r'\bis\s+\w+ed\b', corrected):
        signals.append("user prefers active voice")

    # Technical jargon removal
    tech_words = re.findall(r'\b[A-Z]{3,}[a-z]*\b', original)
    if tech_words and not any(w in corrected for w in tech_words):
        signals.append(f"user simplifies technical terms ({', '.join(tech_words[:2])})")

    return signals

RULE_SYNTHESIS_PROMPT = """Based on these user correction patterns, generate 3-5 clear writing style rules.

Patterns:
{patterns}

Output as JSON: {{"rules": ["rule 1", "rule 2", ...]}}"""

def synthesize_rules_from_corrections() -> list[str]:
    corrections = load_corrections()
    if len(corrections) < 3:
        return []

    all_signals = []
    for c in corrections:
        signals = detect_style_shift(c["original"], c["corrected"])
        all_signals.extend(signals)

    if not all_signals:
        return []

    # Count signal frequency
    signal_counts = {}
    for s in all_signals:
        signal_counts[s] = signal_counts.get(s, 0) + 1

    # Only use signals that appear in >30% of corrections
    threshold = max(1, len(corrections) * 0.3)
    frequent = [s for s, count in signal_counts.items() if count >= threshold]

    if not frequent:
        return list(signal_counts.keys())[:3]

    return frequent

def chat_with_correction_learning(user_message: str, user_correction: str = None) -> str:
    if user_correction:
        # Find the last response to compare against
        last_response_file = Path("/tmp/last_agent_response.txt")
        if last_response_file.exists():
            last_response = last_response_file.read_text()
            save_correction(last_response, user_correction)
            print(f"[correction recorded] signals: {detect_style_shift(last_response, user_correction)}")

    rules = synthesize_rules_from_corrections()
    system = "You are a helpful assistant."
    if rules:
        system += " Follow these user style preferences:\n" + "\n".join(f"- {r}" for r in rules[:5])
        print(f"[learned rules] {rules}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    reply = response.content[0].text
    Path("/tmp/last_agent_response.txt").write_text(reply)
    return reply

if __name__ == "__main__":
    # Simulate a user correcting verbose responses to shorter ones
    verbose = "The process of containerization is a technology approach that involves encapsulating an application and its dependencies into a container. This container can then run consistently across different computing environments."
    concise = "Containerization packages apps + deps into portable containers that run consistently anywhere."

    save_correction(verbose, concise)
    save_correction(verbose + " additional verbose text here.", "Short: containers = portable app packages.")

    reply = chat_with_correction_learning("What is serverless computing?")
    print(f"\nResponse:\n{reply}")

# Expected Token Savings: Learned rules prevent repeated back-and-forth corrections; saves 1-3 turns per session
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Implicit Preference Inference from Conversation Signals

Detect engagement patterns (message length, follow-up questions, topic returns) to infer preferences without explicit ratings.

```python
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
import anthropic

client = anthropic.Anthropic()
ENGAGEMENT_FILE = Path("/tmp/engagement_signals.json")

@dataclass
class EngagementSignal:
    timestamp: float
    response_words: int
    user_followup_words: int     # longer followup = more engaged with topic
    time_to_reply_s: float       # shorter = more engaging
    asked_for_more: bool         # "tell me more", "expand", "continue"
    corrected: bool              # "actually", "no wait", "I meant"
    topic: str

def load_signals() -> list[EngagementSignal]:
    if not ENGAGEMENT_FILE.exists():
        return []
    data = json.loads(ENGAGEMENT_FILE.read_text())
    return [EngagementSignal(**d) for d in data]

def record_signal(signal: EngagementSignal) -> None:
    signals = load_signals()
    signals.append(signal)
    ENGAGEMENT_FILE.write_text(json.dumps([asdict(s) for s in signals[-100:]], indent=2))

def infer_preferences(signals: list[EngagementSignal]) -> dict:
    if len(signals) < 5:
        return {}

    prefs = {}

    # Response length preference
    engaged = [s for s in signals if s.asked_for_more or s.user_followup_words > 50]
    unengaged = [s for s in signals if s.corrected or s.time_to_reply_s > 300]

    if engaged:
        avg_engaged_len = sum(s.response_words for s in engaged) / len(engaged)
        if avg_engaged_len < 100:
            prefs["preferred_length"] = "concise (<100 words)"
        elif avg_engaged_len < 250:
            prefs["preferred_length"] = "medium (100-250 words)"
        else:
            prefs["preferred_length"] = "detailed (250+ words)"

    # Topic preferences based on engagement
    topic_scores = {}
    for s in signals:
        if s.topic:
            score = (1 if s.asked_for_more else 0) - (1 if s.corrected else 0)
            topic_scores[s.topic] = topic_scores.get(s.topic, 0) + score

    prefs["preferred_topics"] = [t for t, score in topic_scores.items() if score > 0][:3]
    prefs["disliked_topics"] = [t for t, score in topic_scores.items() if score < -1][:2]

    return prefs

PREFERENCE_AWARE_PROMPT = """You are a helpful assistant. Adapt your responses based on these learned user engagement patterns:
{preferences}

Be responsive to these patterns — they reflect what this user finds most valuable."""

def engaged_chat(user_message: str, topic: str = "general") -> tuple[str, float]:
    signals = load_signals()
    prefs = infer_preferences(signals)

    system = "You are a helpful assistant."
    if prefs:
        pref_lines = []
        if "preferred_length" in prefs:
            pref_lines.append(f"Preferred response length: {prefs['preferred_length']}")
        if prefs.get("preferred_topics"):
            pref_lines.append(f"User engages deeply with: {', '.join(prefs['preferred_topics'])}")
        if pref_lines:
            system = PREFERENCE_AWARE_PROMPT.format(preferences="\n".join(pref_lines))

    start = time.monotonic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    reply = response.content[0].text
    elapsed = time.monotonic() - start

    asked_for_more = any(kw in user_message.lower() for kw in ["more", "expand", "detail", "continue"])
    corrected = any(kw in user_message.lower() for kw in ["actually", "no wait", "i meant", "wrong"])

    # Simulate recording engagement (in real use, these would come from next user message)
    record_signal(EngagementSignal(
        timestamp=start,
        response_words=len(reply.split()),
        user_followup_words=len(user_message.split()),
        time_to_reply_s=elapsed,
        asked_for_more=asked_for_more,
        corrected=corrected,
        topic=topic,
    ))

    return reply, elapsed

if __name__ == "__main__":
    # Simulate engagement signals accumulating over time
    for i in range(8):
        record_signal(EngagementSignal(
            timestamp=time.time() - (8 - i) * 60,
            response_words=80 if i % 2 == 0 else 300,
            user_followup_words=60 if i % 2 == 0 else 15,
            time_to_reply_s=30,
            asked_for_more=(i % 2 == 0),
            corrected=(i % 2 == 1),
            topic="python" if i < 4 else "databases",
        ))

    signals = load_signals()
    prefs = infer_preferences(signals)
    print(f"Inferred preferences: {prefs}")

    reply, _ = engaged_chat("Explain database indexing strategies.", topic="databases")
    print(f"\nResponse:\n{reply[:400]}")

# Expected Token Savings: Implicit signals require no extra API calls; reduces clarification loops
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Async Preference Update Pipeline — Decouple Learning from Serving

Separate the preference learning from the serving path so feedback processing doesn't add latency.

```python
import asyncio
import json
from pathlib import Path
from dataclasses import dataclass, asdict, field
import anthropic

client = anthropic.AsyncAnthropic()
PREF_STORE = Path("/tmp/async_prefs.json")

@dataclass
class Feedback:
    response_id: str
    response_text: str
    rating: int         # -1, 0, 1
    dimension_notes: str = ""

@dataclass
class PreferenceModel:
    rules: list[str] = field(default_factory=list)
    total_feedback: int = 0
    last_updated: float = 0.0

FEEDBACK_QUEUE: asyncio.Queue = asyncio.Queue()

PREFERENCE_UPDATE_PROMPT = """You are a preference learning system. Update user style preferences based on feedback.

Current rules:
{current_rules}

New feedback:
- Response: {response_text}
- Rating: {rating} ({rating_label})
- Notes: {notes}

Generate updated rules (3-6 rules max). If rating is negative, invert or remove related rules.
Output JSON: {{"rules": ["rule 1", "rule 2", ...]}}"""

async def process_feedback_background(pref_model: PreferenceModel) -> None:
    """Background task: process feedback queue and update preference model."""
    while True:
        try:
            feedback: Feedback = await asyncio.wait_for(FEEDBACK_QUEUE.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        rating_label = {1: "positive", 0: "neutral", -1: "negative"}.get(feedback.rating, "unknown")
        current_rules = "\n".join(f"- {r}" for r in pref_model.rules) or "None yet"

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": PREFERENCE_UPDATE_PROMPT.format(
                    current_rules=current_rules,
                    response_text=feedback.response_text[:300],
                    rating=feedback.rating,
                    rating_label=rating_label,
                    notes=feedback.dimension_notes or "none",
                ),
            }],
        )

        import re
        raw = response.content[0].text
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                pref_model.rules = data.get("rules", pref_model.rules)
                pref_model.total_feedback += 1
                print(f"[pref-update] {len(pref_model.rules)} rules after {pref_model.total_feedback} feedbacks")
            except Exception:
                pass

        FEEDBACK_QUEUE.task_done()

async def serve_response(user_message: str, pref_model: PreferenceModel) -> tuple[str, str]:
    system = "You are a helpful assistant."
    if pref_model.rules:
        rules_text = "\n".join(f"- {r}" for r in pref_model.rules)
        system = f"You are a helpful assistant. Style preferences:\n{rules_text}"

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    reply = response.content[0].text
    import uuid
    return str(uuid.uuid4())[:8], reply

async def submit_feedback(response_id: str, response_text: str, rating: int, notes: str = ""):
    await FEEDBACK_QUEUE.put(Feedback(response_id, response_text, rating, notes))

async def run_demo():
    pref_model = PreferenceModel()

    # Start background preference processor
    bg_task = asyncio.create_task(process_feedback_background(pref_model))

    # Serve responses and collect feedback
    queries = [
        ("Explain REST APIs.", "too verbose, prefers concise"),
        ("What is a database index?", "good length, liked it"),
        ("Explain microservices.", "good"),
    ]
    ratings = [-1, 1, 1]

    for (query, note), rating in zip(queries, ratings):
        resp_id, reply = await serve_response(query, pref_model)
        print(f"\nQ: {query}\nA: {reply[:150]}")
        await submit_feedback(resp_id, reply, rating, note)
        await asyncio.sleep(0.1)  # let background task process

    # Wait for feedback queue to drain
    await FEEDBACK_QUEUE.join()
    bg_task.cancel()

    print(f"\n=== Final preferences ({pref_model.total_feedback} feedbacks) ===")
    for rule in pref_model.rules:
        print(f"  - {rule}")

if __name__ == "__main__":
    asyncio.run(run_demo())

# Expected Token Savings: Feedback processed async (no serving latency); Haiku for learning = 4x cheaper
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Multi-Dimensional Preference Profile with Domain Routing

Maintain separate preference profiles per topic domain and select the right profile at routing time.

```python
import json
import re
from pathlib import Path
from dataclasses import dataclass, field, asdict
import anthropic

client = anthropic.Anthropic()
PROFILES_FILE = Path("/tmp/domain_profiles.json")

DOMAINS = ["technical", "business", "casual", "creative"]

@dataclass
class DomainProfile:
    domain: str
    preferred_length: str = "medium"   # short/medium/long
    preferred_format: str = "prose"    # prose/bullets/structured
    preferred_tone: str = "neutral"    # formal/neutral/casual
    example_positive: str = ""         # exemplar of liked response
    rating_sum: float = 0.0
    rating_count: int = 0

    def update(self, rating: int, response: str):
        self.rating_sum += rating
        self.rating_count += 1
        if rating > 0 and not self.example_positive:
            self.example_positive = response[:200]

    def to_system_instruction(self) -> str:
        return (
            f"Length: {self.preferred_length}. "
            f"Format: {self.preferred_format}. "
            f"Tone: {self.preferred_tone}."
        )

def load_profiles() -> dict[str, DomainProfile]:
    if not PROFILES_FILE.exists():
        return {d: DomainProfile(domain=d) for d in DOMAINS}
    data = json.loads(PROFILES_FILE.read_text())
    return {d: DomainProfile(**v) for d, v in data.items()}

def save_profiles(profiles: dict[str, DomainProfile]) -> None:
    PROFILES_FILE.write_text(json.dumps({k: asdict(v) for k, v in profiles.items()}, indent=2))

DOMAIN_CLASSIFIER_PROMPT = """Classify this message into one domain: technical, business, casual, creative.
Message: {message}
Respond with one word:"""

def classify_domain(message: str) -> str:
    """Classify query domain using a cheap call."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=5,
        messages=[{
            "role": "user",
            "content": DOMAIN_CLASSIFIER_PROMPT.format(message=message[:200]),
        }],
    )
    domain = response.content[0].text.strip().lower()
    return domain if domain in DOMAINS else "casual"

def domain_aware_chat(user_message: str, rating_for_last: int = None,
                       last_response: str = None, last_domain: str = None) -> tuple[str, str]:
    profiles = load_profiles()

    # Record rating for previous response
    if rating_for_last is not None and last_domain and last_response:
        profiles[last_domain].update(rating_for_last, last_response)
        save_profiles(profiles)
        print(f"[rated {last_domain}: {rating_for_last:+d}]")

    # Classify current query
    domain = classify_domain(user_message)
    profile = profiles[domain]
    print(f"[domain={domain}] {profile.to_system_instruction()}")

    system = (
        f"You are a helpful assistant. "
        f"User preferences for {domain} topics: {profile.to_system_instruction()}"
    )

    # Include positive example if available
    if profile.example_positive:
        system += f"\n\nExample of a well-received response style:\n{profile.example_positive}"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text, domain

if __name__ == "__main__":
    # Simulate building up domain profiles from past feedback
    profiles = load_profiles()

    # Technical domain: user likes concise bullet points
    profiles["technical"].preferred_length = "short"
    profiles["technical"].preferred_format = "bullets"
    profiles["technical"].preferred_tone = "formal"

    # Business domain: user likes structured prose
    profiles["business"].preferred_length = "medium"
    profiles["business"].preferred_format = "structured"
    profiles["business"].preferred_tone = "formal"

    save_profiles(profiles)

    queries = [
        "How does database connection pooling work?",      # technical
        "What are the key metrics for a SaaS business?",  # business
        "Got any tips for staying focused while coding?",  # casual
    ]

    last_reply, last_domain = None, None
    for query in queries:
        print(f"\nQ: {query}")
        reply, domain = domain_aware_chat(query, rating_for_last=1,
                                          last_response=last_reply, last_domain=last_domain)
        print(f"A: {reply[:200]}")
        last_reply, last_domain = reply, domain

# Expected Token Savings: Domain classification uses Haiku; preferences prevent stylistic rework across sessions
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Preference Distillation — Compress History into Few-Shot Examples

Convert the entire preference history into 2-3 few-shot examples that efficiently encode style without long rule lists.

```python
import json
from pathlib import Path
import anthropic

client = anthropic.Anthropic()
HISTORY_FILE = Path("/tmp/preference_history.json")

def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    return json.loads(HISTORY_FILE.read_text())

def save_interaction(question: str, response: str, rating: int) -> None:
    history = load_history()
    history.append({"question": question, "response": response, "rating": rating})
    HISTORY_FILE.write_text(json.dumps(history[-30:], indent=2))

DISTILL_PROMPT = """Analyze these rated interactions and create 2-3 few-shot examples that best demonstrate the user's preferred response style.

Interactions (rating: +1=liked, -1=disliked):
{interactions}

Select or synthesize examples that show what the user LIKES. Each example should be a Q&A pair that demonstrates preferred style.

Output JSON:
{{"examples": [{{"q": "question", "a": "ideal response"}}, ...]}}"""

def distill_few_shot_examples() -> list[dict]:
    history = load_history()
    if len(history) < 4:
        return []

    liked = [h for h in history if h["rating"] > 0]
    if not liked:
        return []

    interactions_text = "\n\n".join(
        f"[rating={h['rating']}]\nQ: {h['question'][:100]}\nA: {h['response'][:200]}"
        for h in history[-15:]
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": DISTILL_PROMPT.format(interactions=interactions_text),
        }],
    )

    import re
    match = re.search(r'\{.*\}', response.content[0].text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return data.get("examples", [])
        except Exception:
            pass
    return []

def chat_with_few_shot_prefs(user_message: str) -> str:
    examples = distill_few_shot_examples()
    messages = []

    # Prepend few-shot examples as conversation history
    for ex in examples[:2]:
        messages.append({"role": "user", "content": ex["q"]})
        messages.append({"role": "assistant", "content": ex["a"]})

    messages.append({"role": "user", "content": user_message})

    if examples:
        print(f"[few-shot] {len(examples)} style examples injected")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=messages,
    )
    return response.content[0].text

if __name__ == "__main__":
    # Seed history with some rated interactions
    pairs = [
        ("What is a REST API?", "REST (Representational State Transfer) is an architectural style for building web services. It uses HTTP methods and is stateless.", 1),
        ("Explain caching.", "Caching stores frequently accessed data in fast memory to reduce latency and database load. Common types: in-memory (Redis), CDN, browser. Use TTL to expire stale data.", 1),
        ("What is Docker?", "Docker is a platform that uses OS-level virtualization to deliver software in packages called containers. Containers are isolated from each other and bundle their own software, libraries and configuration files...", -1),
    ]

    for q, a, r in pairs:
        save_interaction(q, a, r)

    print("Chatting with distilled preference examples...")
    reply = chat_with_few_shot_prefs("What is GraphQL?")
    print(f"\nResponse:\n{reply}")

    # Record this for future preference learning
    save_interaction("What is GraphQL?", reply, 0)  # neutral until user rates

# Expected Token Savings: 2-3 few-shot examples are more token-efficient than long rule lists; compress 30 turns into 200 tokens
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Signal Type | Storage | Latency Impact | Best For |
|--------|------------|---------|---------------|----------|
| 1 | Explicit ratings | JSON file | None (pre-call injection) | Simple thumbs-up/down feedback |
| 2 | Edit/correction diffs | JSON file | None | Users who rephrase responses |
| 3 | Implicit engagement patterns | JSON file | None | No explicit feedback mechanism |
| 4 | Async feedback queue | In-memory | Zero (background) | High-throughput production agents |
| 5 | Per-domain profiles | JSON file | +1 cheap classify call | Mixed-domain assistants |
| 6 | Distilled few-shot examples | JSON file | None | Compressing long preference histories |
