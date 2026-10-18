# pychat 运行时数据目录

程序运行时会在**工作目录**（即你启动程序时所在的目录）生成配置文件与数据：

| 文件 | 由谁生成 | 说明 |
|---|---|---|
| `blacklist.txt` | 服务器 | IP 黑名单，每行一个 IP |
| `device_blacklist.txt` | 服务器 | MAC / 设备 ID 黑名单，格式 `标识\|原因` |
| `hardware_blacklist.txt` | 服务器 | 硬件指纹黑名单，每行一个指纹 |
| `mute_list.txt` | 服务器 | 禁言列表，格式 `用户名\|解除时间戳` |
| `server_hidden.txt` | 服务器 | 隐藏模式状态，`0`=公开 `1`=隐藏 |
| `private_chat_log.txt` | 服务器 | 私聊记录，超过 5MB 自动轮转为 `.bak` |
| `client_device_id.txt` | 客户端 | 本机设备标识（首次运行生成） |
| `message_draft.txt` | 客户端 | 未发送的输入草稿 |
| `shared_files/` | 服务器 | 共享文件目录，最多 20 个文件 |

## 说明

- 这些文件已被 `.gitignore` 排除，**不会**提交到仓库
- 如果希望把程序与数据分开存放，可以把本目录作为工作目录：
  ```bash
  cd data && python ../src/服务器.py
  ```
- `shared_files/` 目录会在服务器首次启动时自动创建
