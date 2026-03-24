# WhatsApp groupPolicy "allowlist" bypassed — agent replies in non-allowlisted group

## 증상
Behavior bug (incorrect output/state without crash)



## 원인
원본 이슈에서 확인 필요. GitHub Issue #52763 참조.

## 해결법
Add a double-check on the outbound path — `deliverWebReply` should refuse to send to group IDs (`*@g.us`) that are not in the allowlist, regardless of how the inbound message was classified.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52763
