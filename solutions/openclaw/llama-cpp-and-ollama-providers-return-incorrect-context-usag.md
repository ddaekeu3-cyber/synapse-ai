# llama-cpp and Ollama providers return incorrect context usage due to field name mismatch

## 증상
Behavior bug (incorrect output/state without crash)

에러 메시지:
```
[bug] llama-cpp and Ollama providers return incorrect context usage due to field name mismatch
```

---

## Issue Content

### Problem Description

OpenClaw fails to accurately track token usage d

## 원인
원본 이슈에서 확인 필요. GitHub Issue #53448 참조.

## 해결법
|
| ❌ **Ollama** | AFFECTED | Popular model management service |
| ✅ **vLLM** | NOT AFFECTED | Compatible (OpenAI format) |
| ✅ **HuggingFace TGI** | NOT AFFECTED | Compatible (OpenAI format) |
| ✅ **OpenAI API** | NOT AFFECTED | Compatible (OpenAI format) |

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 환경
- OpenClaw 버전: 해당 이슈 시점 기준
- 관련 스킬/도구: openclaw
- OS: 다양

## 출처
https://github.com/openclaw/openclaw/issues/53448
