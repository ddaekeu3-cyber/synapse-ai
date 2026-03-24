# Feature: Graceful error handling for LLM failures — never expose raw errors to users

## 증상
When an LLM request fails (e.g., Anthropic API rejects due to corrupted thinking blocks in session history), the raw error message is sent directly to the user via the messaging channel (WhatsApp, Telegram, etc.) as an auto-reply.

에러 메시지:
`

This was sent verbatim to the user on WhatsApp, who had no idea what it meant.

## Expected Behavior

1. **Never send raw LLM/API errors as replies to users.** The error should be caught internally

## 원인
원본 이슈에서 확인 필요. GitHub Issue #39612 참조.

## 해결법
Added `../shared/error-escalation.md` protocol to all persona AGENTS.md files instructing agents to catch errors and escalate. But this only works if the agent can actually process the instruction — in the case above, the LLM call never succeeds, so the agent-level instructions are never read.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/39612
