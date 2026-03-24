# First-class tool execution approvals: pause + interrupt + resume

## 증상
First-class Tool Execution Approvals: Pause, Interrupt, and Resume



## 원인
원본 이슈에서 확인 필요. GitHub Issue #19072 참조.

## 해결법
through sessions.
4. **Policy defaults + docs**
   - Add safe defaults for risky tools and migration docs.
5. **Adapter migration**
   - Move Lessinbox adapter to core pause/resume path; keep compatibility shim.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/19072
