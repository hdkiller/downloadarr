#!/bin/bash

# Activate virtual environment if it exists
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi
while true; do
    # Run downloadarr
    python3 downloadarr.py
    timestamp=$(date +"%Y-%m-%d %H:%M:%S")
    echo "[$timestamp] Downloadarr exited, restarting in 120s..."
    sleep 120
done
