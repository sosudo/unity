#!/bin/bash
# Show unity model jobs, their nodes/ports, and the ssh-config lines to paste locally.
echo "=== jobs ==="
squeue -u $USER -o "%.10i %.22j %.8T %.10M %R"
echo
echo "=== ready servers ==="
for f in ~/unity-models/*.out; do
  [ -e "$f" ] || continue
  URL=$(grep -m1 -oE "http://[^ ]+:[0-9]+/v1" "$f" 2>/dev/null)
  JOB=$(basename "$f" .out)
  if [ -n "$URL" ] && grep -qE "Application startup complete|Uvicorn running" "$f"; then
    echo "READY  $JOB  $URL"
  elif [ -n "$URL" ]; then
    echo "boot   $JOB  $URL (not ready yet)"
  fi
done
echo
echo "=== paste into local ~/.ssh/config (babel-compute-node block) ==="
for f in ~/unity-models/*.out; do
  [ -e "$f" ] || continue
  grep -qE "Application startup complete|Uvicorn running" "$f" || continue
  NODE=$(grep -m1 -oE "http://[^ :]+:[0-9]+/v1" "$f" | sed -E "s@http://([^:]+):.*@\1@")
  PORT=$(grep -m1 -oE "http://[^ ]+:([0-9]+)/v1" "$f" | sed -E "s@.*:([0-9]+)/v1@\1@")
  [ -n "$NODE" ] && echo "    LocalForward $PORT $NODE:$PORT"
done
