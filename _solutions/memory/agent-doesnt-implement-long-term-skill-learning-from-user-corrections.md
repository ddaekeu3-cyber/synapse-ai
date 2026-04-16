---
layout: solution
title: "Agent Doesn't Implement Long-Term Skill Learning from User Corrections"
category: memory
description: "When users correct the agent, store those corrections as durable skills — so the same mistake is never made twice and the agent continuously improves across sessions."
tags: [memory, learning, corrections, skill, personalization, fine-tuning, few-shot]
---

## Problem

Users correct agents constantly: "No, format it as a table not a list", "Don't include disclaimers", "Always use British spelling", "I prefer shorter answers". Agents acknowledge the correction in the moment but forget it the next session. The user corrects the same behavior ten times. The agent never improves. This erodes trust and wastes user effort — every correction should be a lasting investment.

```python
# Naive: correction acknowledged but never persisted
def respond(user_message: str, correction: str = None) -> str:
    if correction:
        print(f"Got it! I'll do that.")  # acknowledged...
    # ...but the next session starts fresh with no memory of it
    return client.messages.create(...).content[0].text
```

## Solution Options

### Option 1: Correction Extractor with JSON Skill Store

Detect corrections in user messages, extract the rule they imply, and persist it to a JSON skill file that is injected into future sessions.

```python
import anthropic
import json
from pathlib import Path
from dataclasses import dataclass, field

SKILLS_FILE = Path("agent_skills.json")

@dataclass
class LearnedSkill:
    trigger: str          # what context triggers this skill
    rule: str             # the behavior rule to apply
    example_correction: str
    confidence: float = 1.0
    application_count: int = 0

def load_skills() -> list[LearnedSkill]:
    if not SKILLS_FILE.exists():
        return []
    data = json.loads(SKILLS_FILE.read_text())
    return [LearnedSkill(**s) for s in data]

def save_skills(skills: list[LearnedSkill]) -> None:
    SKILLS_FILE.write_text(json.dumps(
        [vars(s) for s in skills], indent=2
    ))

client = anthropic.Anthropic()

EXTRACT_PROMPT = """Does this user message correct or redirect the agent's behavior?
If yes, extract the implied rule.

User message: {message}

Return JSON:
{{
  "is_correction": true/false,
  "trigger": "<what context this applies to>",
  "rule": "<the behavioral rule to learn>",
  "confidence": <0.0-1.0>
}}

Examples of corrections: "use a table", "be more concise", "don't use bullet points",
"always include code examples", "use British spelling"."""

def detect_and_learn(user_message: str) -> LearnedSkill | None:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": EXTRACT_PROMPT.format(message=user_message)}],
    )
    try:
        data = json.loads(r.content[0].text)
        if not data["is_correction"]:
            return None
        skill = LearnedSkill(
            trigger=data["trigger"],
            rule=data["rule"],
            example_correction=user_message,
            confidence=data["confidence"],
        )
        skills = load_skills()
        # Check for duplicate rule
        for existing in skills:
            if existing.trigger.lower() == skill.trigger.lower():
                existing.rule = skill.rule  # update existing
                save_skills(skills)
                print(f"[SKILL UPDATED] {skill.trigger}: {skill.rule}")
                return skill
        skills.append(skill)
        save_skills(skills)
        print(f"[SKILL LEARNED] {skill.trigger}: {skill.rule}")
        return skill
    except Exception:
        return None

def build_skills_system_prompt(base_prompt: str) -> str:
    skills = load_skills()
    if not skills:
        return base_prompt
    skill_lines = "\n".join(f"- {s.rule}" for s in skills)
    return f"{base_prompt}\n\nLearned user preferences (always apply these):\n{skill_lines}"

def respond(user_message: str, base_system: str = "You are a helpful assistant.") -> str:
    # Check for correction first
    detect_and_learn(user_message)
    # Apply all learned skills
    system = build_skills_system_prompt(base_system)
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return r.content[0].text


# Session 1: user corrects formatting
print("Session 1:")
print(respond("List the top 5 Python libraries"))
print(respond("Please use a table format instead of bullet points"))

# Session 2: skill is remembered
print("\nSession 2 (new session, same agent):")
print(respond("List the top 5 JavaScript frameworks"))  # should use table format

# Expected Token Savings: Skill injection adds ~100 tokens/session; eliminates repeated correction overhead
# Environment: ANTHROPIC_API_KEY
```

### Option 2: Few-Shot Example Library Built from Corrections

When a user provides a corrected version of a response, store the (original_request, corrected_response) pair as a few-shot example. Inject relevant examples into future prompts via semantic similarity.

```python
import anthropic
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class FewShotExample:
    request: str
    bad_response_summary: str
    good_response: str
    domain: str
    tags: list[str]

EXAMPLES_FILE = Path("few_shot_examples.json")

def load_examples() -> list[FewShotExample]:
    if not EXAMPLES_FILE.exists():
        return []
    return [FewShotExample(**e) for e in json.loads(EXAMPLES_FILE.read_text())]

def save_example(example: FewShotExample) -> None:
    examples = load_examples()
    examples.append(example)
    EXAMPLES_FILE.write_text(json.dumps([vars(e) for e in examples], indent=2))

client = anthropic.Anthropic()

EXTRACT_EXAMPLE_PROMPT = """The user is correcting an agent response. Extract a few-shot example.

Original request: {request}
User's correction/improvement: {correction}

Return JSON:
{{
  "bad_response_summary": "<what was wrong in one sentence>",
  "good_response": "<the correct response based on the correction>",
  "domain": "<topic domain>",
  "tags": ["<tag1>", "<tag2>"]
}}"""

def learn_from_correction(request: str, correction: str) -> FewShotExample | None:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": EXTRACT_EXAMPLE_PROMPT.format(
            request=request, correction=correction,
        )}],
    )
    try:
        data = json.loads(r.content[0].text)
        example = FewShotExample(
            request=request,
            bad_response_summary=data["bad_response_summary"],
            good_response=data["good_response"],
            domain=data["domain"],
            tags=data["tags"],
        )
        save_example(example)
        print(f"[FEW-SHOT STORED] domain={example.domain} tags={example.tags}")
        return example
    except Exception:
        return None

def _keyword_similarity(a: str, b: str) -> float:
    wa = set(a.lower().split()) - {"a", "an", "the", "to", "for", "of"}
    wb = set(b.lower().split()) - {"a", "an", "the", "to", "for", "of"}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)

def retrieve_relevant_examples(query: str, k: int = 2) -> list[FewShotExample]:
    examples = load_examples()
    if not examples:
        return []
    scored = [(e, _keyword_similarity(query, e.request + " " + " ".join(e.tags))) for e in examples]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [e for e, score in scored[:k] if score > 0.05]

def respond_with_learned_examples(user_message: str) -> str:
    examples = retrieve_relevant_examples(user_message)
    if examples:
        ex_text = "\n\n".join(
            f"Example request: {e.request}\n"
            f"Correct response style: {e.good_response}\n"
            f"(Avoid: {e.bad_response_summary})"
            for e in examples
        )
        system = f"You are a helpful assistant.\n\nLearned examples to follow:\n{ex_text}"
    else:
        system = "You are a helpful assistant."

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return r.content[0].text


# Learn from a correction
learn_from_correction(
    request="Explain how to sort a list in Python",
    correction="Good start, but please always include a runnable code example at the end, "
               "not just a description. Here's what I want: [code example]"
)

# Future response applies the learned pattern
print(respond_with_learned_examples("How do I filter a list in Python?"))
print(respond_with_learned_examples("How do I reverse a string in Python?"))

# Expected Token Savings: Few-shot examples add ~200 tokens; saves repeated corrections worth many more
# Environment: ANTHROPIC_API_KEY
```

### Option 3: Structured Preference Profile with Conflict Resolution

Maintain a typed preference profile (format, tone, length, domain-specific rules). Detect conflicts between new corrections and existing preferences, resolve them, and update the profile.

```python
import anthropic
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

@dataclass
class PreferenceProfile:
    output_format: str = "default"         # "table" | "bullet" | "prose" | "code-first" | "default"
    response_length: str = "balanced"      # "terse" | "balanced" | "detailed"
    tone: str = "professional"             # "casual" | "professional" | "academic"
    include_code_examples: bool = True
    include_disclaimers: bool = True
    language_variant: str = "American"     # "American" | "British" | "Australian"
    domain_rules: dict[str, str] = field(default_factory=dict)  # domain → custom rule

PROFILE_FILE = Path("user_preference_profile.json")

def load_profile() -> PreferenceProfile:
    if not PROFILE_FILE.exists():
        return PreferenceProfile()
    data = json.loads(PROFILE_FILE.read_text())
    return PreferenceProfile(**data)

def save_profile(profile: PreferenceProfile) -> None:
    PROFILE_FILE.write_text(json.dumps(vars(profile), indent=2))

client = anthropic.Anthropic()

UPDATE_PROFILE_PROMPT = """Based on this user correction, update the preference profile.

Current profile: {current_profile}

User correction: {correction}

Return JSON with only the fields that should change:
{{
  "output_format": "<if changed>",
  "response_length": "<if changed>",
  "tone": "<if changed>",
  "include_code_examples": <if changed>,
  "include_disclaimers": <if changed>,
  "language_variant": "<if changed>",
  "domain_rules": {{"<domain>": "<rule>"}}
}}

Return only changed fields. Return {{}} if no profile changes."""

def update_profile_from_correction(correction: str) -> dict[str, str]:
    profile = load_profile()
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": UPDATE_PROFILE_PROMPT.format(
            current_profile=json.dumps(vars(profile)),
            correction=correction,
        )}],
    )
    try:
        changes = json.loads(r.content[0].text)
        if not changes:
            return {}
        # Apply changes
        for key, value in changes.items():
            if key == "domain_rules" and isinstance(value, dict):
                profile.domain_rules.update(value)
            elif hasattr(profile, key):
                setattr(profile, key, value)
        save_profile(profile)
        print(f"[PROFILE UPDATED] Changes: {changes}")
        return changes
    except Exception:
        return {}

def profile_to_system_instructions(profile: PreferenceProfile) -> str:
    instructions = []
    if profile.output_format != "default":
        instructions.append(f"Always format responses as: {profile.output_format}")
    if profile.response_length == "terse":
        instructions.append("Keep responses concise — prefer brevity over completeness.")
    elif profile.response_length == "detailed":
        instructions.append("Provide thorough, detailed responses.")
    if profile.tone == "casual":
        instructions.append("Use a casual, friendly tone.")
    if not profile.include_code_examples:
        instructions.append("Do not include code examples unless explicitly requested.")
    if not profile.include_disclaimers:
        instructions.append("Do not include disclaimers, caveats, or safety warnings.")
    if profile.language_variant == "British":
        instructions.append("Use British spelling (colour, behaviour, centre, etc.)")
    for domain, rule in profile.domain_rules.items():
        instructions.append(f"For {domain} topics: {rule}")
    return "\n".join(instructions)

def respond(user_message: str) -> str:
    # Check if message is a correction
    if any(k in user_message.lower() for k in ["don't", "please", "instead", "always", "never", "prefer"]):
        update_profile_from_correction(user_message)

    profile = load_profile()
    instructions = profile_to_system_instructions(profile)
    system = "You are a helpful assistant."
    if instructions:
        system += f"\n\nUser preferences:\n{instructions}"

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return r.content[0].text


# Apply several corrections to build profile
respond("Don't include disclaimers in your responses")
respond("Please use British spelling always")
respond("Keep responses terse and to the point")

# Future response applies full profile
print("\nResponse with learned preferences:")
print(respond("What are the main differences between Python 2 and Python 3?"))

# Expected Token Savings: Profile injection adds ~80 tokens; eliminates repeated correction overhead per session
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Async Correction Pipeline with Priority Decay

Corrections accumulate over time. Recent corrections carry more weight. Use exponential decay to ensure old corrections fade while frequently-repeated corrections stay strong.

```python
import anthropic
import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class WeightedSkill:
    rule: str
    weight: float
    last_corrected: float
    correction_count: int
    decay_halflife_hours: float = 168.0  # 1 week

    def effective_weight(self) -> float:
        age_hours = (time.time() - self.last_corrected) / 3600
        decay = math.exp(-age_hours * math.log(2) / self.decay_halflife_hours)
        return self.weight * decay

    def reinforce(self, additional_weight: float = 0.5) -> None:
        self.weight = min(1.0, self.effective_weight() + additional_weight)
        self.last_corrected = time.time()
        self.correction_count += 1

WEIGHTED_SKILLS_FILE = Path("weighted_skills.json")

def load_weighted_skills() -> list[WeightedSkill]:
    if not WEIGHTED_SKILLS_FILE.exists():
        return []
    return [WeightedSkill(**s) for s in json.loads(WEIGHTED_SKILLS_FILE.read_text())]

def save_weighted_skills(skills: list[WeightedSkill]) -> None:
    WEIGHTED_SKILLS_FILE.write_text(json.dumps([vars(s) for s in skills], indent=2))

client = anthropic.AsyncAnthropic()

async def extract_rule_async(correction: str) -> str | None:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content":
            f"Extract the behavioral rule from this correction in one short sentence.\n"
            f"Correction: {correction}\nReturn the rule only, or 'none' if not a correction."}],
    )
    rule = r.content[0].text.strip()
    return None if rule.lower() == "none" else rule

async def process_correction_async(correction: str) -> WeightedSkill | None:
    rule = await extract_rule_async(correction)
    if not rule:
        return None
    skills = load_weighted_skills()
    # Check if similar rule exists
    for skill in skills:
        if _rules_similar(skill.rule, rule):
            skill.reinforce()
            save_weighted_skills(skills)
            print(f"[SKILL REINFORCED] weight={skill.effective_weight():.2f} count={skill.correction_count}: {skill.rule}")
            return skill
    new_skill = WeightedSkill(
        rule=rule,
        weight=0.8,
        last_corrected=time.time(),
        correction_count=1,
    )
    skills.append(new_skill)
    save_weighted_skills(skills)
    print(f"[SKILL LEARNED] weight={new_skill.weight}: {rule}")
    return new_skill

def _rules_similar(a: str, b: str) -> bool:
    wa = set(a.lower().split()) - {"a", "an", "the", "always", "never", "please"}
    wb = set(b.lower().split()) - {"a", "an", "the", "always", "never", "please"}
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) > 0.5

def build_weighted_system(base: str, min_weight: float = 0.3) -> str:
    skills = load_weighted_skills()
    active = [s for s in skills if s.effective_weight() >= min_weight]
    active.sort(key=lambda s: s.effective_weight(), reverse=True)
    if not active:
        return base
    rules = "\n".join(
        f"- {s.rule} [confidence: {s.effective_weight():.0%}]"
        for s in active[:5]  # top 5 by weight
    )
    return f"{base}\n\nLearned user preferences (apply in proportion to confidence):\n{rules}"

async def async_respond(user_message: str, base_system: str = "You are a helpful assistant.") -> str:
    # Detect corrections in parallel with building response
    correction_task = asyncio.create_task(process_correction_async(user_message))
    system = build_weighted_system(base_system)
    response_r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    await correction_task  # ensure learning completes
    return response_r.content[0].text

async def main():
    await async_respond("Please always format code in Python using type hints")
    await async_respond("Don't add closing remarks like 'I hope this helps'")
    await async_respond("Please always format code in Python using type hints")  # reinforcement

    print("\nResponse with weighted skills:")
    result = await async_respond("How do I implement a binary search in Python?")
    print(result[:400])

asyncio.run(main())

# Expected Token Savings: Decay prunes stale skills; reinforcement keeps active ones; ~80 tokens overhead
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Domain-Scoped Skill Routing

Different domains (coding, writing, analysis) may have different learned behaviors. Route corrections to the appropriate domain bucket and inject only relevant skills per query.

```python
import anthropic
import json
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class DomainSkill:
    domain: str
    rule: str
    example: str

DOMAIN_SKILLS_FILE = Path("domain_skills.json")
KNOWN_DOMAINS = ["coding", "writing", "analysis", "math", "general"]

def load_domain_skills() -> dict[str, list[DomainSkill]]:
    if not DOMAIN_SKILLS_FILE.exists():
        return {d: [] for d in KNOWN_DOMAINS}
    raw = json.loads(DOMAIN_SKILLS_FILE.read_text())
    return {domain: [DomainSkill(**s) for s in skills] for domain, skills in raw.items()}

def save_domain_skills(skills_by_domain: dict[str, list[DomainSkill]]) -> None:
    DOMAIN_SKILLS_FILE.write_text(json.dumps(
        {d: [vars(s) for s in skills] for d, skills in skills_by_domain.items()}, indent=2
    ))

client = anthropic.Anthropic()

CLASSIFY_AND_EXTRACT_PROMPT = """Classify this user correction and extract the rule.

Correction: {correction}
Known domains: {domains}

Return JSON:
{{
  "is_correction": true/false,
  "domain": "<one of the known domains>",
  "rule": "<behavioral rule in one sentence>"
}}"""

def learn_domain_correction(correction: str) -> DomainSkill | None:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=120,
        messages=[{"role": "user", "content": CLASSIFY_AND_EXTRACT_PROMPT.format(
            correction=correction,
            domains=", ".join(KNOWN_DOMAINS),
        )}],
    )
    try:
        data = json.loads(r.content[0].text)
        if not data["is_correction"]:
            return None
        skill = DomainSkill(
            domain=data["domain"],
            rule=data["rule"],
            example=correction[:80],
        )
        all_skills = load_domain_skills()
        domain_skills = all_skills.get(skill.domain, [])
        # Deduplicate
        existing_rules = {s.rule.lower() for s in domain_skills}
        if skill.rule.lower() not in existing_rules:
            domain_skills.append(skill)
            all_skills[skill.domain] = domain_skills
            save_domain_skills(all_skills)
            print(f"[DOMAIN SKILL] [{skill.domain}] {skill.rule}")
        return skill
    except Exception:
        return None

CLASSIFY_QUERY_PROMPT = """Which domain does this query belong to?
Domains: {domains}
Query: {query}
Return JSON: {{"domain": "<domain>"}}"""

def classify_query(query: str) -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=40,
        messages=[{"role": "user", "content": CLASSIFY_QUERY_PROMPT.format(
            domains=", ".join(KNOWN_DOMAINS), query=query,
        )}],
    )
    try:
        return json.loads(r.content[0].text)["domain"]
    except Exception:
        return "general"

def respond_with_domain_skills(user_message: str) -> str:
    # Check for correction
    learn_domain_correction(user_message)

    # Classify query domain
    domain = classify_query(user_message)
    all_skills = load_domain_skills()
    domain_skills = all_skills.get(domain, []) + all_skills.get("general", [])

    system = "You are a helpful assistant."
    if domain_skills:
        rules = "\n".join(f"- {s.rule}" for s in domain_skills[:5])
        system += f"\n\nLearned preferences for {domain} questions:\n{rules}"
        print(f"[ROUTING] domain={domain} applying {len(domain_skills)} skill(s)")

    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return r.content[0].text


# Learn domain-specific corrections
respond_with_domain_skills("For coding answers, always show the imports at the top")
respond_with_domain_skills("For writing tasks, use active voice throughout")
respond_with_domain_skills("For analysis tasks, always include a confidence level")

# Domain-routed responses
print("\nCoding response:")
print(respond_with_domain_skills("How do I parse JSON in Python?")[:300])
print("\nWriting response:")
print(respond_with_domain_skills("Write a paragraph about cloud computing")[:300])

# Expected Token Savings: Domain-scoped injection avoids injecting irrelevant rules; targeted ~50 tokens per query
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Correction-Driven Prompt Auto-Optimizer

Collect corrections over multiple sessions and periodically run an optimization pass that rewrites the system prompt to naturally incorporate all learned rules.

```python
import anthropic
import json
from dataclasses import dataclass
from pathlib import Path

CORRECTIONS_LOG = Path("corrections_log.json")
OPTIMIZED_PROMPT_FILE = Path("optimized_system_prompt.txt")

@dataclass
class CorrectionRecord:
    session_id: str
    turn: int
    correction_text: str
    extracted_rule: str

def log_correction(session_id: str, turn: int, correction: str, rule: str) -> None:
    records = []
    if CORRECTIONS_LOG.exists():
        records = json.loads(CORRECTIONS_LOG.read_text())
    records.append({"session_id": session_id, "turn": turn,
                    "correction_text": correction, "extracted_rule": rule})
    CORRECTIONS_LOG.write_text(json.dumps(records, indent=2))

client = anthropic.Anthropic()

EXTRACT_RULE_PROMPT = """Is this user message correcting agent behavior? If so, state the implied rule.
Message: {message}
Return JSON: {{"is_correction": true/false, "rule": "<rule or empty>"}}"""

OPTIMIZE_PROMPT = """You are improving a system prompt by incorporating user feedback.

Current system prompt:
{current_prompt}

User corrections (rules to incorporate):
{rules}

Rewrite the system prompt to naturally incorporate all these rules.
Make it clear, concise, and actionable. Return only the new system prompt."""

def detect_correction(message: str) -> str | None:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content": EXTRACT_RULE_PROMPT.format(message=message)}],
    )
    try:
        data = json.loads(r.content[0].text)
        return data["rule"] if data["is_correction"] else None
    except Exception:
        return None

def optimize_prompt(base_prompt: str, min_corrections: int = 3) -> str:
    if not CORRECTIONS_LOG.exists():
        return base_prompt
    records = json.loads(CORRECTIONS_LOG.read_text())
    if len(records) < min_corrections:
        return base_prompt  # not enough data yet

    rules = list({r["extracted_rule"] for r in records if r["extracted_rule"]})
    if not rules:
        return base_prompt

    r = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": OPTIMIZE_PROMPT.format(
            current_prompt=base_prompt,
            rules="\n".join(f"- {rule}" for rule in rules),
        )}],
    )
    optimized = r.content[0].text
    OPTIMIZED_PROMPT_FILE.write_text(optimized)
    print(f"[OPTIMIZER] Rewrote system prompt incorporating {len(rules)} rules")
    return optimized

def load_current_system_prompt(base: str) -> str:
    if OPTIMIZED_PROMPT_FILE.exists():
        return OPTIMIZED_PROMPT_FILE.read_text()
    return base

BASE_SYSTEM = "You are a helpful technical assistant."

def respond(user_message: str, session_id: str = "s1", turn: int = 0) -> str:
    rule = detect_correction(user_message)
    if rule:
        log_correction(session_id, turn, user_message, rule)
        print(f"[CORRECTION LOGGED] {rule}")

    system = load_current_system_prompt(BASE_SYSTEM)
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return r.content[0].text


# Accumulate corrections over multiple simulated sessions
corrections = [
    "Always include the time complexity in your algorithm explanations",
    "Please don't use the word 'straightforward' — it's condescending",
    "Show before/after code comparisons when explaining refactoring",
    "Use PEP 8 style in all Python examples",
]
for i, c in enumerate(corrections):
    respond(c, session_id=f"session_{i}")

# Optimize the system prompt
optimized = optimize_prompt(BASE_SYSTEM)
print(f"\nOptimized system prompt:\n{optimized[:500]}")

# Now respond using the optimized prompt
print("\nResponse with optimized prompt:")
print(respond("How do I make this code more efficient: [my_list[i] for i in range(len(my_list))]"))

# Expected Token Savings: Optimization amortizes rule injection into base prompt; cleaner than appending rules
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Storage | Conflict Resolution | Decay | Scope | Best For |
|--------|---------|--------------------|----|------|----------|
| 1. JSON Skill Store | JSON flat file | Overwrite on match | No | Global | Quick implementation |
| 2. Few-Shot Examples | JSON pairs | No (additive) | No | Semantic similarity | Learning response style |
| 3. Preference Profile | Typed struct | Field-level update | No | Global | Structured preferences |
| 4. Weighted + Decay | JSON with weights | Reinforcement merge | Yes | Global | Long-running agents |
| 5. Domain-Scoped | Domain buckets | Domain-level dedup | No | Per-domain | Multi-domain assistants |
| 6. Prompt Optimizer | Correction log + prompt | Holistic rewrite | No | Global | Periodic optimization |

**Recommended**: Option 3 (preference profile) for a clean, typed approach. Option 4 (weighted decay) for agents serving users over months. Option 6 (optimizer) for teams iterating on system prompts based on real user feedback.
