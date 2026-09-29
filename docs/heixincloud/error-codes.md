---
sidebar_position: 3
title: 错误码规范
---

# 黑心云错误码规范

黑心云在 Remnawave 上游的 `ERRORS` 注册表基础上，加了一层统一的错误码 / 日志码规范，用于把分散在 Panel 后端和各个 Node 节点上的报错、警告、提示收敛成可检索、可对号入座的编号。

## 格式

```text
HX-<PROC>-<SEV>-<NNNNNN>
```

| 段位     | 取值                          | 含义                              |
| -------- | ----------------------------- | --------------------------------- |
| `HX`     | 固定                          | 黑心云前缀，便于全局检索          |
| `PROC`   | `P` / `N`                     | `P` = Panel 后端，`N` = Node 节点 |
| `SEV`    | `ERR` / `WARN` / `INFO`       | 错误 / 警告 / 提示                |
| `NNNNNN` | 六位数字                      | 序号，同一 `PROC` + `SEV` 内唯一  |

示例：

```text
HX-P-ERR-000001    Panel 内部错误
HX-P-ERR-000010    Panel 启用节点失败
HX-N-ERR-000017    Node Xray 启动失败
HX-P-WARN-000001   Panel 警告
HX-N-INFO-000001   Node 提示
```

`HX-*-ERR-000000` 保留为「未分类」，异常过滤器遇到无法映射的错误时会回退到它。

`HX-*-ERR-990001` ~ `990004` 是框架级兜底码，对应上游异常过滤器的 `E000` / `E401` / `E403` / `E500`。`990000` 段整体保留给基础设施错误，不参与顺序编号。

## 在哪里出现

错误码会同时出现在两个地方：

1. **运行日志**。异常过滤器输出的每条错误日志都带 `hxCode` 字段，可以直接 `grep HX-P-ERR-` 或 `grep HX-N-ERR-` 定位。

   ```text
   ERROR [HttpExceptionFilter] Failed to get system stats - { code: 'A010', hxCode: 'HX-N-ERR-000008', path: '/node/stats/get-system-stats' }
   ```

2. **API 错误响应**。错误响应体在原有 `errorCode` 之外新增 `hxCode` 字段：

   ```json
   {
     "timestamp": "2026-09-29T00:00:00.000Z",
     "path": "/api/nodes",
     "message": "Enable node error",
     "errorCode": "A010",
     "hxCode": "HX-P-ERR-000010"
   }
   ```

原有的 `errorCode`（`A###` / `N###`）保持原样，第三方对接不受影响；`hxCode` 是新增字段，可按需忽略。

## 与上游错误码的关系

`HX_ERROR_CODES` 与 `ERRORS` 的语义键一一对应，编号独立于上游的 `A###` / `N###` 数字。

之所以不直接复用上游数字，是因为上游存在**同号不同义**：

| 上游码 | 冲突条目                                                                 |
| ------ | ------------------------------------------------------------------------ |
| `A089` | `BULK_UPDATE_ALL_USERS_ERROR` / `INVALID_USER_STATUS_ERROR`              |
| `A199` | `DELETE_PASSKEY_ERROR` / `VALIDATE_REMNAWAVE_SETTINGS_ERROR`             |
| `A219` | `CONNECTED_NODES_NOT_FOUND` / `GET_ALL_NODE_PLUGINS_ERROR`               |

黑心云 fork 已把后一个条目重新编号为 `A259` / `A260` / `A261`，让 `errorCode` 本身也不再撞车。

## 新增错误 / 警告 / 提示

- 新增 API 错误：在 `ERRORS` 里加语义键和条目，然后在 `hx-codes.ts` 的 `HX_ERROR_CODES` 末尾追加下一个序号。`satisfies Record<keyof typeof ERRORS, string>` 会在编译期强制两边对齐，漏加会直接编译失败。
- 新增警告 / 提示：在 `HX_WARN_CODES` / `HX_INFO_CODES` 里追加下一个序号，并在日志调用处带上。
- 已发布的码**永不复用、永不改号**，只能在末尾追加。

码表位置：

- Panel 后端：`libs/contract/constants/errors/hx-codes.ts`
- Node 节点：`libs/contract/constants/errors/hx-codes.ts`

## 相关文件

- `libs/contract/constants/errors/errors.ts`：上游语义错误注册表
- `libs/contract/constants/errors/hx-codes.ts`：黑心云统一码表与工具函数
- `src/common/exception/*.filter.ts`：异常过滤器，负责把码写进日志和响应
