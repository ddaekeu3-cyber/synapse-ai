---
layout: solution
title: "LLM inference too slow or performance degrades over time"
category: performance
source: MLJourney - Debugging Common Local LLM Errors
description: "Token generation below 20 tok/s (GPU) or 5 tok/s (CPU). Performance starts strong but degrades over time. High latency despite adequate"
---

# LLM inference too slow or performance degrades over time

## 증상
Token generation below 20 tok/s (GPU) or 5 tok/s (CPU). Performance starts strong but degrades over time. High latency despite adequate hardware.

## 원인
Suboptimal CPU thread configuration. Thermal throttling from excessive heat (>80-85°C GPU, >90-95°C CPU). GPU memory bandwidth limitations. Background processes consuming resources.

## 해결법
### 추론 성능 개선

1. **스레드 수 최적화**: 물리 코어 수에 맞춰 테스트
   ```bash
   OMP_NUM_THREADS=8 ./llama-server -m model.gguf
   ```
2. **온도 모니터링**: `nvidia-smi` (GPU), `lm-sensors` (CPU)
   - 80°C 이상이면 쓰로틀링 발생 → 냉각 개선
3. **RAM 업그레이드**: DDR4 3200MHz+ → CPU 바운드 작업 20-40% 개선
4. **백그라운드 앱 종료**: 브라우저, 업데이트, 바이러스 스캐너
5. **병목 프로파일링**: `nvidia-smi dmon` (GPU), `cProfile` (CPU)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
MLJourney - Debugging Common Local LLM Errors
