# data_agentcore.py
# Generic Bedrock AgentCore app wired to Snowflake + Snowflake MCP

import boto3
from bedrock_agentcore import BedrockAgentCoreApp
import json
import logging
import requests
from pathlib import Path
from typing import Dict, Any, Optional

import yaml
from strands import Agent, tool
from strands.models.anthropic import AnthropicModel
from strands_tools import current_time, http_request, use_aws, retrieve
import snowflake.connector

# Create Bedrock AgentCore application instance
app = BedrockAgentCoreApp()

# Configure basic logging for the application
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Configuration loading from config.yaml with safe defaults
# -----------------------------------------------------------------------------

def load_config() -> Dict[str, Any]:
    """Load configuration from config.yaml file with fallback to empty dict."""
    config_file = Path(__file__).parent / "config.yaml"
    if config_file.exists():
        try:
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
            logger.info(f"Configuration loaded from {config_file}")
            return config
        except Exception as e:
            logger.warning(f"Failed to load config.yaml: {e}. Using empty config.")
            return {}
    else:
        logger.warning("config.yaml not found, using empty config.")
        return {}

def get_config(key: str, section: Optional[str] = None, default: Any = None) -> Any:
    """Get configuration value from loaded config with an optional default."""
    if section and section in CONFIG and key in CONFIG[section]:
        return CONFIG[section][key]
    return default

# Load configuration once at module import
CONFIG: Dict[str, Any] = load_config()

# -----------------------------------------------------------------------------
# MCP client for calling tools on a Snowflake-managed MCP server
# -----------------------------------------------------------------------------

class MCPClient:
    """Client for interacting with a Snowflake-managed MCP server over HTTP."""

    def __init__(
        self,
        mcp_endpoint: str,
        auth_token: str,
        database: str,
        schema: str,
        mcp_server_name: str,
    ):
        """Initialize MCP client with endpoint, auth, and Snowflake context."""
        self.mcp_endpoint = mcp_endpoint
        self.auth_token = auth_token
        self.database = database
        self.schema = schema
        self.mcp_server_name = mcp_server_name
        # Build full MCP server URL (JSON-RPC endpoint)
        self.mcp_url = (
            f"{mcp_endpoint}/api/v2/databases/{database}/schemas/{schema}"
            f"/mcp-servers/{mcp_server_name}"
        )

    def _create_jsonrpc_payload(
        self,
        method: str,
        tool_name: str,
        arguments: Dict[str, Any],
        request_id: int = 1,
    ) -> Dict[str, Any]:
        """Create JSON-RPC 2.0 payload for MCP tool invocation."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

    def call_mcp_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        request_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Call a tool on the MCP server using direct HTTP POST."""
        if request_id is None:
            request_id = 1

        payload = self._create_jsonrpc_payload(
            "tools/call", tool_name, arguments, request_id
        )

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.auth_token}",
        }

        try:
            logger.info(
                f"Calling MCP tool '{tool_name}' at {self.mcp_url} "
                f"with arguments: {arguments}"
            )
            response = requests.post(
                self.mcp_url,
                headers=headers,
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            response_data = response.json()
            logger.info("MCP call completed successfully")
            return {"success": True, "response": response_data}
        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP request error calling MCP tool '{tool_name}': {e}")
            return {"success": False, "error": f"HTTP request failed: {e}"}
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error for MCP tool '{tool_name}': {e}")
            return {"success": False, "error": f"Invalid JSON response: {e}"}
        except Exception as e:
            logger.error(f"Unexpected error calling MCP tool '{tool_name}': {e}")
            return {"success": False, "error": str(e)}

# -----------------------------------------------------------------------------
# Snowflake / MCP configuration pulled from config.yaml
# -----------------------------------------------------------------------------

SNOWFLAKE_ACCOUNT = get_config("account", "snowflake")
SNOWFLAKE_USER = get_config("user", "snowflake")
SNOWFLAKE_PAT_TOKEN = get_config("pat_token", "snowflake")
SNOWFLAKE_DATABASE = get_config("database", "snowflake")
SNOWFLAKE_SCHEMA = get_config("schema", "snowflake")
SNOWFLAKE_WAREHOUSE = get_config("warehouse", "snowflake")
MCP_SERVER_NAME = get_config("server_name", "mcp")

AWS_PLACE_INDEX_NAME = get_config("place_index_name", "aws")

# Build Snowflake base endpoint from account name
MCP_ENDPOINT = f"https://{SNOWFLAKE_ACCOUNT}.snowflakecomputing.com"

logger.info(
    f"Snowflake: {SNOWFLAKE_USER}@{SNOWFLAKE_ACCOUNT}/"
    f"{SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA}"
)
logger.info(
    f"MCP Server: {SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA}.{MCP_SERVER_NAME}"
)

# Initialize MCP client with config-driven parameters
mcp_client = MCPClient(
    mcp_endpoint=MCP_ENDPOINT,
    auth_token=SNOWFLAKE_PAT_TOKEN,
    database=SNOWFLAKE_DATABASE,
    schema=SNOWFLAKE_SCHEMA,
    mcp_server_name=MCP_SERVER_NAME,
)

# Initialize Amazon Location Service client for geocoding
location_client = boto3.client("location")

# -----------------------------------------------------------------------------
# Snowflake connection helper using PAT
# -----------------------------------------------------------------------------

def get_snowflake_connection():
    """Create and return a Snowflake connection using PAT as password."""
    try:
        conn = snowflake.connector.connect(
            account=SNOWFLAKE_ACCOUNT,
            user=SNOWFLAKE_USER,
            password=SNOWFLAKE_PAT_TOKEN,
            database=SNOWFLAKE_DATABASE,
            schema=SNOWFLAKE_SCHEMA,
            warehouse=SNOWFLAKE_WAREHOUSE,
        )
        logger.info("Successfully connected to Snowflake using PAT token")

        cursor = conn.cursor()
        cursor.execute(f"USE WAREHOUSE {SNOWFLAKE_WAREHOUSE}")
        cursor.close()
        logger.info(f"Activated warehouse: {SNOWFLAKE_WAREHOUSE}")

        return conn
    except Exception as e:
        logger.error(f"Failed to connect to Snowflake: {e}")
        return None

# -----------------------------------------------------------------------------
# Generic tools: geocoding, MCP data tool, direct Snowflake SQL
# -----------------------------------------------------------------------------

@tool
def geocode_address(place_index_name: str, address: str) -> dict:
    """Geocode a street address into coordinates using Amazon Location Service."""
    response = location_client.search_place_index_for_text(
        IndexName=place_index_name,
        Text=address,
    )

    if response.get("Results"):
        result = response["Results"][0]
        return {
            "coordinates": result["Place"]["Geometry"]["Point"],
            "label": result["Place"]["Label"],
        }

    return {"error": "Address not found."}

@tool
def call_mcp_data_tool(tool_name: str, arguments_json: str) -> str:
    """Call a configured MCP tool by name with JSON-encoded arguments."""
    try:
        arguments = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as e:
        return f"Invalid JSON for arguments: {e}"

    result = mcp_client.call_mcp_tool(tool_name, arguments)

    if result.get("success", False):
        response_data = result.get("response", {})
        if "result" in response_data:
            tool_result = response_data["result"]
            if "content" in tool_result:
                content = tool_result["content"]
                if isinstance(content, list):
                    return "\n".join(
                        [
                            str(item.get("text", item))
                            if isinstance(item, dict)
                            else str(item)
                            for item in content
                        ]
                    )
                return str(content)
            return json.dumps(tool_result, indent=2)
        return json.dumps(response_data, indent=2)

    return f"MCP error: {result.get('error', 'Unknown error')}"

@tool
def run_snowflake_query(statement: str) -> str:
    """Execute a SQL statement in Snowflake and return a formatted result string."""
    conn = None
    cursor = None

    try:
        conn = get_snowflake_connection()
        if not conn:
            return "Error: Failed to establish Snowflake connection"

        cursor = conn.cursor()
        cursor.execute(statement)

        results = cursor.fetchall()
        column_names = (
            [desc[0] for desc in cursor.description] if cursor.description else []
        )

        if not results:
            return "Query executed successfully. No results returned."

        output = []
        output.append(f"Query returned {len(results)} row(s)")
        output.append("\nColumns: " + " | ".join(column_names))
        output.append("-" * 80)

        for i, row in enumerate(results[:100]):
            row_str = " | ".join(
                [str(val) if val is not None else "NULL" for val in row]
            )
            output.append(f"Row {i + 1}: {row_str}")

        if len(results) > 100:
            output.append(f"\n... ({len(results) - 100} more rows not shown)")

        return "\n".join(output)
    except snowflake.connector.errors.ProgrammingError as e:
        logger.error(f"Snowflake query error: {e}")
        return f"Query Error: {e}"
    except Exception as e:
        logger.error(f"Unexpected error executing 
