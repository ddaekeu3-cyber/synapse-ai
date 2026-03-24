# Feishu plugin duplicate registration causes Gateway crash

## 증상
When configuring Feishu channel and modifying the openclaw.json file, the Gateway crashes unexpectedly.

에러 메시지:
`
3. Try to manually add a second Feishu channel or modify the agent configuration
4. Gateway crashes

## Error Logs

`

## 원인
원본 이슈에서 확인 필요. GitHub Issue #37028 참조.

## 해결법
Use terminal to manually start agents with `openclaw agent <agent-id>` instead of channel-based routing

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/37028
