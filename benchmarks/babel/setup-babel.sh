#!/bin/bash
# Unity model-serving setup (runs on a babel login node; heavy work backgrounded)
set -u
mkdir -p ~/unity-models
DATA=/data/user_data/$USER
mkdir -p "$DATA/hf" 2>/dev/null || DATA=$HOME

# ── sbatch scripts ────────────────────────────────────────────────
write_script() { # $1=file $2=model $3=served-name $4=tp $5=mem
cat > ~/unity-models/$1 <<EOF
#!/bin/bash
#SBATCH --job-name=unity-$3
#SBATCH --partition=general
#SBATCH --gres=gpu:$4
#SBATCH --cpus-per-task=12
#SBATCH --mem=${5}
#SBATCH --time=2-00:00:00
#SBATCH --output=%x-%j.out
# Serve $2 as an OpenAI-compatible endpoint for Unity.
# Point an agents.yaml entry at it (codex backend talks OpenAI wire format):
#   - names: [$3-1]
#     model: $3
#     backend: codex
#     provider: vllm
#     base_url: http://\$(hostname):8000/v1
#     api_key: unity
source ~/miniconda/etc/profile.d/conda.sh
conda activate unity
export HF_HOME=$DATA/hf
echo "=== serving $3 on http://\$(hostname):8000/v1 (api key: unity) ==="
vllm serve $2 \\
  --served-model-name $3 \\
  --tensor-parallel-size $4 \\
  --api-key unity \\
  --host 0.0.0.0 --port 8000 \\
  --download-dir $DATA/hf
EOF
chmod +x ~/unity-models/$1
}
write_script qwen3-32b.sh            Qwen/Qwen3-32B                     qwen3-32b            2 128G
write_script goedel-prover-v2-32b.sh Goedel-LM/Goedel-Prover-V2-32B     goedel-prover-v2-32b 2 128G
write_script llama33-70b.sh          meta-llama/Llama-3.3-70B-Instruct  llama33-70b          4 256G
# goedel serves on 8001 so both models can co-locate on one node
sed -i 's@--port 8000@--port 8001@; s@:8000/v1@:8001/v1@' ~/unity-models/goedel-prover-v2-32b.sh
echo "scripts written:"; ls ~/unity-models/

# ── conda env (backgrounded; slow) ────────────────────────────────
source ~/miniconda/etc/profile.d/conda.sh 2>/dev/null || source ~/anaconda3/etc/profile.d/conda.sh
conda env remove -n unity -y >/dev/null 2>&1
nohup bash -c '
source ~/miniconda/etc/profile.d/conda.sh
conda create -y -n unity python=3.12 &&
conda activate unity &&
pip install -q uv &&
uv pip install --system --python "$(which python)" -q "vllm==0.11.2" "flashinfer-cubin==0.5.2" --torch-backend=cu128 &&
echo UNITY_ENV_READY
' > ~/unity-env-setup.log 2>&1 &
echo "env setup backgrounded (log: ~/unity-env-setup.log)"
