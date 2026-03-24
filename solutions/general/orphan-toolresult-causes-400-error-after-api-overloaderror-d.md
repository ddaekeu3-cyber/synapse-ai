# Orphan tool_result causes 400 error after API overload/error during tool call

## 증상
When the Anthropic API returns an error (e.g., `overloaded_error`) **during** a tool call execution, Clawdbot inserts a synthetic `tool_result` to "repair" the transcript. However, this breaks the conversation structure because:

에러 메시지:
```
400 {"type":"error","error":{"type":"invalid_request_error","message":"messages.34.content.1: unexpected `tool_use_id` found in `tool_result` blocks: toolu_01CvTeaPH28nkKQyXrfUFiqh. Each `tool_res

## 원인
원본 이슈에서 확인 필요. GitHub Issue #42607 참조.

## 해결법
Currently the only fix is to manually delete the session file and sessions.json entry to force a fresh session.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: general
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/42607
