---
layout: solution
title: "exec approval fails in control-ui over plain HTTP due to missing approval client recognition"
category: gog
---

# exec approval fails in control-ui over plain HTTP due to missing approval client recognition

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```shell
[gateway] hasExecApprovalClients: client=openclaw-control-ui scopes=[]
[gateway] exec.approval.request — expiring immediately, no approval route
GatewayClientRequestError: unknown or expired 

## 원인
원본 이슈에서 확인 필요. GitHub Issue #51932 참조.

## 해결법
ensure exec approval is registered before returning (#3357) is merged but does not resolve this case — the issue is upstream in scope resolution, not in registration timing. Proposed fix: hasExecApprovalClients() should additionally recognize GATEWAY_CLIENT_IDS.CONTROL_UI and webchat clients as valid approval clients regardless of scopes.
This issue is separate from the post-approval followup/chan

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: gog
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/51932
