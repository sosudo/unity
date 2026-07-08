#!/bin/bash
#SBATCH --job-name=unity-dsv4-flash
#SBATCH --partition=general
#SBATCH --gres=gpu:8
#SBATCH --cpus-per-task=16
#SBATCH --mem=384G
#SBATCH --time=2-00:00:00
#SBATCH --output=%x-%j.out
# DeepSeek-V4-Flash (284B MoE, 13B active, MIT). Native-FP8 weights ~284GB -> TP8;
# STANDALONE-ONLY on the 8-GPU cap.
# Serves on port 8005. agents.yaml: backend codex, base_url http://localhost:8005/v1, api_key unity.
source ~/miniconda/etc/profile.d/conda.sh
conda activate unity-new
mkdir -p /data/user_data/$USER/hf 2>/dev/null || true
export HF_HOME=/data/user_data/$USER/hf
echo "=== serving deepseek-v4-flash on http://$(hostname):8005/v1 (api key: unity) ==="
vllm serve deepseek-ai/DeepSeek-V4-Flash \
  --served-model-name deepseek-v4-flash \
  --tensor-parallel-size 8 \
  --max-model-len 65536 \
  --kv-cache-dtype fp8 \
  --api-key unity \
  --host 0.0.0.0 --port 8005 \
  --download-dir /data/user_data/$USER/hf
