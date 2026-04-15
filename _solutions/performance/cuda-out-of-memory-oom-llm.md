---
layout: solution
title: "CUDA out of memory (OOM) when running local LLM"
category: performance
source: MLJourney - Debugging Common Local LLM Errors
description: "\"CUDA out of memory\" or \"failed to allocate X bytes\" error. System freezes (Windows) or process terminates (Linux). Cannot load or run the"
---

# CUDA out of memory (OOM) when running local LLM

## 증상
"CUDA out of memory" or "failed to allocate X bytes" error. System freezes (Windows) or process terminates (Linux). Cannot load or run the model.

## 원인
Model weights exceed available VRAM/RAM. KV cache memory scales with context length and batch size. GPU memory fragmentation from repeated load/unload cycles.

## 해결법
### OOM 에러 해결

1. **메모리 확인**: `nvidia-smi` (GPU) 또는 `htop` (CPU)로 가용 메모리 확인
2. **양자화 적용**: FP16 → 8-bit (메모리 절반) 또는 4-bit (1/4)
   ```bash
   # llama.cpp 4-bit 양자화
   ./quantize model.gguf model-q4.gguf q4_k_m
   ```
3. **컨텍스트 윈도우 축소**: 8K → 4K로 줄이면 KV 캐시 1/4
4. **레이어 오프로딩**: GPU VRAM + CPU RAM 분할 사용
   ```bash
   ./llama-server -m model.gguf -ngl 20  # 20 레이어만 GPU
   ```
5. **프로세스 재시작**: GPU 메모리 단편화 해소

## 예상 토큰 절약
이 에러로 삽질 시: 약 10,000~30,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
MLJourney - Debugging Common Local LLM Errors
