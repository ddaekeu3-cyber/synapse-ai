# Add clearer timeout controls for slow local-model providers

## 증상
Slow local-model runs appear to need more explicit timeout control in OpenClaw, especially for larger Ollama models (for example 32B-class coder models on local Apple Silicon hardware).



## 원인
원본 이슈에서 확인 필요. GitHub Issue #51331 참조.

## 해결법
es
- Add explicit provider-level timeout configuration for local providers.
- Document recommended timeout settings for larger local models.
- Surface timeout cause more clearly in errors/logs.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/51331
