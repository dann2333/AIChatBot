# AIBot

AIBot 是一个基于 `Telegram Bot API` 与 OpenAI 兼容 `/chat/completions` 接口的 AI 对话机器人，定位是在聊天里随手用一下的轻量助手。

它支持流式回复、上下文与滚动摘要、长期记忆、图片与文件传入、网络搜索、网页抓取、**带持久目录的沙箱 Shell**、群组白名单、内联模式与访客模式，以及 `/config` 配置面板与配置热重载。

> AIBot 本身不提供模型、API Key 或 Telegram Bot Token，这些都需要自行准备与配置。


## 关于数据安全

所有数据都保存在运行目录（Docker 下为 `/data`）：

- `config.yaml`：配置
- `Chat.db`：SQLite 数据库，保存对话历史、滚动摘要与长期记忆
- `workspace/`：沙箱持久目录
- `Bot.log`：日志（可通过 `Other.Log` 关闭）

机器人会连接以下服务：

- Telegram 服务器
- 你在 `AI.Vendor` 中配置的模型服务
- 开启网页搜索时的 DuckDuckGo，以及可选的 Tavily
- 开启链接请求时，模型指定的网页地址
- 沙箱允许联网时，模型在沙箱中访问的地址

图片和文件会下载后发送给模型服务，请只向你信任的模型提供商发送敏感内容。

`Bot.Token`、`Bot.ApiHash`、`AI.Vendor.*.Key`、`Other.Password` 等敏感配置不会通过 `/get` 或 `/config` 显示，但仍应妥善保存 `config.yaml`，不要提交到公开仓库。


## 部署

### Docker（推荐）

仓库推送到 `main` 或打 `v*` 标签后，GitHub Actions 会自动构建 `linux/amd64` 与 `linux/arm64` 镜像并推送到 `ghcr.io/<用户名>/aichatbot`。

```bash
mkdir aibot && cd aibot
curl -O https://raw.githubusercontent.com/dann2333/AIChatBot/main/docker-compose.yml
docker compose up -d          # 首次启动会在 ./data 生成 config.yaml 模板后退出
vim data/config.yaml          # 填写必要配置
docker compose up -d
docker compose logs -f
```

- 拉取镜像提示无权限时，在 GitHub 仓库的 Packages 页面把包设为 Public，或先 `docker login ghcr.io`。
- 想自己构建：`docker compose up -d --build`。Docker Hub 访问慢时可以加 `--build-arg BASE_IMAGE=<镜像源>/python:3.12-slim`。
- 需要更多沙箱工具时可以追加系统包：`--build-arg EXTRA_PACKAGES="nodejs ffmpeg"`。
- `docker-compose.yml` 中的 `security_opt` 是 bubblewrap 沙箱在容器内运行所必需的，三项缺一不可；删掉后 Bot 仍能运行，只是 Shell 工具会被禁用。
- 在 Docker 中 `/stop` 相当于重启（`restart: unless-stopped`）；要彻底停止请使用 `docker compose stop`。

### 手动部署

需要 Python 3.10 及以上（curl-cffi 0.15 起才支持 AsyncSession 的重试）和 Linux（沙箱依赖 bubblewrap）。

```bash
apt install git python3-pip bubblewrap -y
git clone https://github.com/dann2333/AIChatBot && cd AIChatBot
pip install -r requirements.txt      # 非虚拟环境可能需要 --break-system-packages
python3 main.py                      # 首次运行会生成 config.yaml 模板
```

持久化运行可参考 `screen`、`pm2`、`systemd` 等方式。没有安装 bubblewrap 时，其余功能照常可用，只是 Shell 工具会被禁用。


## 配置说明

> 建议先参看 `readme.config.yaml` 内的逐项注释，也可以使用 `example.config.yaml` 的最简配置。启动时 Bot 会按默认值补全可选项。

### 必要配置

- `SuAdmin`：超级管理员的 Telegram 用户 ID 列表。
- `Bot.ApiId` 与 `Bot.ApiHash`：在 <https://my.telegram.org/apps> 创建应用后获取。
- `Bot.Token`：在 [@BotFather](https://t.me/BotFather) 创建 Bot 后获取。
- `AI.Vendor` 与 `AI.DefaultModel`：至少配置一个模型提供商，并把其中某个模型填入 `AI.DefaultModel`。模型服务需要支持 OpenAI 兼容的 `/chat/completions` 接口与流式响应。

```yaml
SuAdmin:
- 123456789
Bot:
  ApiId: 你的ApiId
  ApiHash: 你的ApiHash
  Token: 你的BotToken
AI:
  Vendor:
    MyVendor:
      BaseUrl: https://example.com/v1
      Key: sk-xxxxxxxx
      Model:
      - gpt-5.5
  DefaultModel: gpt-5.5
```

### 模型提供商

`BaseUrl` 填写 API 根地址，程序会自动拼接 `/chat/completions`。不同提供商中有同名模型时，可以给模型名加任意前缀来区分，实际请求时前缀会自动去除：

```yaml
AI:
  Vendor:
    VendorA:
      BaseUrl: https://vendor-a.example/v1
      Key: sk-a
      Model:
      - VendorA:gpt-5.5
    VendorB:
      BaseUrl: https://vendor-b.example/v1
      Key: sk-b
      Model:
      - VendorB:gpt-5.5
  DefaultModel: VendorA:gpt-5.5
  FallbackModel:
  - VendorB:gpt-5.5     # 请求失败时按顺序切换，总重试次数由 Network.Retry 决定
```

### 常用可选配置

| 配置项 | 说明 |
| --- | --- |
| `Bot.CreateSession` | 是否保存 Pyrogram Session 文件（可加快启动），默认开启。 |
| `Bot.Proxy` | Bot 连接 Telegram 的 Pyrogram 代理配置，类型为 `dict`。 |
| `Bot.HideCommand` | 是否清空 Telegram 命令面板。 |
| `Network.Proxy` | 模型请求、网页搜索和网页抓取使用的代理列表，会轮询使用。 |
| `Network.Timeout` / `Retry` | 外部请求的超时（流式请求为连接超时，另有 120 秒空闲超时）与重试次数。 |
| `AI.SystemPrompt` | 全局系统提示词。 |
| `AI.ExtraSystemPrompt` | 每次请求附加的动态提示词，可使用 `{cur_date}`、`{cur_time}`、`{cur_datetime}`、`{timezone}`、`{model_name}`、`{assistant_name}`。 |
| `AI.ReasoningEffort` | 默认推理预算：`auto`、`none`、`minimal`、`low`、`medium`、`high`、`xhigh`。 |
| `AI.MaxContext` | 每次请求最多带上的历史消息数。 |
| `AI.Summary` | 滚动摘要，见下文。 |
| `AI.Memory` | 长期记忆与历史检索，见下文。 |
| `AI.Tool.Shell` | 沙箱 Shell，见下文。 |
| `AI.MaxFileSize` | 单个图片或文件的最大体积（MB）。 |
| `Other.MaxTextLength` | 单条消息展示的最大长度，超出后显示翻页按钮。 |
| `Other.AvoidJoinGroups` | 防拉群：`0` 关闭、`1` 仅管理员可拉群、`2` 仅超管可拉群。 |
| `Other.HideAds` | 隐藏 `/help` 末尾的项目信息。 |
| `Other.Password` | `/stop` 的关机密码。 |

### 配置热重载

Bot 每 3 秒检查一次 `config.yaml`，文件被修改后会自动重载，无需 `/reload` 或重启。新配置有语法或类型错误时会保留旧配置，并把错误发给超管。`/set` 与 `/config` 的修改会立即生效并写回文件。

### 版本更新介绍

启动时如果发现 `Other.CurrentVersion` 比程序版本旧，会把新版本的更新内容随启动通知一起发给超管，然后自动更新该值。


## 沙箱 Shell

开启 `AI.Tool.Shell.Enable`（默认开启）并安装 bubblewrap 后，模型可以调用 `shell`、`write_file`、`send_file` 工具，在一个轻量的 Linux 沙箱中执行命令：

| 沙箱内路径 | 宿主机路径 | 说明 |
| --- | --- | --- |
| `/workspace/chat` | `workspace/chats/<聊天ID>` | 工作目录，本聊天所有人共享 |
| `/workspace/user` | `workspace/users/<用户ID>` | `HOME`，当前用户私有，跨聊天共享；`pip install --user` 会装到这里 |
| `/workspace/chat/uploads` | | 用户发送的图片和文件会自动保存到这里 |
| `/tmp` | | 临时目录，每次命令结束后清空 |

- 宿主机的 `/usr` 等系统目录以只读方式挂载，所以系统里装了什么（python3、curl、git……）沙箱里就能用什么；Bot 的代码、配置、数据库和其他目录在沙箱中都不可见。
- 每条命令都在独立的命名空间中运行，结束或超时（`AI.Tool.Shell.Timeout`，默认 60 秒）后，沙箱内的所有进程都会被清理。
- `AI.Tool.Shell.Network` 控制沙箱内能否联网（默认允许）；`MaxOutput` 控制返回给模型的输出长度；`WorkDir` 是持久目录的根路径。
- 所有能与 Bot 对话的人（包括白名单群成员）都能间接使用 Shell。沙箱只隔离文件系统与进程，不限制磁盘与 CPU 用量，请据此决定是否开启以及是否允许联网。
- `send_file` 只在普通聊天中可用；内联模式和访客模式无法直接发送文件。


## 记忆

整个设计以轻量为目标，全部基于 SQLite（单表 + WAL + FTS5），不依赖向量库。

- **上下文**：每个聊天保存最近的对话，请求时最多带上 `AI.MaxContext` 条。私聊可用 `/context` 关闭记录，群聊始终记录。图片和文件不再以 base64 存进数据库，历史中只保留占位说明与保存路径。
- **滚动摘要**（`AI.Summary`）：未摘要的消息超过 `MaxMessage` 条时，会在后台把较早的一半压缩进摘要，摘要随系统提示词发送，因此长对话不会被硬截断。`Model` 可以指定一个便宜的模型，留空时使用对话模型。
- **长期记忆**（`AI.Memory`）：模型可以用 `memory_save` / `memory_delete` 记住用户偏好等稳定信息（分为"用户"和"聊天"两个范围），之后每次对话自动注入。用户可以用 `/memory` 查看或清除。
- **历史检索**：模型可以用 `memory_search` 全文检索本聊天中已经滚出上下文的旧消息（中文按三字切分检索）。每个会话最多保留 `AI.Memory.MaxHistory` 条历史。

从旧版本升级时，旧的"每个聊天一张表"的 `Chat.db` 会在启动时自动迁移。


## 工具调用

开启 `AI.Tool.Enable` 后，模型可调用：

| 工具 | 开关 | 说明 |
| --- | --- | --- |
| `web_search` | `AI.Tool.WebSearch.Enable` | 优先 DuckDuckGo，无结果时使用 Tavily（需配置 `ApiKey`） |
| `web_fetch` | `AI.Tool.FetchURL.Enable` | 抓取网页正文 |
| `shell` / `write_file` / `send_file` | `AI.Tool.Shell.Enable` | 沙箱，见上文 |
| `memory_save` / `memory_delete` / `memory_search` | `AI.Memory.Enable` | 记忆，见上文 |

`AI.Tool.MaxToolCall` 限制一次对话的最大工具调用轮数，达到上限后模型必须直接作答。

`AI.FileSupport` 开启后，文档还会以 `file` 类型直接发给模型（需要模型服务支持）；不开启时，只要沙箱可用，模型也能在沙箱中读取用户上传的文件。


## 自动回复与群组身份识别

`AI.AutoReply` 开启后，管理员或白名单群组可以通过以下方式触发对话：

- 私聊直接发送消息；
- 在消息中提及 `@机器人用户名`；
- 回复 Bot 的消息；
- 群组消息命中 `AI.WakeWord` 中的正则表达式。

`AI.IDRecognition.Enable` 开启后，群聊中发送者名称和正文会按 `AI.IDRecognition.Format` 组合，并附加 `AI.IDRecognition.Prompt`，让模型能区分不同发言者。


## 权限

- **超级管理**：可以授权或取消管理员、管理白名单群组、读写配置、退出群组、关闭 Bot。配置中是 YAML 数组，即使只有一个超管也要保留 `- `。
- **普通管理**：可以对话、切换模型、设置个人系统提示词和推理预算、开关个人上下文、查看模型可用性结果。超管使用 `/grant`、`/ungrant` 管理。
- **白名单群组**：群内所有成员都可以使用基础对话功能（含工具），个人设置类命令仍仅限管理员。
- **游客**：只能使用 `/help`、`/version`、`/stats`、`/start`。


## 指令

#### 公共指令

| 指令 | 说明 |
| --- | --- |
| `/help` | 帮助菜单 |
| `/version` | 版本信息 |
| `/stats` | 权限状态；可对话者还会看到对话数量、模型、工具与沙箱状态 |
| `/start` | 发送 `Other.Intro.Text` |

#### 对话指令（管理员或白名单群组）

| 指令 | 说明 |
| --- | --- |
| `/chat <内容>` / `/c` | 向模型发送消息，可附带图片或文件，也可以回复一条消息提问。生成中可点"停止"（私聊草稿自带停止按钮） |
| `/clear` | 清除当前聊天的上下文与摘要（长期记忆不受影响） |
| `/page <页数>` / `/p` | 回复 Bot 的长消息进行跳页 |
| `/memory` | 查看长期记忆；`/memory clear` 清除自己的记忆，`/memory clear chat` 清除本聊天的记忆（管理员） |

#### 普通管理指令

| 指令 | 说明 |
| --- | --- |
| `/model [模型名]` | 不带参数时显示模型选择面板 |
| `/context` | 开关自己的私聊上下文记录 |
| `/prompt [提示词]` | 设置个人系统提示词，不带参数时清除 |
| `/effort` / `/reason` | 推理预算选择面板 |
| `/prune [正则]` | 普通管理员查看最近一次测试结果；超管会实际测试模型可用性与耗时 |

#### 超级管理指令

| 指令 | 说明 |
| --- | --- |
| `/grant [id...]` / `/ungrant [id...]` | 回复用户或跟 id 来授权 / 取消管理员 |
| `/grantscan` | 清除已注销的管理员 |
| `/trust` / `/distrust` | 把当前群加入 / 移出白名单 |
| `/get <路径>` | 读取配置，如 `/get AI.DefaultModel`；读取顶层用 `.` |
| `/set <路径> <值>` | 修改配置，空格写作 `\s`，列表可写 `[a, b]`，如 `/set AI.SystemPrompt 你是一个\s有帮助的助手` |
| `/config [路径]` | 配置面板：浏览配置项、切换开关、修改值、增删列表元素、恢复默认值。点"修改"或"添加"后直接发送新值即可，发送 `/cancel` 取消 |
| `/reload` | 立即从 `config.yaml` 重载（通常热重载已经足够） |
| `/id` | 显示聊天与用户 ID，回复消息时显示被回复用户的信息 |
| `/leave` | 退出当前群 |
| `/stop <密码>` | 关闭 Bot |


## 特殊模式

### 内联模式

管理员可以在任意聊天输入 `@机器人用户名 内容`，选择结果后 Bot 会在该消息中流式作答。内联模式使用独立的上下文。需要在 [@BotFather](https://t.me/BotFather) 中对 Bot 执行 `/setinline` 和 `/setinlinefeedback`（设为 100%）。

### 访客模式

通过 Telegram 的 guest message 能力，管理员或白名单群组可以在对应入口中对话，并使用 `/clear`、`/stats`、`/memory`、`/model`、`/effort`、`/trust`、`/distrust`、`/id`。


## 注意事项

- `config.yaml` 中的 ID 应填写数字，而不是用户名。
- `AI.DefaultModel` 必须与 `AI.Vendor.*.Model` 中的名称完全一致。
- 网络搜索、网页抓取、图片和文件功能需要模型服务支持相应的 OpenAI 兼容格式。
- 配置有语法或类型错误时会拒绝加载，可以对照 `readme.config.yaml` 修正。
