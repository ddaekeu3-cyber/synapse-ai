# Feature: Sub-agent should auto-save progress on timeout

## 증상
When a sub-agent times out (e.g. 5min/10min limit), all its work is lost. The parent agent only receives the last stdout output, not the actual file changes or progress made.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #48964 참조.

## 해결법
On timeout, automatically:
1. Run `git diff` (if in a git repo) to capture file changes
2. Include a structured progress summary in the timeout result
3. Optionally allow the parent to resume from where the sub-agent left off

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/48964
