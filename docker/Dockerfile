# Image for registries that only need the MCP server to start and answer
# introspection (initialize, tools/list). The graphical terminal is Windows-only
# and is not part of this image; tool calls that need it report that no
# xtell-serial window is running.
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "mcp>=1.0,<2" "pyserial>=3.5"

COPY xtell_serial_mcp.py ./

CMD ["python", "xtell_serial_mcp.py"]
