#!/bin/bash
#SBATCH --job-name=unity-leanstral
#SBATCH --partition=general
#SBATCH --gres=gpu:8
#SBATCH --cpus-per-task=16
#SBATCH --mem=320G
#SBATCH --time=2-00:00:00
#SBATCH --output=%x-%j.out
# Leanstral 1.5 (119B-A6B MoE, Apache-2.0) — Mistral's Lean 4 proof-engineering model.
# 587/672 PutnamBench. bf16 ~238GB -> TP8 on 48GB GPUs. Serves on port 8004.
# agents.yaml: backend codex, base_url http://localhost:8004/v1 (via tunnel), api_key unity.
source ~/miniconda/etc/profile.d/conda.sh
conda activate unity-new
mkdir -p /data/user_data/$USER/hf 2>/dev/null || true
export HF_HOME=/data/user_data/$USER/hf
echo "=== serving leanstral on http://$(hostname):8004/v1 (api key: unity) ==="
vllm serve mistralai/Leanstral-1.5-119B-A6B \
  --served-model-name leanstral \
  --tokenizer-mode mistral --config-format mistral --load-format mistral \
  --tensor-parallel-size 8 \
  --max-model-len 131072 \
  --api-key unity \
  --host 0.0.0.0 --port 8004 \
  --download-dir /data/user_data/$USER/hf
