FROM python:3.12-slim

LABEL org.opencontainers.image.title="casee-mcp-server"
LABEL org.opencontainers.image.description="CaSee Intelligence MCP Server for AI Agents"
LABEL org.opencontainers.image.version="1.0.1"
LABEL org.opencontainers.image.url="https://casee.me"

WORKDIR /app

# Copy casee SDK wheel and install
COPY casee-1.4.1-py3-none-any.whl /tmp/
RUN pip install --no-cache-dir \
    /tmp/casee-1.4.1-py3-none-any.whl \
    "mcp>=1.0.0" && \
    rm /tmp/casee-1.4.1-py3-none-any.whl

# Copy source code
COPY src/ /app/src/
COPY pyproject.toml /app/

# Install package in development mode
RUN pip install --no-cache-dir -e .

# Expose Streamable-HTTP port
EXPOSE 8100

ENV MCP_TRANSPORT=streamable-http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8100
ENV MCP_PATH=/mcp

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "from casee import __version__; print('OK')" || exit 1

ENTRYPOINT ["casee-mcp", "--http"]
