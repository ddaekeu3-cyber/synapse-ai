# failed to build v2026.3.13

## 증상
Regression (worked before, now fails)

에러 메시지:
`).
6.243 ────╯
6.243 
6.869 
6.869 > openclaw@2026.3.13 build:plugin-sdk:dts /app
6.869 > tsc -p tsconfig.plugin-sdk.dts.json
6.869 
30.62 src/browser/pw-ai.ts(22,3): error TS2724: '"./pw-tools-core.

## 원인
원본 이슈에서 확인 필요. GitHub Issue #46073 참조.

## 해결법
-v2026.3.13 v2026.3.13
2. git build --no-cache -t openclaw:local .

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: docker
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/46073
