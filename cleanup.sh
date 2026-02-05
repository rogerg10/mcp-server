#!/bin/bash
# cleanup.sh
# Simple helper to stop any AgentCore processes running this app

set -e

echo "Stopping agentcore processes (if any)..."
pkill -f "agentcore launch" || true

echo "Cleanup complete."
