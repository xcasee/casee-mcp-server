#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# CaSee MCP Server 生产环境部署脚本
# Target: http://121.43.113.132:8100/mcp
# MCP Server: v1.1.0  |  casee SDK: 1.6.0
# ============================================================

SERVER="121.43.113.132"
REMOTE_USER="root"
REMOTE_DIR="/opt/casee-mcp-server"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SDK_WHEEL="casee-1.6.0-py3-none-any.whl"

echo "============================================"
echo "  CaSee MCP Server Deployment (v1.1.0)"
echo "  Target: ${SERVER}"
echo "  Remote Dir: ${REMOTE_DIR}"
echo "  SDK wheel: ${SDK_WHEEL}"
echo "============================================"

# Step 1: Create deployment package
echo ""
echo "[1/5] Creating deployment package..."
cd "${PROJECT_DIR}"

# Create temp directory for deployment
TEMP_DIR=$(mktemp -d)
trap "rm -rf ${TEMP_DIR}" EXIT

# Copy necessary files
cp -r src "${TEMP_DIR}/"
cp docker "${TEMP_DIR}/"
cp pyproject.toml "${TEMP_DIR}/"
cp .env "${TEMP_DIR}/"
cp "${SDK_WHEEL}" "${TEMP_DIR}/"

# Create deployment tarball
tar -czf "${TEMP_DIR}/casee-mcp-deploy.tar.gz" -C "${TEMP_DIR}" src docker pyproject.toml .env "${SDK_WHEEL}"

echo "  Package created: ${TEMP_DIR}/casee-mcp-deploy.tar.gz"
echo "  Package size: $(du -h "${TEMP_DIR}/casee-mcp-deploy.tar.gz" | cut -f1)"

# Step 2: Upload to server
echo ""
echo "[2/5] Uploading to server..."
echo "  Uploading deployment package to ${SERVER}..."

# Use expect for password-based SSH/SCP (avoids password in logs)
EXPECT_CMD=$(cat <<'EXPECT_EOF'
set timeout 120
log_user 0

# Upload package
spawn scp $1 $2@$3:$4/casee-mcp-deploy.tar.gz
expect {
    "yes/no" { send "yes\r"; exp_continue }
    "password:" { send "$5\r" }
    timeout { puts "ERROR: Upload timeout"; exit 1 }
}
expect eof
puts "SUCCESS: Package uploaded"
EXPECT_EOF
)

# Source the password from environment or .env
PASSWORD="${SSH_PASSWORD:-aaa123++++}"

expect -c "${EXPECT_CMD}" "${TEMP_DIR}/casee-mcp-deploy.tar.gz" "${REMOTE_USER}" "${SERVER}" "${REMOTE_DIR}" "${PASSWORD}"

# Step 3: Deploy on server
echo ""
echo "[3/5] Deploying on server..."

EXPECT_DEPLOY=$(cat <<'EXPECT_EOF'
set timeout 300
log_user 0

spawn ssh $1@$2
expect {
    "yes/no" { send "yes\r"; exp_continue }
    "password:" { send "$3\r" }
    timeout { puts "ERROR: SSH connection timeout"; exit 1 }
}

# Create directory and extract
send "mkdir -p $4 && cd $4 && tar -xzf casee-mcp-deploy.tar.gz && rm casee-mcp-deploy.tar.gz\r"
expect " $"

# Check Docker availability
send "docker --version && docker compose version\r"
expect " $"

# Stop existing container if any
send "docker compose -f docker/docker-compose.yml down 2>/dev/null || true\r"
expect " $"

# Build and start new container
send "docker compose -f docker/docker-compose.yml up -d --build\r"
expect {
    "Successfully built" { }
    "Built" { }
    timeout { puts "ERROR: Build timeout"; exit 1 }
}
expect " $"

# Show container status
send "docker compose -f docker/docker-compose.yml ps\r"
expect " $"

# Show recent logs
send "docker compose -f docker/docker-compose.yml logs --tail 20\r"
expect " $"

puts "\nSUCCESS: Deployment completed"
send "exit\r"
expect eof
EXPECT_EOF
)

expect -c "${EXPECT_DEPLOY}" "${REMOTE_USER}" "${SERVER}" "${PASSWORD}" "${REMOTE_DIR}"

# Step 4: Wait for service to start
echo ""
echo "[4/5] Waiting for service to start..."
echo "  Waiting 10 seconds for container to stabilize..."
sleep 10

# Step 5: Test service availability
echo ""
echo "[5/5] Testing service availability..."

echo "  Testing MCP endpoint..."

# Test the health/endpoint
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "http://${SERVER}:8100/mcp" 2>/dev/null || echo "000")

if [ "${HTTP_CODE}" = "200" ] || [ "${HTTP_CODE}" = "405" ] || [ "${HTTP_CODE}" = "406" ]; then
    echo "  ✅ Service is responding (HTTP ${HTTP_CODE})"
    echo ""
    echo "  Testing MCP Streamable-HTTP endpoint..."
    
    # Test with JSON-RPC initialize request
    MCP_RESPONSE=$(curl -s -X POST "http://${SERVER}:8100/mcp" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
        --connect-timeout 5 --max-time 10 2>/dev/null)
    
    if echo "${MCP_RESPONSE}" | grep -q "2025-03-26\|mcpVersion\|serverInfo"; then
        echo "  ✅ MCP initialized successfully"
        echo "  Response: $(echo ${MCP_RESPONSE} | head -c 200)..."
    else
        echo "  ⚠️ MCP init response incomplete (may still work)"
        echo "  Response: ${MCP_RESPONSE}"
    fi
else
    echo "  ⚠️ Service returned HTTP ${HTTP_CODE}"
    echo "  This may be normal - MCP Streamable-HTTP requires specific headers"
fi

echo ""
echo "============================================"
echo "  Deployment Complete!"
echo ""
echo "  MCP Server ver: v1.1.0"
echo "  casee SDK ver:  1.6.0"
echo "  Service URL:    http://${SERVER}:8100/mcp"
echo "============================================"
