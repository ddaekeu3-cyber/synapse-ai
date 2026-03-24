# Misleading error: 'approval-timeout' when exec is blocked by sandbox mode (not approvals system)

## 증상
When an agent's exec tool call is blocked due to **sandbox mode** (non-main session sandboxed), the error returned is:

에러 메시지:
```
Approval required (id <uuid>). Approve to run; updates will arrive after completion.
```

Followed by a system message:

```
Exec denied (gateway id=<uuid>, approval-timeout): <command>
```

This 

## 원인
원본 이슈에서 확인 필요. GitHub Issue #26666 참조.

## 해결법
— `openclaw sandbox explain` → set `sandbox.mode = "off"` — is discoverable but not surfaced by the error
- Boot check / heartbeat sessions work fine (internal/trusted context bypasses sandbox), which makes the problem look intermittent and harder to diagnose

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: telegram
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/26666
