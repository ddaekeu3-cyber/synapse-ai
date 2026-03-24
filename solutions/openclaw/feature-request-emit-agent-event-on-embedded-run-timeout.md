# Feature Request: Emit Agent Event on Embedded Run Timeout

## 증상
When an embedded run times out, OpenClaw only logs a warning but does not emit an agent event. This makes it impossible for plugins (like task-monitor) to detect and handle timeout scenarios.

에러 메시지:
```javascript
const abortTimer = setTimeout(() => {
    if (!isProbeSession) log$14.warn(`embedded run timeout: runId=${params.runId}...`);
    // Only logs, no emitAgentEvent!
    abortRun(true);
}, 

## 원인
원본 이슈에서 확인 필요. GitHub Issue #52353 참조.

## 해결법
Add `emitAgentEvent` call before returning from timeout handler:

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/52353
