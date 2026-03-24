# Cron Tool Parameter Validation Error

## 증상
The `cron add` tool fails with parameter validation errors when trying to create scheduled jobs.

에러 메시지:
```python
cron(action="add", name="Test Task", schedule={"kind": "at", "at": "2026-03-15T08:00:00Z"}, sessionTarget="main", payload={"kind": "systemEvent", "text": "test"})
```

## Expected Result

A 

## 원인
원본 이슈에서 확인 필요. GitHub Issue #45875 참조.

## 해결법
1. Check cron tool parameter validation logic in the gateway
2. Ensure nested objects are properly parsed
3. Add better error messages for debugging

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/45875
