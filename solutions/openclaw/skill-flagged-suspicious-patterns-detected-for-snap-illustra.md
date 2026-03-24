# Skill flagged — suspicious patterns detected for snap-illustrator

## 증상
I published a skill called `snap-illustrator` to ClawHub, but it was flagged as "suspicious patterns detected" by ClawHub Security.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #1119 참조.

## 해결법
Implemented in v1.0.4**: We have completely removed all disk fallback logic. The skill now strictly reads `HF_TOKEN` directly from the environment variables and never writes or reads from local config files. We also updated the requires metadata in `SKILL.md` to properly declare `node` and the `HF_TOKEN` environment variable.
4. No hardcoded secrets, no obfuscated scripts, and no unauthorized file

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/clawhub/issues/1119
