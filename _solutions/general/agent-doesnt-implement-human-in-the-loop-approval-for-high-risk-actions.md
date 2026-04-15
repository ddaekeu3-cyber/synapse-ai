---
layout: solution
title: "Agent Doesn't Implement Human-in-the-Loop Approval for High-Risk Actions"
category: general
description: "Autonomous agents execute irreversible actions — deleting files, sending emails, making purchases — without human review. Human-in-the-loop (HITL) approval pauses execution before high-risk tool calls, requiring explicit confirmation before proceeding."
tags: [general, human-in-the-loop, approval, safety, autonomy, risk-management]
---

# Agent Doesn't Implement Human-in-the-Loop Approval for High-Risk Actions

## Problem

Agents given broad tool access will eventually perform irreversible actions at the wrong moment: deleting the wrong file, sending a draft email to a client, placing an order with incorrect quantities. Without approval gates, there is no opportunity to catch these mistakes before they cause damage. The cost of a single wrong irreversible action often exceeds the cost of pausing for review hundreds of times.

## Why This Happens

Fully automated agents are the goal, so approval logic is seen as friction. Teams add tools without classifying them as reversible or irreversible. There is no framework for "pause here and wait" in most agentic architectures — the default is to execute and hope.

## Solutions

### Option 1: Risk-Classified Tool Registry — Tag tools as safe/reversible/irreversible

```python
import anthropic
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Any

class RiskLevel(Enum):
    SAFE = "safe"              # Read-only, no side effects
    REVERSIBLE = "reversible"  # Has side effects but can be undone
    IRREVERSIBLE = "irreversible"  # Cannot be undone — requires approval


@dataclass
class ManagedTool:
    name: str
    description: str
    fn: Callable
    risk: RiskLevel
    approval_message: str = ""  # What to show the human when approval needed


class HITLToolRegistry:
    def __init__(self, auto_approve_reversible: bool = False):
        self.tools: dict[str, ManagedTool] = {}
        self.auto_approve_reversible = auto_approve_reversible

    def register(self, tool: ManagedTool) -> None:
        self.tools[tool.name] = tool

    def _get_human_approval(self, tool: ManagedTool, kwargs: dict) -> bool:
        print(f"\n{'='*50}")
        print(f"⚠ APPROVAL REQUIRED: {tool.name}")
        print(f"Risk level: {tool.risk.value.upper()}")
        print(f"Action: {tool.approval_message or tool.description}")
        print(f"Parameters: {kwargs}")
        print(f"{'='*50}")
        response = input("Approve? [y/N]: ").strip().lower()
        return response == "y"

    def execute(self, tool_name: str, **kwargs) -> tuple[Any, bool]:
        """Returns (result, was_approved). If not approved, returns (None, False)."""
        tool = self.tools.get(tool_name)
        if not tool:
            raise ValueError(f"Unknown tool: {tool_name}")

        if tool.risk == RiskLevel.SAFE:
            return tool.fn(**kwargs), True

        if tool.risk == RiskLevel.REVERSIBLE and self.auto_approve_reversible:
            return tool.fn(**kwargs), True

        # Requires human approval
        approved = self._get_human_approval(tool, kwargs)
        if not approved:
            print(f"[HITL] Action '{tool_name}' rejected by user.")
            return None, False

        result = tool.fn(**kwargs)
        print(f"[HITL] Action '{tool_name}' executed with approval.")
        return result, True


class HITLAgent:
    def __init__(self, registry: HITLToolRegistry):
        self.client = anthropic.Anthropic()
        self.registry = registry

    def run(self, user_request: str) -> str:
        # Simple single-turn demo: LLM decides what to do, registry gates execution
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=(
                "You are an assistant. Identify what action to take. "
                "Reply with: ACTION: <tool_name> PARAMS: <json_params>\n"
                "Or reply with just a text answer if no tool is needed."
            ),
            messages=[{"role": "user", "content": user_request}]
        )
        text = response.content[0].text

        if "ACTION:" in text and "PARAMS:" in text:
            import json, re
            tool_match = re.search(r"ACTION:\s*(\w+)", text)
            params_match = re.search(r"PARAMS:\s*(\{.*\})", text, re.DOTALL)
            if tool_match and params_match:
                tool_name = tool_match.group(1)
                params = json.loads(params_match.group(1))
                result, approved = self.registry.execute(tool_name, **params)
                if not approved:
                    return "Action was declined by the user. No changes were made."
                return f"Action completed: {result}"

        return text


# Setup tools with risk levels
registry = HITLToolRegistry(auto_approve_reversible=True)

registry.register(ManagedTool(
    name="read_file",
    description="Read file contents",
    fn=lambda path: f"Contents of {path}: [file data]",
    risk=RiskLevel.SAFE,
))
registry.register(ManagedTool(
    name="create_draft",
    description="Create email draft",
    fn=lambda to, subject, body: f"Draft created to {to}",
    risk=RiskLevel.REVERSIBLE,
    approval_message="Create email draft (can be deleted before sending)",
))
registry.register(ManagedTool(
    name="send_email",
    description="Send email immediately",
    fn=lambda to, subject, body: f"Email sent to {to}",
    risk=RiskLevel.IRREVERSIBLE,
    approval_message="SEND email — this cannot be unsent",
))
registry.register(ManagedTool(
    name="delete_file",
    description="Permanently delete a file",
    fn=lambda path: f"Deleted {path}",
    risk=RiskLevel.IRREVERSIBLE,
    approval_message="PERMANENTLY DELETE file — this cannot be recovered",
))

agent = HITLAgent(registry)
# In real usage, LLM would parse request and call registry.execute directly
result, approved = registry.execute("delete_file", path="/data/important.csv")
print(f"Result: {result}, Approved: {approved}")

# Expected Token Savings: No token savings — prevents irreversible mistakes worth far more than token costs
# Environment: File management agents, email agents, e-commerce agents, any agent with write access
```

### Option 2: Async Approval Queue — Non-blocking: queue action, notify human, resume on response

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import uuid

class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"

@dataclass
class PendingAction:
    action_id: str
    tool_name: str
    params: dict
    description: str
    requested_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    status: ApprovalStatus = ApprovalStatus.PENDING
    timeout_seconds: float = 300.0  # 5 minutes


class AsyncApprovalQueue:
    def __init__(self):
        self._pending: dict[str, PendingAction] = {}
        self._events: dict[str, asyncio.Event] = {}

    async def request_approval(self, tool_name: str, params: dict, description: str) -> PendingAction:
        action_id = str(uuid.uuid4())[:8]
        action = PendingAction(
            action_id=action_id,
            tool_name=tool_name,
            params=params,
            description=description,
        )
        self._pending[action_id] = action
        self._events[action_id] = asyncio.Event()

        # Notify human (in production: send Slack/email/webhook)
        print(f"\n[APPROVAL REQUEST {action_id}]")
        print(f"  Tool: {tool_name}")
        print(f"  Description: {description}")
        print(f"  Params: {params}")
        print(f"  Timeout: {action.timeout_seconds}s")
        print(f"  To approve: queue.respond('{action_id}', True)")
        print(f"  To reject:  queue.respond('{action_id}', False)")

        return action

    async def wait_for_approval(self, action_id: str) -> ApprovalStatus:
        action = self._pending.get(action_id)
        if not action:
            return ApprovalStatus.REJECTED

        event = self._events.get(action_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=action.timeout_seconds)
        except asyncio.TimeoutError:
            action.status = ApprovalStatus.EXPIRED
            print(f"[HITL] Action {action_id} expired — defaulting to REJECT")

        return action.status

    def respond(self, action_id: str, approved: bool) -> bool:
        action = self._pending.get(action_id)
        if not action or action.status != ApprovalStatus.PENDING:
            return False
        action.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        event = self._events.get(action_id)
        if event:
            event.set()
        return True

    def pending_count(self) -> int:
        return sum(1 for a in self._pending.values() if a.status == ApprovalStatus.PENDING)


# Global queue (would be a service in production)
approval_queue = AsyncApprovalQueue()


async def safe_execute(tool_name: str, fn, description: str, **kwargs) -> tuple[any, ApprovalStatus]:
    action = await approval_queue.request_approval(tool_name, kwargs, description)
    status = await approval_queue.wait_for_approval(action.action_id)

    if status == ApprovalStatus.APPROVED:
        result = fn(**kwargs)
        return result, status
    return None, status


async def demo():
    client = anthropic.AsyncAnthropic()

    # Simulate: agent wants to delete a file
    async def simulate_approval():
        await asyncio.sleep(1)  # Simulate human reviewing
        # Find the first pending action and approve it
        for action_id, action in approval_queue._pending.items():
            if action.status == ApprovalStatus.PENDING:
                approval_queue.respond(action_id, True)
                print(f"\n[HUMAN] Approved action {action_id}")
                break

    asyncio.create_task(simulate_approval())

    result, status = await safe_execute(
        tool_name="delete_file",
        fn=lambda path: f"Deleted {path}",
        description="Delete temporary log file older than 30 days",
        path="/logs/app_2026_01_01.log"
    )
    print(f"\nResult: {result} (Status: {status.value})")


asyncio.run(demo())

# Expected Token Savings: No token savings; async model means agent continues other work while waiting
# Environment: Slack/Teams-integrated agents, workflow automation, agents with human reviewers
```

### Option 3: Risk-Scoring LLM — Use model to classify action risk before execution

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class RiskAssessment:
    score: int          # 1-10
    reversible: bool
    reason: str
    requires_approval: bool  # score >= threshold

    @property
    def risk_label(self) -> str:
        if self.score <= 3:
            return "LOW"
        if self.score <= 6:
            return "MEDIUM"
        return "HIGH"


class LLMRiskClassifier:
    APPROVAL_THRESHOLD = 6

    def __init__(self):
        self.client = anthropic.Anthropic()

    def assess(self, tool_name: str, params: dict, context: str = "") -> RiskAssessment:
        prompt = f"""You are a risk assessment expert for AI agent actions.

TOOL: {tool_name}
PARAMS: {json.dumps(params)}
CONTEXT: {context or 'No additional context'}

Assess the risk of this action:
- Score 1-10 (1=harmless read, 10=catastrophic irreversible change)
- Is it reversible?
- Why?

Return JSON only:
{{"score": 1-10, "reversible": true/false, "reason": "one sentence"}}"""

        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}]
        )
        try:
            data = json.loads(response.content[0].text)
            score = int(data["score"])
            return RiskAssessment(
                score=score,
                reversible=bool(data.get("reversible", False)),
                reason=data.get("reason", ""),
                requires_approval=score >= self.APPROVAL_THRESHOLD,
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return RiskAssessment(score=10, reversible=False, reason="Parse error — defaulting to HIGH risk", requires_approval=True)


class RiskScoringAgent:
    def __init__(self, approval_threshold: int = 6):
        self.client = anthropic.Anthropic()
        self.classifier = LLMRiskClassifier()
        self.classifier.APPROVAL_THRESHOLD = approval_threshold

    def execute_with_assessment(
        self, tool_name: str, fn, params: dict, context: str = ""
    ) -> tuple[any, RiskAssessment]:
        assessment = self.classifier.assess(tool_name, params, context)

        print(f"[RISK] {tool_name}: {assessment.risk_label} ({assessment.score}/10)")
        print(f"       Reversible: {assessment.reversible} — {assessment.reason}")

        if assessment.requires_approval:
            print(f"\n⚠  HIGH RISK ACTION DETECTED")
            answer = input(f"Execute '{tool_name}' with params {params}? [y/N]: ").strip().lower()
            if answer != "y":
                print("[HITL] Action rejected.")
                return None, assessment

        result = fn(**params)
        return result, assessment

    def run_plan(self, actions: list[tuple[str, callable, dict]]) -> list[dict]:
        """Execute a list of (tool_name, fn, params) with per-action risk gating."""
        results = []
        for tool_name, fn, params in actions:
            result, assessment = self.execute_with_assessment(tool_name, fn, params)
            results.append({
                "tool": tool_name,
                "risk": assessment.risk_label,
                "approved": result is not None,
                "result": str(result)[:100] if result else "REJECTED",
            })
        return results


# Usage
agent = RiskScoringAgent(approval_threshold=6)

actions = [
    ("read_config", lambda path: "config data", {"path": "/etc/app.conf"}),
    ("write_report", lambda path, content: "written", {"path": "/reports/q1.txt", "content": "data"}),
    ("drop_table", lambda table: "dropped", {"table": "users_prod"}),
]

results = agent.run_plan(actions)
for r in results:
    print(f"\n{r['tool']}: [{r['risk']}] → {r['result']}")

# Expected Token Savings: Haiku risk assessment costs ~20 tokens; saves cost of reversing catastrophic mistakes
# Environment: DevOps agents, database agents, file management, any agent with admin-level tool access
```

### Option 4: Approval Policy Engine — Configurable rules for what requires approval

```python
import anthropic
import json
import re
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class ApprovalRule:
    name: str
    condition: Callable[[str, dict], bool]  # (tool_name, params) -> needs_approval
    message: str
    severity: str = "warning"  # "warning" or "critical"


class ApprovalPolicyEngine:
    def __init__(self):
        self.rules: list[ApprovalRule] = []
        self._approver: Callable[[str, str, dict], bool] | None = None

    def add_rule(self, rule: ApprovalRule) -> None:
        self.rules.append(rule)

    def set_approver(self, approver: Callable[[str, str, dict], bool]) -> None:
        self._approver = approver

    def evaluate(self, tool_name: str, params: dict) -> list[ApprovalRule]:
        """Return list of triggered rules."""
        return [r for r in self.rules if r.condition(tool_name, params)]

    def check_and_approve(self, tool_name: str, params: dict) -> bool:
        triggered = self.evaluate(tool_name, params)
        if not triggered:
            return True  # Auto-approved

        critical = [r for r in triggered if r.severity == "critical"]
        warnings = [r for r in triggered if r.severity == "warning"]

        print(f"\n{'='*50}")
        print(f"🔴 APPROVAL REQUIRED for '{tool_name}'")
        for rule in critical:
            print(f"  [CRITICAL] {rule.name}: {rule.message}")
        for rule in warnings:
            print(f"  [WARNING]  {rule.name}: {rule.message}")
        print(f"  Params: {json.dumps(params)}")
        print(f"{'='*50}")

        if self._approver:
            return self._approver(tool_name, "\n".join(r.message for r in triggered), params)

        # Default: CLI prompt
        answer = input("Approve? [y/N]: ").strip().lower()
        return answer == "y"


def build_default_policy() -> ApprovalPolicyEngine:
    engine = ApprovalPolicyEngine()

    # Rule 1: Any "delete" or "drop" in tool name
    engine.add_rule(ApprovalRule(
        name="destructive_tool_name",
        condition=lambda name, _: any(kw in name.lower() for kw in ["delete", "drop", "remove", "destroy", "purge"]),
        message="Tool name suggests destructive operation",
        severity="critical",
    ))

    # Rule 2: Production data targets
    engine.add_rule(ApprovalRule(
        name="production_target",
        condition=lambda _, params: any(
            "prod" in str(v).lower() or "_prod" in str(v).lower()
            for v in params.values()
        ),
        message="Action targets production environment",
        severity="critical",
    ))

    # Rule 3: Large batch operations (>100 items)
    engine.add_rule(ApprovalRule(
        name="large_batch",
        condition=lambda _, params: any(
            isinstance(v, (list, str)) and len(v) > 100
            for v in params.values()
        ),
        message="Large batch operation (>100 items)",
        severity="warning",
    ))

    # Rule 4: External communications (email, Slack, webhooks)
    engine.add_rule(ApprovalRule(
        name="external_communication",
        condition=lambda name, _: any(kw in name.lower() for kw in ["send", "post", "publish", "notify", "email"]),
        message="Action sends external communication",
        severity="warning",
    ))

    # Rule 5: Financial operations
    engine.add_rule(ApprovalRule(
        name="financial_operation",
        condition=lambda _, params: any(
            kw in str(params).lower() for kw in ["payment", "charge", "invoice", "purchase", "billing"]
        ),
        message="Action involves financial transaction",
        severity="critical",
    ))

    return engine


class PolicyGatedAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.policy = build_default_policy()

    def execute(self, tool_name: str, fn, **params) -> tuple[any, bool]:
        approved = self.policy.check_and_approve(tool_name, params)
        if not approved:
            return None, False
        return fn(**params), True


# Usage
agent = PolicyGatedAgent()

# This will trigger "production_target" and "large_batch" rules
result, ok = agent.execute(
    "update_users",
    fn=lambda table, ids: f"Updated {len(ids)} rows in {table}",
    table="users_prod",
    ids=list(range(500))
)
print(f"Result: {result}, Approved: {ok}")

# Expected Token Savings: Rule-based policy adds zero LLM tokens; zero latency overhead
# Environment: Enterprise agents, compliance-sensitive deployments, SOC2/GDPR environments
```

### Option 5: Dry-Run Mode — Preview all actions without executing, then confirm

```python
import anthropic
import json
from dataclasses import dataclass, field

@dataclass
class PlannedAction:
    step: int
    tool_name: str
    params: dict
    description: str
    risk_level: str  # "safe", "medium", "high"
    executed: bool = False
    result: str = ""


class DryRunAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self._dry_run_mode = True
        self._plan: list[PlannedAction] = []

    def plan(self, user_request: str) -> list[PlannedAction]:
        """Generate a plan without executing anything."""
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system="""You are a planning agent. Given a user request, output a JSON array of planned actions.
Each action has: tool_name, params (dict), description (human-readable), risk_level ("safe"/"medium"/"high").
Be specific and complete. Return JSON only.""",
            messages=[{"role": "user", "content": f"Plan how to: {user_request}"}]
        )

        try:
            steps = json.loads(response.content[0].text)
            self._plan = [
                PlannedAction(
                    step=i + 1,
                    tool_name=s["tool_name"],
                    params=s.get("params", {}),
                    description=s["description"],
                    risk_level=s.get("risk_level", "medium"),
                )
                for i, s in enumerate(steps)
            ]
        except (json.JSONDecodeError, KeyError):
            self._plan = []

        return self._plan

    def preview(self) -> None:
        """Display the plan for human review."""
        if not self._plan:
            print("No plan generated.")
            return

        print(f"\n{'='*60}")
        print("PLANNED ACTIONS (DRY RUN — nothing has been executed)")
        print('='*60)
        for action in self._plan:
            risk_icon = {"safe": "✓", "medium": "~", "high": "⚠"}.get(action.risk_level, "?")
            print(f"\nStep {action.step} [{risk_icon} {action.risk_level.upper()}]")
            print(f"  Tool: {action.tool_name}")
            print(f"  Description: {action.description}")
            print(f"  Params: {json.dumps(action.params)}")

        high_risk = [a for a in self._plan if a.risk_level == "high"]
        if high_risk:
            print(f"\n⚠  {len(high_risk)} HIGH RISK action(s) in plan")
        print(f"{'='*60}")

    def execute_plan(self, tool_fns: dict[str, callable]) -> list[PlannedAction]:
        """Execute all approved steps."""
        for action in self._plan:
            fn = tool_fns.get(action.tool_name)
            if fn:
                try:
                    result = fn(**action.params)
                    action.result = str(result)
                    action.executed = True
                except Exception as e:
                    action.result = f"ERROR: {e}"
            else:
                action.result = f"No implementation for '{action.tool_name}'"
        return self._plan

    def request_confirmation(self) -> bool:
        self.preview()
        answer = input("\nExecute this plan? [y/N]: ").strip().lower()
        return answer == "y"


# Usage
agent = DryRunAgent()
plan = agent.plan("Send a weekly report email to all managers and archive last week's data files")

if agent.request_confirmation():
    tool_fns = {
        "send_email": lambda to, subject, body="": f"Email sent to {to}",
        "archive_files": lambda path, days=7: f"Archived files older than {days} days from {path}",
        "generate_report": lambda format="pdf": f"Report generated as {format}",
    }
    results = agent.execute_plan(tool_fns)
    for r in results:
        status = "✓" if r.executed else "✗"
        print(f"[{status}] Step {r.step}: {r.result}")
else:
    print("Plan rejected. No actions taken.")

# Expected Token Savings: One planning call replaces many speculative tool calls; prevents retry costs
# Environment: Multi-step automation agents, DevOps, data pipeline agents requiring audit trail
```

### Option 6: Approval Webhook — Send approval request to external system, block until response

```python
import anthropic
import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class WebhookApprovalRequest:
    request_id: str
    tool_name: str
    params: dict
    requester: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    status: str = "pending"   # pending / approved / rejected
    reviewer: str = ""
    reviewed_at: str = ""


class WebhookApprovalGateway:
    """Simulates a webhook-based approval system (Slack, PagerDuty, custom portal)."""

    def __init__(self, webhook_url: str = "https://approvals.internal/webhook"):
        self.webhook_url = webhook_url
        self._requests: dict[str, WebhookApprovalRequest] = {}
        self._callbacks: dict[str, asyncio.Event] = {}

    async def send_for_approval(
        self,
        tool_name: str,
        params: dict,
        requester: str,
        timeout: float = 120.0,
    ) -> tuple[bool, str]:
        """Returns (approved, request_id). Blocks until webhook callback arrives."""
        request_id = str(uuid.uuid4())[:8]
        request = WebhookApprovalRequest(
            request_id=request_id,
            tool_name=tool_name,
            params=params,
            requester=requester,
        )
        self._requests[request_id] = request
        self._callbacks[request_id] = asyncio.Event()

        # Simulate sending webhook to approval system
        print(f"\n[WEBHOOK] Approval request sent: {request_id}")
        print(f"  Tool: {tool_name}, Params: {params}")
        print(f"  Webhook URL: {self.webhook_url}")
        print(f"  Timeout: {timeout}s")

        # Simulate receiving webhook callback
        asyncio.create_task(self._simulate_callback(request_id, delay=2.0, approved=True))

        try:
            await asyncio.wait_for(self._callbacks[request_id].wait(), timeout=timeout)
            req = self._requests[request_id]
            return req.status == "approved", request_id
        except asyncio.TimeoutError:
            print(f"[WEBHOOK] Request {request_id} timed out — defaulting to REJECTED")
            return False, request_id

    async def _simulate_callback(self, request_id: str, delay: float, approved: bool) -> None:
        """Simulates the webhook callback arriving from the approval portal."""
        await asyncio.sleep(delay)
        self.handle_callback(request_id, approved=approved, reviewer="manager@company.com")

    def handle_callback(self, request_id: str, approved: bool, reviewer: str = "") -> bool:
        """Called when the approval portal sends back a decision via webhook."""
        req = self._requests.get(request_id)
        if not req or req.status != "pending":
            return False
        req.status = "approved" if approved else "rejected"
        req.reviewer = reviewer
        req.reviewed_at = datetime.utcnow().isoformat()
        event = self._callbacks.get(request_id)
        if event:
            event.set()
        print(f"[WEBHOOK] {request_id}: {req.status.upper()} by {reviewer}")
        return True


class WebhookGatedAgent:
    def __init__(self, gateway: WebhookApprovalGateway):
        self.client = anthropic.AsyncAnthropic()
        self.gateway = gateway

    async def execute_with_approval(
        self, tool_name: str, fn, requester: str = "agent", **params
    ) -> tuple[any, bool]:
        approved, request_id = await self.gateway.send_for_approval(
            tool_name=tool_name, params=params, requester=requester
        )
        if approved:
            result = fn(**params)
            print(f"[AGENT] '{tool_name}' executed (request {request_id})")
            return result, True
        else:
            print(f"[AGENT] '{tool_name}' rejected (request {request_id})")
            return None, False


async def main():
    gateway = WebhookApprovalGateway()
    agent = WebhookGatedAgent(gateway)

    result, approved = await agent.execute_with_approval(
        "deploy_to_production",
        fn=lambda version, env: f"Deployed v{version} to {env}",
        requester="ci-bot",
        version="2.1.0",
        env="production",
    )
    print(f"Deploy result: {result}, Approved: {approved}")


asyncio.run(main())

# Expected Token Savings: No token savings; webhook enables async human review via Slack/email/portal
# Environment: CI/CD pipelines, compliance workflows, SOC2-audited deployments, PagerDuty integrations
```

## Comparison

| Option | Blocking | Integration | Audit Trail | Best For |
|--------|----------|-------------|-------------|----------|
| Risk-Classified Registry | Sync (CLI) | None | None | Simple scripts, local tools |
| Async Approval Queue | Async | Any notification | In-memory | Slack/Teams integrated workflows |
| LLM Risk Classifier | Sync (CLI) | None | None | Agents without pre-tagged tools |
| Policy Engine | Sync (CLI) | None | None | Compliance, rule-based environments |
| Dry-Run Mode | Sync (plan then execute) | None | Plan preview | Multi-step automation, batch jobs |
| Webhook Gateway | Async | External portal | Full | Enterprise, SOC2, audit-required systems |
