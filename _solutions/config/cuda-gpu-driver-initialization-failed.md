---
layout: solution
title: "CUDA initialization failed / GPU not recognized despite nvidia-smi working"
category: config
source: MLJourney - Debugging Common Local LLM Errors
description: "\"CUDA initialization failed\", \"No kernel image available for device\". GPU not recognized by PyTorch despite nvidia-smi showing the"
---

# CUDA initialization failed / GPU not recognized despite nvidia-smi working

## 증상
"CUDA initialization failed", "No kernel image available for device". GPU not recognized by PyTorch despite nvidia-smi showing the GPU.

## 원인
PyTorch CUDA version mismatches with GPU drivers. Conflicting applications holding GPU locks. Outdated or incompatible GPU drivers.

## 해결법
### CUDA/GPU 드라이버 문제 해결

1. **버전 호환성 확인**:
   ```bash
   nvcc --version       # 시스템 CUDA
   nvidia-smi           # 드라이버 버전
   python -c "import torch; print(torch.version.cuda)"  # PyTorch CUDA
   ```
2. **드라이버 업데이트**: `sudo apt install nvidia-driver-535`
3. **PyTorch 재설치** (CUDA 버전 맞춰서):
   ```bash
   pip install torch --index-url https://download.pytorch.org/whl/cu121
   ```
4. **GPU 접근 테스트**: `python -c "import torch; print(torch.cuda.is_available())"`
5. **충돌 앱 종료**: 다른 ML 프레임워크, 마이닝 소프트웨어 등
6. **GPU 리셋**: `nvidia-smi --gpu-reset` (모든 GPU 프로세스 종료됨 주의)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
MLJourney - Debugging Common Local LLM Errors
