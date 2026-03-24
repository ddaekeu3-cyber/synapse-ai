# ACP/acpx: claude-agent-acp requires API key, doesn't work with Claude Max subscription OAuth tokens

## 증상
The stock acpx plugin in v2026.3.22+ uses `@zed-industries/claude-agent-acp` as the ACP adapter for Claude Code sessions. This adapter requires a standard Anthropic API key (`sk-ant-api03-...`) and can't use Claude Max subscriptions with the adapter.

에러 메시지:
```bash
# Works (claude CLI with OAuth):
claude --print -p "say hello"
# Output: Hello!

# Fails (acpx → claude-agent-acp):
acpx --approve-all exec "say hello"
# Output: [error] RUNTIME: Authenticatio

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53456 참조.

## 해결법
API tokens into the child process environment.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53456
