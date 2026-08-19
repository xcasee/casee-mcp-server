"""casee-mcp CLI entry point.

Usage:
    casee-mcp          # stdio mode (default)
    casee-mcp --http   # streamable-http mode
"""

import argparse
import os


def main():
    parser = argparse.ArgumentParser(
        prog="casee-mcp",
        description="CaSee Intelligence MCP Server",
    )
    parser.add_argument(
        "--http", action="store_true",
        help="Run in Streamable-HTTP mode (default: stdio)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("MCP_HOST", "0.0.0.0"),
        help="HTTP host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MCP_PORT", "8100")),
        help="HTTP port (default: 8100)",
    )
    args = parser.parse_args()

    if args.http:
        os.environ["MCP_TRANSPORT"] = "streamable-http"
        if args.host:
            os.environ["MCP_HOST"] = args.host
        if args.port:
            os.environ["MCP_PORT"] = str(args.port)

    from casee_mcp_server.server import main as run_server
    run_server()


if __name__ == "__main__":
    main()