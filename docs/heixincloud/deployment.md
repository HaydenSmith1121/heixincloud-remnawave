---
sidebar_position: 1
title: 部署教程
---

# 黑心云部署教程

这套部署面向以下拓扑：

| 服务器         | 角色                 | 说明                                    |
| -------------- | -------------------- | --------------------------------------- |
| 美国服务器     | Panel + Caddy + Node | 公网提供服务面板，同时作为一个 VPN 节点 |
| 韩国服务器 1-3 | Node                 | 只运行节点服务                          |

部署模板位于仓库 `deploy/heixincloud/`：

- `panel/`：Panel、PostgreSQL、Valkey、Caddy
- `node/`：任意服务器都可复用的 Node 模板
- `response-rules.json`：订阅响应规则，必须导入 Panel

## 1. 准备信息

先准备这些值：

| 变量                         | 示例                | 用途              |
| ---------------------------- | ------------------- | ----------------- |
| `PANEL_DOMAIN`               | `panel.example.com` | Panel 和订阅域名  |
| `ACME_EMAIL`                 | `admin@example.com` | Caddy 申请证书    |
| `US_IP`                      | `203.0.113.10`      | 美国服务器公网 IP |
| `KR1_IP`、`KR2_IP`、`KR3_IP` | `198.51.100.11` 等  | 韩国节点公网 IP   |

:::caution

建议在非 443 端口部署美国 VPN inbound，例如 `8443`。美国服务器的 `80/443` 交给 Caddy 为 Panel 申请证书。

如果旧客户端强制使用美国服务器的 `443` 作为 VPN 端口，不要直接切换。必须先确认旧入站协议参数，再选择端口复用、SNI 分流或保留旧节点过渡。

:::

:::note

Compose 中的镜像固定在明确版本（`remnawave/backend:3.4.4`、`remnawave/node:3.4.1`、`postgres:18.4`、`valkey/valkey:9-alpine`、`caddy:2.9`），避免迁移期间上游发新版导致行为漂移。升级时先改 tag 再 `docker compose pull && docker compose up -d`，并先在非生产环境验证。

:::

## 2. 部署美国 Panel

### 2.1 上传部署文件

在美国服务器创建目录并上传 `deploy/heixincloud/panel/` 中的三个文件：

```bash
sudo mkdir -p /opt/heixincloud/panel
cd /opt/heixincloud/panel
cp .env.example .env
```

文件应为：

```text
/opt/heixincloud/panel/.env
/opt/heixincloud/panel/docker-compose.yml
/opt/heixincloud/panel/Caddyfile
```

### 2.2 生成密钥

```bash
cd /opt/heixincloud/panel
sed -i "s/^APP_SECRET=.*/APP_SECRET=$(openssl rand -hex 64)/" .env
sed -i "s/^METRICS_PASS=.*/METRICS_PASS=$(openssl rand -hex 64)/" .env
pw=$(openssl rand -hex 24)
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$pw/" .env
sed -i "s|postgres:change_me@|postgres:$pw@|" .env
```

编辑 `.env`，至少修改：

```dotenv
PANEL_DOMAIN=panel.example.com
ACME_EMAIL=admin@example.com
FRONT_END_DOMAIN=panel.example.com
SUB_PUBLIC_DOMAIN=panel.example.com/api/sub
```

### 2.3 启动并检查

```bash
cd /opt/heixincloud/panel
docker compose up -d
docker compose ps
docker compose logs -f --tail=100 caddy remnawave
```

确认 `https://panel.example.com` 可以打开登录页后再继续。

### 2.4 导入响应规则

进入 Panel 的订阅响应规则设置，导入或粘贴 `deploy/heixincloud/response-rules.json`。

最后一条必须是空条件且 `responseType` 为 `XRAY_BASE64` 的规则。Remnawave 按顺序匹配，第一条命中即返回；没有规则命中时会返回 `403`。

## 3. 创建节点

在 Panel 中进入 `Nodes -> Management`，依次创建四个节点：

| 节点   | Address                | Node Port | 建议名称 |
| ------ | ---------------------- | --------- | -------- |
| 美国   | `host.docker.internal` | `2222`    | `US-01`  |
| 韩国 1 | `KR1_IP`               | `2222`    | `KR-01`  |
| 韩国 2 | `KR2_IP`               | `2222`    | `KR-02`  |
| 韩国 3 | `KR3_IP`               | `2222`    | `KR-03`  |

每个节点创建时会生成独立的 `SECRET_KEY`。不要把节点 A 的密钥用到节点 B。

美国同机节点的 `host.docker.internal:2222` 依赖 Panel Compose 中的 `extra_hosts`。如果该地址不通，改为美国服务器公网 IP，或在服务器上执行 `ip route`、`docker network inspect remnawave-network` 后确认 Panel 到宿主的可达地址。

## 4. 部署韩国 Node

在每台韩国服务器执行：

```bash
sudo mkdir -p /opt/heixincloud/node
cd /opt/heixincloud/node
```

上传 `deploy/heixincloud/node/docker-compose.yml` 和 `.env.example`，然后：

```bash
cp .env.example .env
sed -i 's/^SECRET_KEY=.*/SECRET_KEY=替换为此节点在Panel生成的密钥/' .env
docker compose up -d
docker compose logs -f --tail=100
```

三台韩国服务器使用同一份模板，但 `.env` 中的 `SECRET_KEY` 各不相同。

## 5. 部署美国 Node

在美国服务器另建 Node 目录：

```bash
sudo mkdir -p /opt/heixincloud/node
cd /opt/heixincloud/node
```

上传同一份 `node/docker-compose.yml` 和 `.env.example`：

```bash
cp .env.example .env
sed -i 's/^SECRET_KEY=.*/SECRET_KEY=替换为美国节点在Panel生成的密钥/' .env
docker compose up -d
docker compose logs -f --tail=100
```

美国 Node 与 Caddy 会同时监听端口，因此必须在 Panel inbound 或 Host 配置中错开端口。

## 6. 防火墙

### 美国服务器

| 端口                         | 来源               | 用途                         |
| ---------------------------- | ------------------ | ---------------------------- |
| `22`                         | 管理员 IP          | SSH                          |
| `80`、`443`                  | 公网               | Caddy、Panel、订阅           |
| `2222`                       | Docker bridge/本机 | Panel 访问同机 Node 管理 API |
| 客户端 VPN 端口，例如 `8443` | 公网               | 美国 VPN 节点                |

先查看 Compose 网段：

```bash
docker network inspect remnawave-network -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
```

假设返回 `172.18.0.0/16`，使用 UFW 时执行：

```bash
sudo ufw allow from 172.18.0.0/16 to any port 2222 proto tcp
sudo ufw allow 80,443/tcp
sudo ufw allow 8443/tcp
sudo ufw enable
```

不要将 `2222` 开放给全网。

### 韩国服务器

假设美国 Panel 公网 IP 是 `203.0.113.10`：

```bash
sudo ufw allow from 203.0.113.10 to any port 2222 proto tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

`443` 仅为示例客户端 VPN 端口，实际以 Panel inbound 配置为准。

## 7. DNS

至少添加：

```text
panel.example.com  A  203.0.113.10
```

确认 DNS 生效后再启动 Caddy，否则证书申请会失败：

```bash
dig +short panel.example.com
```

## 8. 迁移旧数据

部署成功并不等于切换完成。旧订阅还能继续用的关键，是保留旧 `token`、VLESS UUID、旧端点地址和旧入站协议参数。请继续执行[迁移教程](/heixincloud/migration)。

## 9. 上线检查

- 四个节点在 Panel 中都是在线状态。
- 韩国的 `2222/tcp` 只允许美国 Panel IP。
- 美国 `2222/tcp` 不对公网开放。
- 导入的响应规则最后有空条件 Base64 兜底。
- 新订阅分别用 Xray、Clash、Sing-box 客户端实测。
- 旧订阅路径 `/sub/clash/{token}`、`/sub/singbox/{token}`、`/sub/base64/{token}` 均能返回配置。
- 旧用户流量、到期时间、状态和套餐分组已核对。
