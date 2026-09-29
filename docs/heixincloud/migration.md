---
sidebar_position: 2
title: 旧数据迁移
---

# 旧数据迁移

迁移工具位于 `tools/heixin-migrate/migrate.py`。它默认只读；只有传入 `--apply` 才会调用 Remnawave API。

## 迁移原则

迁移器保留：

- 旧 `users.token`：作为 Remnawave `shortUuid`
- 旧 `managed_clients.client_uuid`：作为 VLESS UUID
- 到期时间、流量限额、状态、历史流量
- 旧套餐到 Internal Squad 的映射

旧订阅路径由 Panel 前面的 Caddy 兼容：

| 旧路径                 | 新路径                     |
| ---------------------- | -------------------------- |
| `/sub/clash/{token}`   | `/api/sub/{token}/clash`   |
| `/sub/base64/{token}`  | `/api/sub/{token}`         |
| `/sub/singbox/{token}` | `/api/sub/{token}/singbox` |

已对照 Remnawave 3.4.4 源码确认：

- `shortUuid` 是自由字符串，不做 UUID 校验，旧 `token` 可直接沿用。
- 创建接口允许 `ACTIVE` / `DISABLED` / `LIMITED` / `EXPIRED`；更新接口只允许 `ACTIVE` / `DISABLED`，所以过期和超限状态只在首次创建时写入。
- `trafficLimitStrategy: NO_RESET` 仍然有效，`trafficLimitBytes = 0` 表示不限流量。
- API Token 是签名后的 JWT，通过 `Authorization: Bearer` 传递，签发密钥是 `APP_SECRET`。**重新生成 `APP_SECRET` 会让所有已签发的 API Token 失效。**

:::caution

订阅设置里的 `disableSubscriptionAccessByPath` 必须保持关闭（默认即关闭）。开启后 `/api/sub/{token}/clash` 这类按路径指定格式的请求会直接返回 `403`，旧订阅路径兼容也会一起失效。

:::

## 1. 备份旧 SQLite

不要直接复制正在运行的 WAL 数据库。使用 SQLite 官方备份命令生成一致性只读快照：

```bash
sqlite3 /path/to/app.db ".backup '/opt/heixin-migrate/app.db'"
sqlite3 /opt/heixin-migrate/app.db "PRAGMA quick_check;"
```

将 `/opt/heixin-migrate/app.db` 上传到美国 Panel 服务器，并只给迁移命令读取权限。

## 2. 创建 API Token

在 Remnawave Panel 中创建 API Token。迁移器使用：

```text
Authorization: Bearer <token>
```

不要把 Token 写入仓库。

## 3. 准备 Squad 映射

先在 Panel 创建 Internal Squad，再映射旧 `plans.allowed_tags` 对应的套餐。

支持两种方式：

```bash
--plan-squad 10=<SQUAD_UUID>
--plan-squad 20=<SQUAD_UUID>
--internal-squad <DEFAULT_SQUAD_UUID>
```

- `--plan-squad` 优先：按旧 `plan_id` 指定 Squad，可重复。
- `--internal-squad` 兜底：没有套餐映射时使用。

## 4. 先执行 Dry-run

进入仓库：

```bash
cd /opt/heixincloud/panel-src
export REMNAWAVE_API_TOKEN='替换为Panel API Token'
python3 tools/heixin-migrate/migrate.py \
  --db /opt/heixin-migrate/app.db \
  --api-url https://panel.example.com \
  --plan-squad 10=00000000-0000-0000-0000-000000000010 \
  --internal-squad 00000000-0000-0000-0000-000000000099
```

Dry-run 会报告：

```text
legacy_users=... create=... update=... conflicts=... notices=...
```

存在 `CONFLICT` 时必须先处理。重点关注：

- 用户名冲突
- `shortUuid` 已被其他用户占用
- VLESS UUID 重复
- Panel 中已有用户状态无法转换为 `EXPIRED` 或 `LIMITED`

## 5. 执行写入

Dry-run 无冲突后执行：

```bash
export REMNAWAVE_API_TOKEN='替换为Panel API Token'
python3 tools/heixin-migrate/migrate.py \
  --db /opt/heixin-migrate/app.db \
  --api-url https://panel.example.com \
  --plan-squad 10=00000000-0000-0000-0000-000000000010 \
  --internal-squad 00000000-0000-0000-0000-000000000099 \
  --apply \
  --state /opt/heixin-migrate/state.json \
  --traffic-sql /opt/heixin-migrate/traffic.sql
```

`--state` 用于幂等追踪，`--traffic-sql` 用于导入历史流量。

## 6. 导入历史流量

先检查生成的 SQL：

```bash
less /opt/heixin-migrate/traffic.sql
```

生成的 SQL 会在执行时用 `information_schema.columns` 自动判断 `user_traffic` 的主键列名
（Remnawave 3.4.4 起是 `id`，旧版本是 `t_id`），不需要手工改列名。确认内容无误后执行：

```bash
cd /opt/heixincloud/panel
docker compose exec -T remnawave-db \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < /opt/heixin-migrate/traffic.sql
```

如果两个列名都不存在，脚本会直接报错并回滚，不会写入半截数据。需要人工排查时执行：

```bash
docker compose exec remnawave-db \
  psql -U postgres -d postgres \
  -c '\d public.user_traffic'
```

## 7. 无感切换

真正无感切换的重点不是用户记录，而是客户端仍然能连接到兼容的旧入站。

在旧系统切换前：

1. 保留旧 Panel 和旧节点，不删除旧数据库。
2. 在 Remnawave 中创建与旧协议一致的 Config Profile、Inbound 和 Host。
3. 复用旧 VLESS UUID、Reality 公私钥、short ID、SNI、端口和端点地址。
4. 对美国服务器 443 端口冲突提前设计分流方案。
5. 用小批量用户先验证。

切换时：

1. 暂停旧系统的用户写入。
2. 重新执行一次迁移命令，刷新状态和到期时间。
3. 重新生成并执行历史流量 SQL。
4. 将 DNS 或节点地址切换到 Remnawave。
5. 观察 Panel 节点在线状态、订阅请求和客户端连接。

如果旧客户端在原地址、原端口、原协议参数下继续访问，并且 VLESS UUID 和订阅 token 都保留，客户端通常不需要重新导入订阅。

## 8. 回滚

保留以下内容直到新系统稳定：

- 旧 `app.db` 备份
- 旧 Panel 容器/服务
- 旧节点配置和入站密钥
- 迁移状态文件
- 历史流量 SQL

回滚时先切回旧 DNS 或旧节点，再恢复旧服务。不要在新系统验证前删除旧数据。

## 9. 验证清单

- 迁移用户数与旧系统一致
- 抽检用户的 `shortUuid` 等于旧 token
- 抽检用户的 VLESS UUID 与旧配置一致
- 流量、到期时间、状态正确
- 旧套餐对应的 Internal Squad 正确
- 历史流量 SQL 已执行
- `/sub/clash/`、`/sub/singbox/`、`/sub/base64/` 旧路径可用
- 新旧订阅同时返回可用配置
