# ui:build fails on fresh clone due to --prod removing vite

## 증상
Regression (worked before, now fails)

에러 메시지:
`
4. Observe failure

### Expected behavior

The UI should build successfully using Vite.

### Actual behavior

Error: Cannot find module '/workspace/openclaw/ui/node_modules/vite/bin/vite.js'

### Op

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52494 참조.

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
https://github.com/openclaw/openclaw/issues/52494
