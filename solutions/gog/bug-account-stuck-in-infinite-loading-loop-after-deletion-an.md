# Bug: Account stuck in infinite loading loop after deletion and re-authorization (Soft-delete issue)

## 증상
I am unable to log into ClawHub using my primary GitHub account. I previously deleted my ClawHub account and am now trying to re-register using the same GitHub account. However, the authorization process gets stuck in an infinite loop/loading state, or shows as unauthorized.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #1116 참조.

## 해결법
이 이슈의 해결법은 원본 GitHub Issue를 참조하세요.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1116
