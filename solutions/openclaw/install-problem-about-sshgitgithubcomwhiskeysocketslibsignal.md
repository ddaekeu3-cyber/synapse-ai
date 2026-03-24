# Install Problem about ssh://git@github.com/whiskeysockets/libsignal-node.git

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```bash
{\__/}
(● .●)
/ >>> npm i -g openclaw
npm error code 128
npm error An unknown git error occurred
npm error command git --no-replace-objects ls-remote ssh://git@github.com/whiskeysockets/libsig

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52110 참조.

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
https://github.com/openclaw/openclaw/issues/52110
