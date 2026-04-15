---
layout: solution
title: "Firewall or proxy blocks model downloads from Hugging Face"
category: config
source: MLJourney - Debugging Common Local LLM Errors
description: "\"Connection refused\" or timeout during model downloads. Cannot access Hugging Face or GitHub model repositories. Fails in corporate/restricted"
---

# Firewall or proxy blocks model downloads from Hugging Face

## 증상
"Connection refused" or timeout during model downloads. Cannot access Hugging Face or GitHub model repositories. Fails in corporate/restricted networks.

## 원인
Firewall blocking model repository domains. Corporate proxies requiring special configuration. Offline environments lacking pre-downloaded models.

## 해결법
### 네트워크/방화벽 차단 해결

1. **연결 테스트**: `curl -I https://huggingface.co`
2. **프록시 설정**:
   ```bash
   export HTTP_PROXY=http://proxy:8080
   export HTTPS_PROXY=http://proxy:8080
   ```
3. **오프라인 다운로드**: 연결된 환경에서 미리 다운로드
   ```bash
   export HF_HOME=/path/to/models
   huggingface-cli download model-name
   ```
4. **SSL 검사 우회**: 기업 프록시 SSL 인스펙션 → 인증서 설정 추가
5. **미러 사용**: HuggingFace 미러 사이트 활용 (중국: hf-mirror.com)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
MLJourney - Debugging Common Local LLM Errors
