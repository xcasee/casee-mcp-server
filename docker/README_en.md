# Docker Deployment Guide

This folder contains the Docker configuration files for casee-mcp-server.

## File Reference

| File | Purpose |
|------|---------|
| `Dockerfile` | Builds the development image (built from source) |
| `docker-compose.yml` | Development environment docker-compose configuration |
| `docker-compose.server.yml` | Production environment docker-compose configuration (uses prebuilt image) |

## Usage

### Development Environment (Build from Source)

```bash
# Run from the project root directory
cd casee_mcp_server

# 1. Configure the API Key
echo "CASEE_API_KEY=casee_xxx" > .env

# 2. Build and start the container
docker compose -f docker/docker-compose.yml up -d

# 3. Check container status
docker compose -f docker/docker-compose.yml ps

# 4. View logs
docker compose -f docker/docker-compose.yml logs -f

# 5. Stop the container
docker compose -f docker/docker-compose.yml down
```

### Production Environment (Use Prebuilt Image)

```bash
# 1. Configure environment variables
echo "CASEE_API_KEY=casee_xxx" > .env

# 2. Pull or load the image
# Option A: Pull from the registry
docker pull casee-mcp-server:1.0.0

# Option B: Load from a local tar file
docker load -i casee-mcp-server-1.0.0.tar

# 3. Start the service
docker compose -f docker/docker-compose.server.yml up -d

# 4. Check status
docker compose -f docker/docker-compose.server.yml ps
```

### Build and Export the Image (for Offline Deployment)

```bash
# Build the image
docker compose -f docker/docker-compose.yml build

# Export the image as a tar file
docker save casee-mcp-server:1.0.0 -o casee-mcp-server-1.0.0.tar

# Import the image on the target server
docker load -i casee-mcp-server-1.0.0.tar
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CASEE_API_KEY` | Yes | - | CaSee API Key |
| `CASEE_API_BASE_URL` | No | `https://casee.me` | CaSee Intelligence Server URL |
| `CASEE_TIMEOUT` | No | `30` | Request timeout (seconds) |
| `MCP_TRANSPORT` | No | `streamable-http` | Transport protocol |
| `MCP_HOST` | No | `0.0.0.0` | Listen address |
| `MCP_PORT` | No | `8100` | Listen port |
| `MCP_PATH` | No | `/mcp` | MCP endpoint path |

## Port Mapping

| Container Port | Host Port | Purpose |
|----------------|-----------|---------|
| 8100 | 8100 | Streamable-HTTP MCP service |

## Health Check

The container is configured with a health check that periodically runs the following command:

```bash
python3 -c "from casee import __version__; print('OK')"
```

- Check interval: 30 seconds
- Timeout: 5 seconds
- Retries: 3

## FAQ

### Q: How do I change the port mapping?

Edit the `ports` configuration in `docker-compose.yml` or `docker-compose.server.yml`:

```yaml
ports:
  - "custom-port:8100"  # e.g. "9000:8100"
```

### Q: How do I mount a configuration file?

Add a `volumes` configuration to `docker-compose.yml`:

```yaml
services:
  casee-mcp:
    volumes:
      - ./custom_config:/app/config
```

### Q: How do I use a custom domain?

It is recommended to use Nginx or another reverse proxy with SSL certificates configured.

## Related Links

- [Main project README](../README.md)
- [Chinese README](../README_zh.md)
- [OpenAPI Docs](https://casee.me/api-docs)
