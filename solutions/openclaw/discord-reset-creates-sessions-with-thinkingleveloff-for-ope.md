# Discord /reset creates sessions with thinkingLevel=off for OpenAI Codex models, breaking tool calling (regression in 2026.3.23)

## 증상
Regression (worked before, now fails)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #53480 참조.

## 해결법
openclaw config set agents.defaults.thinkingDefault low
openclaw gateway restart
Then /reset again in Discord. The explicit thinkingDefault value is picked up by resolveThinkingDefault() before the buggy catalog lookup:
const configured = params.cfg.agents?.defaults?.thinkingDefault;
if (configured) return configured;  // ← returns "low" here, skips the catalog path

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53480
