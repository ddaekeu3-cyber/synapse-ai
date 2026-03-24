---
layout: solution
title: "Tool failure notifications are misleading when agent retries successfully"
category: openclaw
---

# Tool failure notifications are misleading when agent retries successfully

## 증상
When a tool call fails (e.g. `write` to a protected path like `/usr/local/bin/`) and the agent immediately retries successfully on a different path, the UI still shows a failure notification (⚠️ ✍️ Write: failed) without any indication that the operation ultimately succeeded.

에러 메시지:
`) and the agent immediately retries successfully on a different path, the UI still shows a failure notification (⚠️ ✍️ Write: failed) without any indication that the operation ultimately succeeded.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #42912 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/42912
