# invoke_agentcore.py
# Generic CLI helper to invoke a Bedrock AgentCore runtime with streaming output

import argparse
import json
import subprocess
import sys
import textwrap

import boto3

# Create Bedrock AgentCore boto3 client
agent_core_client = boto3.client("bedrock-agentcore")

# -----------------------------------------------------------------------------
# Streaming chunk formatter for nicer console UX
# -----------------------------------------------------------------------------

def process_streaming_chunk(json_data):
    """Process streaming JSON chunks and format tool usage and output."""
    if isinstance(json_data, dict):
        # Tool usage events
        if "tool_use" in json_data or "tool_call" in json_data:
            tool_info = json_data.get("tool_use", json_data.get("tool_call", {}))
            tool_name = tool_info.get("name", "unknown_tool")
            tool_args = tool_info.get("input", tool_info.get("arguments", {}))
            print(f"\n[tool] {tool_name}")
            if tool_args:
                print(json.dumps(tool_args, indent=2))
            print("[status] running...")
            return ""

        # Tool results
        if "tool_result" in json_data:
            result = json_data["tool_result"]
            print("\n[tool] completed")
            if isinstance(result, dict) and "content" in result:
                content = result["content"]
                if isinstance(content, str) and len(content) > 200:
                    print(f"[result] {content[:200]}...")
                else:
                    print(f"[result] {content}")
            print("[status] agent continuing...\n")
            return ""

        # Thinking / reasoning
        if "thinking" in json_data or "reasoning" in json_data:
            thinking = json_data.get("thinking", json_data.get("reasoning", ""))
            print(f"\n[thinking] {thinking}")
            return ""

        # Step information
        if "step" in json_data:
            step_info = json_data["step"]
            step_num = step_info.get("number", "?")
            step_desc = step_info.get("description", step_info.get("action", ""))
            print(f"\n[step {step_num}] {step_desc}")
            return ""

        # Status updates
        if "status" in json_data:
            status = json_data["status"]
            print(f"\n[status] {status}")
            return ""

        # Content chunks (main assistant text)
        if "content" in json_data:
            content = json_data["content"]
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict) and "text" in item:
                        text_parts.append(item["text"])
                    elif isinstance(item, str):
                        text_parts.append(item)
                return "".join(text_parts)
            if isinstance(content, str):
                return content
            return str(content)

        # Delta-style content
        if "delta" in json_data:
            delta = json_data["delta"]
            if isinstance(delta, dict) and "content" in delta:
                return delta["content"]
            if isinstance(delta, str):
                return delta

        # Text field directly
        if "text" in json_data:
            return json_data["text"]

        # Event markers
        if "event" in json_data:
            event_type = json_data["event"]
            print(f"\n[event] {event_type}")
            return ""

    if isinstance(json_data, str):
        return json_data

    return None

# -----------------------------------------------------------------------------
# Streaming invocation helper
# -----------------------------------------------------------------------------

def stream_response(prompt, agent_arn=None, session_id=None):
    """Invoke a Bedrock AgentCore runtime with streaming output."""
    # Discover a default AgentCore runtime ARN if not provided (simple example)
    if agent_arn is None:
        result = subprocess.run(
            "aws bedrock-agentcore-control list-agent-runtimes "
            "| jq -r .agentRuntimes[].agentRuntimeArn | head -n 1",
            shell=True,
            capture_output=True,
            text=True,
            executable="/bin/bash",
        )
        default_agent_arn = result.stdout.strip()
    else:
        default_agent_arn = agent_arn

    default_session_id = "generic-session-abc123xyz789mnjikg"
    agent_runtime_arn = agent_arn or default_agent_arn
    runtime_session_id = session_id or default_session_id        

    payload = json.dumps({"prompt": prompt}).encode()

    print(f"[invoke] agentRuntimeArn={agent_runtime_arn}")
    print(f"[invoke] sessionId={runtime_session_id}")
    print("[invoke] sending prompt:")
    print(textwrap.indent(prompt, "  "))
    print("=" * 60)

    try:
        response = agent_core_client.invoke_agent_runtime(
            agentRuntimeArn=agent_runtime_arn,
            runtimeSessionId=runtime_session_id,
            payload=payload,
        )

        print("[stream] starting...")
        content_parts = []

        if "text/event-stream" in response.get("contentType", ""):
            # Handle SSE-style streaming
            stream = response.get("response", {})
            if hasattr(stream, "iter_lines"):
                for line in stream.iter_lines():
                    if not line:
                        continue

                    if isinstance(line, bytes):
                        try:
                            line_str = line.decode("utf-8")
                        except UnicodeDecodeError:
                            line_str = line.decode("utf-8", errors="replace")
                    else:
                        line_str = str(line)

                    if line_str.startswith("data: "):
                        data_content = line_str[6:].strip()
                        if not data_content or data_content == "[DONE]":
                            continue

                        try:
                            json_data = json.loads(data_content)
                            chunk = process_streaming_chunk(json_data)
                            if chunk:
                                print(chunk, end="", flush=True)
                                content_parts.append(chunk)
                        except json.JSONDecodeError:
                            print(data_content, end="", flush=True)
                            content_parts.append(data_content)
                    else:
                        print(line_str, end="", flush=True)
                        content_parts.append(line_str)
        else:
            # Non-streaming fallback
            return handle_non_streaming_response(response)

        print("\n" + "=" * 60)
        print("[stream] complete")
        return "".join(content_parts)

    except Exception as e:
        print(f"[error] invoking agent: {e}")
        return None

# -----------------------------------------------------------------------------
# Non-streaming fallback handler
# -----------------------------------------------------------------------------

def handle_non_streaming_response(response):
    """Handle non-streaming responses or StreamingBody with manual chunking."""
    response_data = response.get("response")

    if hasattr(response_data, "read"):
        print("[stream] streaming body detected")
        content_buffer = ""
        json_buffer = ""
        byte_buffer = b""

        while True:
            chunk = response_data.read(64)
            if not chunk:
                if byte_buffer:
                    try:
                        chunk_str = byte_buffer.decode("utf-8", errors="replace")
                        content_buffer += chunk_str
                        json_buffer += chunk_str
                    except Exception:
                        pass
                break

            if isinstance(chunk, bytes):
                byte_buffer += chunk
                try:
                    chunk_str = byte_buffer.decode("utf-8")
                    content_buffer += chunk_str
                    json_buffer += chunk_str
                    byte_buffer = b""
                except UnicodeDecodeError:
                    if len(byte_buffer) > 4096:
                        chunk_str = byte_buffer.decode("utf-8", errors="replace")
                        content_buffer += chunk_str
                        json_buffer += chunk_str
                        byte_buffer = b""
            else:
                chunk_str = str(chunk)
                content_buffer += chunk_str
                json_buffer += chunk_str

            try:
                json_data = json.loads(json_buffer)
                json_buffer = ""
                chunk_out = process_streaming_chunk(json_data)
                if chunk_out:
                    print(chunk_out, end="", flush=True)
            except json.JSONDecodeError:
                pass

        print("\n[stream] complete")
        return content_buffer

    try:
        data = json.loads(response_data)
        chunk = process_streaming_chunk(data)
        if chunk:
            print(chunk)
        return chunk
    except Exception:
        print(response_data)
        return str(response_data)

# -----------------------------------------------------------------------------
# CLI entrypoint for invoking the agent
# -----------------------------------------------------------------------------

def main():
    """Simple CLI for invoking an AgentCore runtime."""
    parser = argparse.ArgumentParser(
        description="Invoke a Bedrock AgentCore runtime with streaming output."
    )
    parser.add_argument(
        "--agent-runtime-arn",
        type=str,
        default=None,
        help="Agent runtime ARN (defaults to first runtime found).",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Session ID for the agent invocation.",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        required=False,
        help="Prompt text to send to the agent.",
    )

    args = parser.parse_args()

    if args.prompt:
        prompt = args.prompt
    else:
        print("Enter prompt (Ctrl+D to finish):")
        prompt = sys.stdin.read().strip()

    if not prompt:
        print("No prompt provided. Exiting.")
        sys.exit(1)

    stream_response(prompt, agent_arn=args.agent_runtime_arn, session_id=args.session_id)

if __name__ == "__main__":
    main()
