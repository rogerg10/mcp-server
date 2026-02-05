#!/bin/bash
# generate_launch_command.sh
# Generate an `agentcore launch` command from config.yaml (for local dev)

set -e  # Exit on error

# Basic color codes for nicer CLI output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

CONFIG_FILE="config.yaml"
PYTHON_FILE="data_agentcore.py"
NO_CONFIRM=false

# Parse command line options
if [ "$1" == "--no-confirm" ]; then
  NO_CONFIRM=true
fi

# Ensure config.yaml exists in current directory
if [ ! -f "$CONFIG_FILE" ]; then
  echo -e "${RED}Error: $CONFIG_FILE not found!${NC}" >&2
  exit 1
fi

# Parse YAML configuration into key=value env variables using Python
ENV_VARS=$(python3 << EOF
import yaml
import sys

try:
    with open('$CONFIG_FILE', 'r') as f:
        config = yaml.safe_load(f) or {}

    snowflake = config.get('snowflake', {})
    mcp = config.get('mcp', {})
    aws = config.get('aws', {})
    agent = config.get('agent', {})

    if snowflake.get('account'):
        print(f"SNOWFLAKE_ACCOUNT={snowflake['account']}")
    if snowflake.get('user'):
        print(f"SNOWFLAKE_USER={snowflake['user']}")
    if snowflake.get('pat_token'):
        print(f"SNOWFLAKE_PAT_TOKEN={snowflake['pat_token']}")
    if snowflake.get('database'):
        print(f"SNOWFLAKE_DATABASE={snowflake['database']}")
    if snowflake.get('schema'):
        print(f"SNOWFLAKE_SCHEMA={snowflake['schema']}")
    if snowflake.get('warehouse'):
        print(f"SNOWFLAKE_WAREHOUSE={snowflake['warehouse']}")

    if mcp.get('server_name'):
        print(f"MCP_SERVER_NAME={mcp['server_name']}")

    if aws.get('place_index_name'):
        print(f"AWS_PLACE_INDEX_NAME={aws['place_index_name']}")

    if agent.get('model'):
        print(f"AGENT_MODEL={agent['model']}")
    if agent.get('max_history_turns'):
        print(f"AGENT_MAX_HISTORY_TURNS={agent['max_history_turns']}")

except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
EOF
)

if [ $? -ne 0 ]; then
  exit 1
fi

# Build the agentcore launch command with environment variables
CMD="agentcore launch

while IFS='=' read -r key value; do
  if [ -n "$value" ]; then
    CMD="$CMD --env $key=$value"
  fi
done <<< "$ENV_VARS"

# Either print or execute the command with confirmation
if [ "$NO_CONFIRM" = true ]; then
  echo "$CMD"
else
  echo -e "${GREEN}=== Generated AgentCore Launch Command ===${NC}"
  echo -e "${BLUE}$CMD${NC}"
  echo ""
  echo -e "${YELLOW}This will launch AgentCore with the above configuration.${NC}"
  read -p "Execute this command? (y/n) " -n 1 -r
  echo ""
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${GREEN}Launching AgentCore...${NC}"
    echo ""
    eval $CMD
  else
    echo -e "${YELLOW}Launch cancelled.${NC}"
    echo ""
    echo "To launch manually, run:"
    echo "$CMD"
    exit 0
  fi
fi
