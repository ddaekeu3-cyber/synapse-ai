# 402 error from third-party proxy not recognized as billing error (failoverReason: null)

## 증상
When using a third-party Anthropic-compatible proxy, a 402 response with message `"402 No available asset for API access, please purchase a subscription"` is not recognized as a billing error.

에러 메시지:
```json
{
  "event": "embedded_run_agent_end",
  "isError": true,
  "error": "402 No available asset for API access, please purchase a subscription",
  "failoverReason": null,
  "model": "claude-opus-

## 원인
원본 이슈에서 확인 필요. GitHub Issue #45774 참조.

## 해결법
ed messages. This proxy's message starts with "402" but doesn't match either pattern.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/45774
