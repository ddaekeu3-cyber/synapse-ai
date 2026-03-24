# Feature: Graceful sub-agent timeout (pre-timeout warning)

## 증상
When a sub-agent hits `runTimeoutSeconds`, it's killed immediately with no chance to save progress. **All unsaved work is lost** — code, research, analysis, generated content, anything.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #6625 참조.

## 해결법
Inject a system message to the sub-agent N seconds before the timeout expires:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: general
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/6625
