# Control UI model picker prefixes openai/ when selecting Anthropic Claude → model not allowed

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```shell
Gateway verbose log evidence
Running openclaw gateway run --verbose and reproducing shows:
CopyCopied!
[ws] ⇄ res ✗ sessions.patch ... errorCode=INVALID_REQUEST errorMessage=model not allowed

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52061 참조.

## 해결법
), and gateway rejects it.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52061
