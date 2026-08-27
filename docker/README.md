# Docker 部署文档

本文件夹包含 casee-mcp-server 的 Docker 相关配置文件。

## 文件说明

| 文件 | 用途 |
|------|------|
| `Dockerfile` | 用于构建开发镜像（从源码构建） |
| `docker-compose.yml` | 开发环境 docker-compose 配置 |
| `docker-compose.server.yml` | 生产环境 docker-compose 配置（使用预构建镜像） |

## 使用方法

### 开发环境（从源码构建）

```bash
# 在项目根目录执行
cd casee_mcp_server

# 1. 配置 API Key
echo "CASEE_API_KEY=casee_xxx" > .env

# 2. 构建并启动容器
docker compose -f docker/docker-compose.yml up -d

# 3. 查看容器状态
docker compose -f docker/docker-compose.yml ps

# 4. 查看日志
docker compose -f docker/docker-compose.yml logs -f

# 5. 停止容器
docker compose -f docker/docker-compose.yml down
```

### 生产环境（使用预构建镜像）

```bash
# 1. 配置环境变量
echo "CASEE_API_KEY=casee_xxx" > .env

# 2. 拉取或加载镜像
# 方式 A：从仓库拉取
docker pull casee-mcp-server:1.0.0

# 方式 B：从本地 tar 加载
docker load -i casee-mcp-server-1.0.0.tar

# 3. 启动服务
docker compose -f docker/docker-compose.server.yml up -d

# 4. 查看状态
docker compose -f docker/docker-compose.server.yml ps
```

### 构建并导出镜像（用于离线部署）

```bash
# 构建镜像
docker compose -f docker/docker-compose.yml build

# 导出镜像为 tar 文件
docker save casee-mcp-server:1.0.0 -o casee-mcp-server-1.0.0.tar

# 在目标服务器导入镜像
docker load -i casee-mcp-server-1.0.0.tar
```

## 环境变量

| 变量 | 必需 | 默认值 | 说明 |
|------|------|--------|------|
| `CASEE_API_KEY` | 是 | - | CaSee API Key |
| `CASEE_API_BASE_URL` | 否 | `https://casee.me` | CaSee Intelligence Server 地址 |
| `CASEE_TIMEOUT` | 否 | `30` | 请求超时时间（秒） |
| `MCP_TRANSPORT` | 否 | `streamable-http` | 传输协议 |
| `MCP_HOST` | 否 | `0.0.0.0` | 监听地址 |
| `MCP_PORT` | 否 | `8100` | 监听端口 |
| `MCP_PATH` | 否 | `/mcp` | MCP 端点路径 |

## 端口映射

| 容器端口 | 主机端口 | 用途 |
|---------|---------|------|
| 8100 | 8100 | Streamable-HTTP MCP 服务 |

## 健康检查

容器配置了健康检查，会定期执行以下命令：

```bash
python3 -c "from casee import __version__; print('OK')"
```

- 检查间隔：30 秒
- 超时时间：5 秒
- 重试次数：3 次

## 常见问题

### Q: 如何修改端口映射？

编辑 `docker-compose.yml` 或 `docker-compose.server.yml` 中的 `ports` 配置：

```yaml
ports:
  - "自定义端口:8100"  # 例如 "9000:8100"
```

### Q: 如何挂载配置文件？

在 `docker-compose.yml` 中添加 `volumes` 配置：

```yaml
services:
  casee-mcp:
    volumes:
      - ./custom_config:/app/config
```

### Q: 如何使用自定义域名？

建议使用 Nginx 或其他反向代理，并配置 SSL 证书。

## 相关链接

- [主项目 README](../README.md)
- [英文 README](../README.md)
- [中文 README](../README_zh.md)
- [OpenAPI 文档](https://casee.me/api-docs)
