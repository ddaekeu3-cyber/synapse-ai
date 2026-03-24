# repairToolUseResultPairing misses orphaned tool IDs from MiniMax/OpenAI-compat models — underscore-stripping creates ID mismatch between JSONL and Anthropic API payload

## 증상
Crash (process/app exits or hangs)

에러 메시지:
`stopReason: "error"`

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52604 참조.

## 해결법
in #13799 (`sanitizeToolCallIds`). That fix sanitizes IDs at serialization time, but `repairToolUseResultPairing` runs on the pre-sanitization JSONL and does its pairing check against the original IDs — so it never sees the mismatch.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52604
