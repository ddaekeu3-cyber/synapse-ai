---
title: "Agent Doesn't Implement Content Policy Enforcement Layer Before Tool Execution"
description: "Agents that pass LLM-generated tool call intent directly to execution without a content policy check allow the model to be manipulated into calling tools with arguments that violate usage policy: generating prohibited content, accessing restricted resources, or performing operations outside the agent's defined scope. Implement a content policy enforcement layer that evaluates tool call intent against policy rules before dispatch."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-content-policy-enforcement-layer-before-tool-execution
tags: [content-policy, tool-enforcement, policy-rules, scope-enforcement, usage-policy, pre-execution-check]
symptoms:
  - "Agent calls a file-read tool with paths outside the allowed directory"
  - "LLM manipulated into calling admin tools that are not in the agent's scope"
  - "No validation that a tool call's intent aligns with the agent's declared purpose"
  - "Policy rules exist in documentation but are not enforced at runtime"
  - "Tool calls with prohibited argument patterns are dispatched without challenge"
---

## Why This Happens

Tool schemas define what arguments a tool accepts, not what the agent is permitted to do with them. A file-read tool accepts any path string — the schema does not prevent reading `/etc/passwd`. Content policy enforcement adds a semantic layer above schema validation: not "is this valid JSON?" but "is this within the agent's authorized scope?". Policy rules are declarative specifications that the enforcement layer evaluates against each tool call intent before dispatch, and violations produce clear rejection reasons rather than runtime errors.

## Solution 1: Policy Rule

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyConditionType(str, Enum):
    ARG_MATCHES = "arg_matches"         # argument value matches regex
    ARG_NOT_MATCHES = "arg_not_matches" # argument must NOT match regex
    ARG_IN_ALLOWLIST = "arg_in_allowlist"
    TOOL_NAME_IS = "tool_name_is"
    CUSTOM = "custom"


@dataclass
class PolicyCondition:
    condition_type: PolicyConditionType
    arg_name: Optional[str] = None
    pattern: Optional[str] = None
    allowlist: Optional[List[str]] = None
    custom_fn: Optional[Callable] = None  # fn(tool_name, args) -> bool

    def evaluate(self, tool_name: str, args: Dict[str, Any]) -> bool:
        if self.condition_type == PolicyConditionType.TOOL_NAME_IS:
            return tool_name == self.pattern

        if self.condition_type in (
            PolicyConditionType.ARG_MATCHES,
            PolicyConditionType.ARG_NOT_MATCHES,
        ):
            value = str(args.get(self.arg_name, ""))
            matched = bool(re.search(self.pattern, value, re.IGNORECASE)) if self.pattern else False
            return matched if self.condition_type == PolicyConditionType.ARG_MATCHES else not matched

        if self.condition_type == PolicyConditionType.ARG_IN_ALLOWLIST:
            value = str(args.get(self.arg_name, ""))
            return value in (self.allowlist or [])

        if self.condition_type == PolicyConditionType.CUSTOM and self.custom_fn:
            return self.custom_fn(tool_name, args)

        return False


@dataclass
class PolicyRule:
    rule_id: str
    description: str
    conditions: List[PolicyCondition]
    effect: PolicyEffect
    all_conditions_required: bool = True   # AND vs OR

    def matches(self, tool_name: str, args: Dict[str, Any]) -> bool:
        if not self.conditions:
            return False
        results = [c.evaluate(tool_name, args) for c in self.conditions]
        return all(results) if self.all_conditions_required else any(results)
```

## Solution 2: Policy Ruleset Builder

```python
def build_default_agent_policy() -> List[PolicyRule]:
    """
    Example policy rules for a general-purpose agent.
    Customize for your agent's specific scope and permissions.
    """
    return [
        PolicyRule(
            rule_id="deny_system_paths",
            description="Prevent reading system files",
            conditions=[
                PolicyCondition(
                    condition_type=PolicyConditionType.ARG_MATCHES,
                    arg_name="path",
                    pattern=r"^/(etc|proc|sys|root|var/log)",
                ),
            ],
            effect=PolicyEffect.DENY,
        ),
        PolicyRule(
            rule_id="deny_path_traversal",
            description="Block directory traversal in any path argument",
            conditions=[
                PolicyCondition(
                    condition_type=PolicyConditionType.ARG_MATCHES,
                    arg_name="path",
                    pattern=r"\.\.[/\\]",
                ),
            ],
            effect=PolicyEffect.DENY,
        ),
        PolicyRule(
            rule_id="require_approval_delete",
            description="Require approval for delete operations",
            conditions=[
                PolicyCondition(
                    condition_type=PolicyConditionType.TOOL_NAME_IS,
                    pattern="delete_file",
                ),
            ],
            effect=PolicyEffect.REQUIRE_APPROVAL,
        ),
        PolicyRule(
            rule_id="deny_admin_tools_from_user_sessions",
            description="Deny admin tool access outside admin sessions",
            conditions=[
                PolicyCondition(
                    condition_type=PolicyConditionType.ARG_MATCHES,
                    arg_name="endpoint",
                    pattern=r"/admin/",
                ),
            ],
            effect=PolicyEffect.DENY,
        ),
        PolicyRule(
            rule_id="allow_safe_read",
            description="Allow reads from permitted directories",
            conditions=[
                PolicyCondition(
                    condition_type=PolicyConditionType.ARG_NOT_MATCHES,
                    arg_name="path",
                    pattern=r"^/(etc|proc|sys|root)",
                ),
            ],
            effect=PolicyEffect.ALLOW,
        ),
    ]
```

## Solution 3: Policy Enforcement Engine

```python
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class PolicyDecision:
    allowed: bool
    effect: PolicyEffect
    matched_rule_id: Optional[str]
    matched_rule_description: str
    tool_name: str

    def is_denied(self) -> bool:
        return self.effect == PolicyEffect.DENY

    def requires_approval(self) -> bool:
        return self.effect == PolicyEffect.REQUIRE_APPROVAL


class ContentPolicyEngine:
    """
    Evaluates tool call intent against a set of policy rules.
    Rules are evaluated in order; the first matching DENY or REQUIRE_APPROVAL
    wins. If no rule matches, the default effect is applied.
    """

    def __init__(
        self,
        rules: List[PolicyRule],
        default_effect: PolicyEffect = PolicyEffect.ALLOW,
    ):
        self._rules = rules
        self._default = default_effect

    def evaluate(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> PolicyDecision:
        for rule in self._rules:
            if rule.matches(tool_name, args):
                if rule.effect in (PolicyEffect.DENY, PolicyEffect.REQUIRE_APPROVAL):
                    return PolicyDecision(
                        allowed=rule.effect != PolicyEffect.DENY,
                        effect=rule.effect,
                        matched_rule_id=rule.rule_id,
                        matched_rule_description=rule.description,
                        tool_name=tool_name,
                    )

        return PolicyDecision(
            allowed=self._default != PolicyEffect.DENY,
            effect=self._default,
            matched_rule_id=None,
            matched_rule_description="default policy",
            tool_name=tool_name,
        )
```

## Solution 4: Policy-Gated Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict, Optional


class PolicyViolationError(Exception):
    def __init__(self, decision: PolicyDecision):
        self.decision = decision
        super().__init__(
            f"policy denied tool '{decision.tool_name}': "
            f"rule '{decision.matched_rule_id}' — {decision.matched_rule_description}"
        )


class PolicyGatedToolDispatcher:
    """
    Wraps tool dispatch with content policy evaluation.
    DENY decisions raise PolicyViolationError.
    REQUIRE_APPROVAL decisions raise unless approval is pre-granted.
    """

    def __init__(
        self,
        engine: ContentPolicyEngine,
        approval_store: Optional[Dict[str, bool]] = None,
    ):
        self._engine = engine
        self._approvals = approval_store or {}
        self._violation_count = 0
        self._allow_count = 0

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        args: Dict[str, Any],
        approval_token: Optional[str] = None,
    ) -> Any:
        decision = self._engine.evaluate(tool_name, args)

        if decision.is_denied():
            self._violation_count += 1
            raise PolicyViolationError(decision)

        if decision.requires_approval():
            approved = approval_token and self._approvals.get(approval_token, False)
            if not approved:
                self._violation_count += 1
                raise PolicyViolationError(decision)

        self._allow_count += 1
        return await tool_fn(**args)

    def stats(self) -> dict:
        total = self._allow_count + self._violation_count
        return {
            "total": total,
            "allowed": self._allow_count,
            "violations": self._violation_count,
            "violation_rate": round(self._violation_count / max(total, 1), 4),
        }
```

## Solution 5: Policy Violation Auditor

```python
import time
from typing import List


class PolicyViolationAuditor:
    def __init__(self, max_records: int = 10000):
        self._records: List[dict] = []
        self._max = max_records

    def record(
        self,
        decision: PolicyDecision,
        session_id: str = "",
        args_summary: str = "",
    ) -> None:
        if not decision.is_denied() and not decision.requires_approval():
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "tool_name": decision.tool_name,
            "rule_id": decision.matched_rule_id,
            "effect": decision.effect.value,
            "args_summary": args_summary[:200],
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        by_rule: dict = {}
        by_tool: dict = {}
        for r in recent:
            rule = r["rule_id"] or "unknown"
            by_rule[rule] = by_rule.get(rule, 0) + 1
            by_tool[r["tool_name"]] = by_tool.get(r["tool_name"], 0) + 1
        return {
            "window_seconds": window_seconds,
            "violations": len(recent),
            "by_rule": dict(sorted(by_rule.items(), key=lambda x: -x[1])),
            "by_tool": dict(sorted(by_tool.items(), key=lambda x: -x[1])),
        }
```

## Solution 6: Policy Enforcement Dashboard

```python
import time


class PolicyEnforcementDashboard:
    def __init__(
        self,
        dispatcher: PolicyGatedToolDispatcher,
        auditor: PolicyViolationAuditor,
    ):
        self._dispatcher = dispatcher
        self._auditor = auditor

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "dispatcher_stats": self._dispatcher.stats(),
            "violation_audit": self._auditor.summary(3600.0),
        }
```

## Comparison

| Approach | Declarative Rules | Condition Evaluation | Approval Support | Audit Trail | Dashboard |
|---|---|---|---|---|---|
| PolicyRule + PolicyCondition | Yes | Yes (regex, allowlist) | No | No | No |
| ContentPolicyEngine | Via rules | Via rules | No | No | No |
| PolicyGatedToolDispatcher | Via engine | Via engine | Yes | No | No |
| PolicyViolationAuditor | No | No | No | Yes | No |
| PolicyEnforcementDashboard | No | No | No | No | Yes |

**Best for production**: Write policy rules as data (YAML or JSON loaded at startup) rather than code — this allows non-engineers to audit and update the policy without a deployment. Use DENY as the default effect and maintain an explicit ALLOW list of permitted tool+argument combinations; default-allow is too permissive for agents with side-effectful tools. Monitor `by_rule` in `PolicyViolationAuditor.summary()`: a rule that fires constantly suggests either the rule is misconfigured or the agent is systematically attempting out-of-scope operations. A spike in violations from a single session_id warrants immediate investigation.
