# openclaw gateway probe fails on local loopback while gateway health / status / cron add succeed Summary

## 증상
Regression (worked before, now fails)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #53443 참조.

## 해결법
es**
Minimum fix
Remove or relax the hardcoded 800ms cap for localLoopback in:
function resolveProbeBudgetMs(overallMs, kind)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53443
