"""Tiny local MCP stdio server used by the M5 offline demo."""

from mcp.server.mcpserver import MCPServer

server = MCPServer("picoclaw-m5-demo")


@server.tool()
def word_count(text: str) -> str:
    """Count whitespace-separated words in text."""
    return str(len(text.split()))


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
