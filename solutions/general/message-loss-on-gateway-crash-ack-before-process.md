# Message loss on gateway crash (ack-before-process)

## 증상
Gateway acks messages to WhatsApp before processing completes. If the gateway crashes mid-processing, the message is lost — WhatsApp thinks it was delivered.



## 원인
원본 이슈에서 확인 필요. GitHub Issue #50563 참조.

## 해결법
Implement one of:
1. **Write-ahead log (WAL)**: Persist inbound messages before acking
2. **Persist-before-ack**: Only ack to WhatsApp after message is durably stored
3. **Delayed ack**: Ack only after initial processing completes

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: general
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/50563
