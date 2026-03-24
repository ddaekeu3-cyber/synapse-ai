# Local Ollama agent runs intermittently fail with 'fetch failed' / timeout on 2026.3.8-beta.1

## 증상
Regression / unresolved runtime bug (local Ollama via OpenClaw)

에러 메시지:
```bash
$ ollama run qwen3.5:122b-a10b "Reply with exactly: OK"
OK
```

OpenClaw failure logs:
```text
{"event":"embedded_run_agent_end", ... "error":"LLM request timed out.",
 "model":"qwen3.5:122b-a

## 원인
원본 이슈에서 확인 필요. GitHub Issue #44556 참조.

## 해결법
runtime bug (local Ollama via OpenClaw)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/44556
