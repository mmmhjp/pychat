# pychat 通信协议说明

版本：pychat v1.0.0

---

## 1. 传输层

| 项目 | 说明 |
|---|---|
| 协议 | TCP |
| 聊天服务端口 | `9999` |
| 发现服务端口 | `9998`（UDP 广播） |
| 地址族 | IPv4 / IPv6 / 双栈，服务器端可切换 |
| 分帧方式 | 4 字节大端长度前缀（`struct.pack('>I', len)`）+ UTF-8 JSON 负载 |
| 单包上限 | 500 MB（`MAX_BODY_SIZE`） |

### 分帧示例

```
┌──────────────────┬───────────────────────────────┐
│ 长度 (4 字节 BE) │ 负载 (JSON, UTF-8)             │
└──────────────────┴───────────────────────────────┘
```

负载统一为 JSON 对象，`type` 字段标识报文类型。

---

## 2. 服务器发现

### 2.1 客户端广播

客户端向 `255.255.255.255:9998` 发送 UDP 广播：

```
DISCOVER_CHAT_SERVER:v1
```

### 2.2 服务器应答

服务器回送 JSON：

```json
{
  "type": "chat_server",
  "port": 9999,
  "ips": ["192.168.1.10", "fe80::1"]
}
```

> **隐藏模式**下服务器不响应任何扫描请求。

---

## 3. 登录握手

### 3.1 客户端 → 服务器

```json
{
  "type": "login",
  "username": "alice",
  "device_id": "32位十六进制设备标识",
  "mac": "AA:BB:CC:DD:EE:FF",
  "hardware_fingerprint": "32位十六进制硬件指纹",
  "hardware_info": { "mac": "...", "cpu": "...", "disk": "...",
                     "motherboard": "...", "bios_uuid": "..." }
}
```

### 3.2 服务器 → 客户端（成功）

```json
{
  "type": "login_success",
  "device_id": "...",
  "server_hidden": false
}
```

### 3.3 服务器 → 客户端（失败）

```json
{ "type": "error", "reason": "用户名 \"alice\" 已在其他设备登录，请更换用户名" }
```

失败原因包括：用户名重复、用户名非法、用户名超长、命中黑名单、隐藏模式、硬件指纹被封禁、连接数已满。

### 3.4 用户名规则

- 长度 1–30 字符
- 不允许包含 `\n` `\r` `\t` `|` `/` `\` `:` `(` `)`
- 不允许包含控制字符（`ord(c) < 32` 或 `== 127`）
- 同名用户同时只能有一个连接

---

## 4. 业务报文

### 4.1 客户端 → 服务器

| `type` | 载荷 | 说明 |
|---|---|---|
| `heartbeat` | — | 心跳，每 5 秒一次 |
| `public_message` | `content` | 群聊消息，≤5000 字符 |
| `private_message` | `to`, `content` | 私聊消息 |
| `file_request` | `to`, `from`, `filename`, `size`, `transfer_id` | 请求发送文件 |
| `file_response` | `to`, `from`, `transfer_id`, `accepted` | 同意/拒绝接收文件 |
| `file_chunk` | `to`, `from`, `transfer_id`, `chunk_index`, `total_chunks`, `data` | 文件分片（base64） |
| `file_end` | `to`, `from`, `transfer_id`, `total_chunks`, `sha256` | 文件传输结束 |
| `file_error` | `to`, `transfer_id`, `reason` | 传输中止 |
| `shared_file_list` | — | 请求共享文件列表 |
| `download_shared_file` | `filename` | 请求下载共享文件 |
| `upload_shared_file_start` | `filename`, `total_size` | 开始分块上传 |
| `upload_shared_file_chunk` | `filename`, `index`, `data` | 上传分片（base64） |
| `upload_shared_file_end` | `filename`, `total_size`, `sha256` | 上传结束 |
| `upload_shared_file` | `filename`, `data`, `size` | 旧版整包上传（兼容保留） |

### 4.2 服务器 → 客户端

| `type` | 载荷 | 说明 |
|---|---|---|
| `login_success` | `device_id`, `server_hidden` | 登录成功 |
| `heartbeat_ack` | — | 心跳应答 |
| `public_message` | `from`, `content`, `is_admin` | 群聊消息 |
| `system_message` | `content`, `is_broadcast`, `user_list?` | 系统消息，`user_list` 存在时需刷新用户列表 |
| `private_message` | `from`, `content` | 私聊消息 |
| `message_history` | `messages[]` | 历史消息（最近 100 条） |
| `kicked` | `reason` | 被踢出 |
| `muted` | `duration`, `reason` | 被禁言 |
| `unmuted` | `reason` | 被解除禁言 |
| `error` | `reason` | 通用错误 |
| `shared_file_list` | `files[]` | 共享文件列表（`{name, size}`） |
| `shared_file_start` | `filename`, `total_size`, `total_chunks`, `sha256` | 开始下载共享文件 |
| `shared_file_chunk` | `filename`, `index`, `data` | 下载分片 |
| `shared_file_end` | `filename`, `total_size`, `total_chunks`, `sha256` | 下载结束 |
| `upload_accepted` | `filename` | 服务端已接受上传 |
| `shared_file_data` | `filename`, `data`, `size` | 旧版整包下载（兼容保留） |

---

## 5. 共享文件分块传输

采用 `start → chunk → end` 三阶段流式传输，避免整文件读入内存。

```
客户端                                    服务器
  │                                         │
  │  upload_shared_file_start               │
  │  {filename, total_size}  ─────────────► │
  │                                         │  写 .part 临时文件
  │  ◄────────── upload_accepted            │
  │                                         │
  │  upload_shared_file_chunk (index=0)     │
  │  ─────────────────────────────────────► │  写盘 + 累积 SHA-256
  │  upload_shared_file_chunk (index=1)     │
  │  ─────────────────────────────────────► │
  │          ...                            │
  │                                         │
  │  upload_shared_file_end                 │
  │  {filename, total_size, sha256} ──────► │  校验大小与哈希
  │                                         │  os.replace 原子改名
```

下载方向对称：服务器 `shared_file_start` → 若干 `shared_file_chunk` → `shared_file_end`。

### 关键约束

- **分块大小**：256 KB（`FILE_CHUNK_SIZE`，两侧必须一致）
- **单文件上限**：512 MB（`MAX_SHARED_FILE_SIZE`）
- **分块序号必须严格递增**，乱序会被接收端判定为损坏并中止
- **大小校验**：实际字节数必须等于声明值
- **哈希校验**：SHA-256 必须匹配，否则删除文件并报错
- **临时文件**：上传期间写入 `.upload_<用户>_<时间戳>.part`，成功后才改名；断开时自动清理

---

## 6. 点对点文件传输

与共享文件不同，走「请求 → 确认 → 分片 → 结束」：

```
发送方                                   接收方
  │  file_request                         │
  │  {to, from, filename, size, tid} ──► │  弹窗询问是否接收
  │                                        │  选择保存路径
  │  ◄───── file_response {accepted}      │
  │                                        │
  │  file_chunk (chunk_index=0..N) ──────► │  按序写盘 + 累积哈希
  │        ...                             │
  │  file_end {total_chunks, sha256} ───► │  校验分片数与哈希
  │                                        │  失败则删文件
  │  ◄───── file_error (任一方出错时)      │
```

传输 ID（`transfer_id`）格式：`{发送方}_{接收方}_{时间戳}`。

---

## 7. 心跳与断线

| 项目 | 值 |
|---|---|
| 客户端心跳间隔 | 5 秒 |
| 服务端超时判定 | 30 秒无心跳即断开并清理 |
| 服务端检测周期 | 10 秒 |
| 客户端重连策略 | 指数退避 `2 × 2^(n-1)` 秒，上限 60 秒，最多 10 次 |

重连期间客户端不弹出错误对话框，只在聊天区提示。

---

## 8. 扩展协议

新增报文类型只需三步：

1. **客户端 → 服务器**：在 `Server._process_message` 中增加 `elif mtype == 'your_type':` 分支
2. **服务器 → 客户端**：在 `Client.handle_message` 中增加同名分支
3. 若涉及文件传输，记得在服务端把 `file_error` 加入转发白名单

接收端对非 dict 报文、0 长度报文、超长报文一律安全丢弃，不影响连接。

---

## 9. 端口与常量一览

| 常量 | 值 | 位置 |
|---|---|---|
| `PORT` | 9999 | 两侧 |
| `DISCOVERY_PORT` | 9998 | 两侧 |
| `MAX_BODY_SIZE` | 500 MB | 两侧 |
| `FILE_CHUNK_SIZE` | 256 KB | 两侧 |
| `MAX_SHARED_FILE_SIZE` | 512 MB | 两侧 |
| `MAX_MESSAGE_LENGTH` | 5000 | 两侧 |
| `HEARTBEAT_TIMEOUT` | 30 秒 | 服务器 |
| `HEARTBEAT_INTERVAL` | 5 秒 | 客户端 |
| `MAX_CONNECTIONS` | 100 | 服务器 |
| `MAX_SHARED_FILES` | 20 | 服务器 |
| `MAX_USERNAME_LENGTH` | 30 | 两侧 |
| `MAX_PRIVATE_LOG_BYTES` | 5 MB | 服务器 |
