# claude-cli backend: assistant responses not persisted to session .jsonl, webchat history lost on reload

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```shell
Session .jsonl — assistant turn (representative entry, all turns identical pattern):

JSON · 13 lines
sessions.json — confirms claude-cli is the active provider:

JSON · 5 lines
(outputTokens

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52362 참조.

## 해결법
s required (external context files) to maintain continuity across sessions

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52362
