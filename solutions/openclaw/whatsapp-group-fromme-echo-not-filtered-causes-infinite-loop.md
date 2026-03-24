# WhatsApp group fromMe echo not filtered, causes infinite loop with implicit reply-to-bot mention detection

## 증상
Behavior bug (incorrect output/state without crash)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #53386 참조.

## 해결법
es:
1. Filter fromMe messages at WhatsApp plugin level before processing group messages
2. OR add config option channels.whatsapp.groups.*.ignoreFromMe: true
3. OR exclude fromMe messages from implicit reply-to-bot mention detection

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53386
