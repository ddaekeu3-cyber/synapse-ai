---
layout: solution
title: "Model loading fails: format mismatch, corruption, or permission denied"
category: config
source: MLJourney - Debugging Common Local LLM Errors
description: "\"Model not found\", \"permission denied\", \"Invalid model file\", \"unknown version\", \"Unexpected EOF\", or \"corruption detected\" errors when loading local"
---

# Model loading fails: format mismatch, corruption, or permission denied

## 증상
"Model not found", "permission denied", "Invalid model file", "unknown version", "Unexpected EOF", or "corruption detected" errors when loading local LLM.

## 원인
Incompatible file formats (GGUF vs GGML vs safetensors). Corrupted partial downloads. File permission restrictions or incorrect paths.

## 해결법
### 모델 로딩 실패 해결

1. **경로 확인**: 절대 경로 사용, 공백은 따옴표로 감싸기
2. **포맷 호환성**:
   - llama.cpp → GGUF 포맷
   - transformers → safetensors 포맷
   - 틀리면 변환 도구 사용
3. **파일 무결성 검증**:
   ```bash
   sha256sum model.gguf  # 공식 체크섬과 비교
   ```
4. **권한 수정**: `chmod +r model.gguf`
5. **재다운로드**: 체크섬 불일치 시 resume 지원 다운로더로 재다운로드

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
MLJourney - Debugging Common Local LLM Errors
