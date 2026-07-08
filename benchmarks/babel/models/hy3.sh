#!/bin/bash
#SBATCH --job-name=unity-hy3
#SBATCH --partition=general
#SBATCH --gres=gpu:8
#SBATCH --cpus-per-task=16
#SBATCH --mem=384G
#SBATCH --time=2-00:00:00
#SBATCH --output=%x-%j.out
# EXPERIMENTAL: Hunyuan 3 (295B MoE, 21B active, Apache-2.0). FP8 ~295GB -> TP8, tight on 384GB.
# Hy3 support landed in vLLM at its launch (mid-2026); babel's CUDA-12.9 driver caps us at an
# older vLLM, so this may fail with an unsupported-architecture error. Try it; if it fails, use
# hy3 via OpenRouter free (tencent/hy3:free) instead. Serves on port 8003.
source ~/miniconda/etc/profile.d/conda.sh
conda activate unity-new
mkdir -p /data/user_data/$USER/hf 2>/dev/null || true
export HF_HOME=/data/user_data/$USER/hf
echo "=== serving hy3 on http://$(hostname):8003/v1 (api key: unity) ==="
vllm serve tencent/Hy3-FP8 \
  --served-model-name hy3 \
  --tensor-parallel-size 8 \
  --max-model-len 65536 \
  --kv-cache-dtype fp8 \
  --api-key unity \
  --host 0.0.0.0 --port 8003 \
  --download-dir /data/user_data/$USER/hf
