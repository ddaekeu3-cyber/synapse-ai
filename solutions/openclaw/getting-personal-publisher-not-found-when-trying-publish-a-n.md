# Getting "Personal publisher not found" when trying publish a new version of the skill

## 증상
From Web platform getting this error  "Personal publisher not found" and nothing shows up in owner dropdown

에러 메시지:
```
◇  Select skills to upload
│  cellcog  UPDATE 1.0.23 → 1.0.24
✖ Uncaught Error: This query or mutation function ran multiple paginated queries. Convex only supports a single paginated query in eac

## 원인
원본 이슈에서 확인 필요. GitHub Issue #1188 참조.

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
https://github.com/openclaw/clawhub/issues/1188
