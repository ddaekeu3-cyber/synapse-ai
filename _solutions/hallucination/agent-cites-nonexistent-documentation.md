---
layout: solution
title: "Agent Cites Nonexistent Documentation or GitHub Issues as Sources"
category: hallucination
description: "Agent references a specific docs page, GitHub issue, RFC, or Stack Overflow post that does not exist. The citation looks real but leads to a 404 or unrelated content."
tags: [hallucination, citation, grounding, source-fabrication]
---

# Agent Cites Nonexistent Documentation or GitHub Issues as Sources

## Symptom
- Agent says "According to the docs at [URL]..." — URL returns 404
- Agent references "GitHub issue #1234" — issue doesn't exist or is unrelated
- Agent cites "RFC 7845 section 3.2" — section doesn't exist
- Citation looks plausible and specific, making it easy to miss
- Agent is confident and consistent in the fabricated citation across turns

## Root Cause
The model generates plausible-looking citations from training data patterns. It has seen many citation formats and can produce realistic-looking ones. When asked to support a claim, the model may generate a citation rather than admit it doesn't have a source. The citation is statistically plausible, not factually verified.

## Fix

### Option 1: Require verifiable citations only

```
System prompt:
"You may only cite sources you can verify via tool call (web fetch, file read,
or search). If you cannot verify a source exists, do not cite it.
Instead, say: 'I believe [claim] based on my training data, but I cannot
verify a specific source — please confirm independently.'
Never fabricate or guess URLs, issue numbers, or document sections."
```

### Option 2: Verify citations via tool call before presenting

```python
async def verified_response(agent_output, web_tool):
    """Extract URLs from response and verify they exist before returning"""
    import re
    urls = re.findall(r'https?://[^\s\)\"\']+', agent_output)

    failed_urls = []
    for url in urls:
        try:
            result = await web_tool.fetch(url, timeout=5)
            if result.status_code >= 400:
                failed_urls.append(url)
        except Exception:
            failed_urls.append(url)

    if failed_urls:
        return f"{agent_output}\n\n⚠️ WARNING: These URLs could not be verified: {failed_urls}"
    return agent_output
```

### Option 3: Citation format that forces acknowledgment of uncertainty

```
System prompt:
"Format citations as follows:
- Verified (you retrieved it this session): [Source: URL or file:line]
- Recalled from training (unverified): [Unverified claim — please check: description]
- Unknown: [I cannot cite a source for this claim]

Never present unverified sources as if you retrieved them this session."
```

### Option 4: Post-response citation audit

```python
CITATION_PATTERNS = [
    r'https?://[^\s]+',                    # URLs
    r'(?:issue|PR|pull request) #?\d+',    # GitHub refs
    r'RFC \d+',                             # RFC citations
    r'(?:section|§)\s*\d+(?:\.\d+)+',      # Document sections
    r'page \d+',                            # Page numbers
]

def flag_citations_for_verification(response):
    import re
    citations = []
    for pattern in CITATION_PATTERNS:
        citations.extend(re.findall(pattern, response, re.IGNORECASE))

    if citations:
        return response + f"\n\n[Auto-flagged citations to verify: {citations}]"
    return response
```

## Recovery
When you find a fabricated citation:
1. Tell the agent: "The [URL/issue] you cited does not exist. Do not invent sources."
2. Ask it to rephrase the claim without the citation
3. Add to your system prompt: "Do not cite [specific fabricated URL pattern] — this is not a real resource"

## Expected Token Savings
Following fabricated documentation leads and realizing it's wrong: ~10,000 tokens
This fix: ~300 tokens for upfront verification

## Environment
- Any knowledge-retrieval or research agent
- Higher risk with: technical documentation, version-specific APIs, recent events
- Source: direct experience, OWASP LLM Top 10 pattern
