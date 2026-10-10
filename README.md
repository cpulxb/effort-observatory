# 档位观测台

一个仅在本机运行的 Codex App / Codex CLI 请求核验网页，核验 Request 与 Response 的思考等级是否一致。

## 启动

运行时只需要 Python 3.10 或更新版本，无第三方 Python 依赖。

```bash
cd /root/Project/effort-observatory
python3 server.py --port 8766
```

浏览器打开 <http://localhost:8766>。Windows 浏览器可通过 WSL localhost 转发访问。

当前部署使用已启用的用户级持久 systemd 服务：

```bash
systemctl --user status effort-observatory
systemctl --user restart effort-observatory
systemctl --user stop effort-observatory
```

服务随 WSL 用户级 systemd 启动，异常退出会自动重启。它不负责启动 WSL 或重启客户端。

## 使用

1. 在首页顶部选择 **Codex App** 或 **Codex CLI**，查看对应客户端的调用日志。
2. CLI 在新终端直接运行 `codex`；App 从系统托盘正常退出后，通过原来的图标重开。查看顶部 **Codex App / Codex CLI** 是否显示「已接入」。已配置但暂时没有对应进程时，会显示「等待新进程」或「等待重启」；只有事件监听器运行、或者磁盘上存在旧日志，不表示当前客户端已经接入。无需修改客户端 `base_url`。
3. 在已接入的客户端发送消息。服务常驻读取客户端写出的调用与响应事件，无需点击开始，也没有 120 秒采集时限。逐行查看并列的 **Request / Response** 模型与档位，以及核验状态、输入/输出用量和会话；请求已发出但响应尚未到达时显示「等待响应」。
4. 点击「查看」，核对两侧证据、调用标识、捕获时间和服务端时间。同一响应的生命周期与重复观测会合并展示。
5. 需要浏览消息、工具或导出时，进入右上角「会话时间线」（`/sessions`）。默认导出仅含核验字段的 JSON；也可选择会话与工具记录，先查看内容范围。会话导出是解析结果，长消息与工具返回保留 16000 字符上限，不是原始日志备份。

首页每页 25 条记录，可搜索模型、档位、会话或 ID，按核验状态和会话筛选，也可切换北京时间与 UTC。顶部统计显示当前客户端、搜索和会话条件下的全部记录，不随状态筛选缩小；表格总数和分页反映完整筛选条件。统计单位是聚合后的观测记录，不等于 API 计费请求数。来源无法确定的记录保留在「客户端待确认」标签，不会默认算入 App 或 CLI。

调用日志主页每 2 秒自动刷新，会话时间线每 10 秒刷新，无变化时使用 ETag 返回 304，并保留展开状态、焦点、文本选择与内部滚动。可暂停页面自动刷新；这不停止事件监听。原有会话时间线保留每页 20 轮的分页，以及按需读取工具内容和原始证据的交互；手机端会话列表和概览默认折叠。

### 客户端事件采集

当前 App 与 CLI 使用原生调用事件和原始响应事件，分别写入观测目录中的 `app/` 与 `cli/`。启动参数仅对新进程生效，因此已经运行的 App 和 CLI 必须重新启动。首页分别显示监听器状态、客户端配置状态与仍在运行的已接入进程：活跃进程显示「已接入 N 个进程」；CLI 已配置但未运行时显示「已配置，等待新进程」，App 则显示「已配置，等待重启」。尚未配置且没有活跃观测进程时才显示「未接入事件采集」。

WSL 中可以使用观测启动器启动 CLI：

```bash
cd /root/Project/effort-observatory
python3 observed_launcher.py --client cli --
```

原本传给 `codex` 的参数放在 `--` 后面，例如 `python3 observed_launcher.py --client cli -- exec "任务内容"`。启动器自动寻找当前已安装的原始 Codex 二进制，也支持 `--binary /绝对路径/codex` 明确指定；它只增加观测环境与日志目录，不修改模型、provider 或 `base_url`。

App 保留原启动方式，原生环境参数与本地日志桥接已配置，等待用户从系统托盘正常退出并通过原来的图标重开。无需使用观测启动脚本；没有设置 `CODEX_CLI_PATH`，不绕过 Windows 原生核心注册。Windows 用户配置 `[analytics] enabled=false` 用于关闭原始 debug 日志的远程遥测。重启后的后端以实际环境参数识别是否接入，配置文件本身不会让页面显示已接入。

CLI 已在 `/root/.bashrc` 和 `/root/.zshrc` 末尾加入 `/root/Project/effort-observatory/launch/bin` 到 PATH。新终端中直接运行 `codex` 即可，原有运行进程不热切换。已验证一次真实 CLI 调用：Request 为 `low`、Response 回显为 `high`，两侧通过调用标识准确配对入库；验证命令退出后，页面会回到「已配置，等待新进程」。

观测数据库与 `raw-events.jsonl` 仅保存模型、档位、标识、状态和用量。原生追踪暂存目录 `data/events/` 在客户端运行期间可能短暂写入完整上下文和工具内容；启动器会裁剪已完成的 payload，正常退出后清洗事件日志及删除临时原始日志。异常退出可能留下暂存内容，该目录仅供本机使用，不应分享原始目录。

Request 来自 `inference_started` 事件引用的真实请求参数。完成、失败和取消事件通过客户端生成的 `inference_call_id` UUID 连接到同一逻辑调用；原始响应生命周期通过服务端 `response_id` 归入该调用。不会根据时间、模型或聊天轮次猜测配对。客户端的内部 HTTP 重试可能共用同一个逻辑调用 ID，因此调用记录不等于所有网络请求包，也不等于计费次数。

原生完成事件本身不含服务端回显的 `reasoning.effort`，只有这类证据时显示「回显未采集」。采集到原始响应、且响应明确没有该字段时，显示「未回显」；不会将缺少采集和服务端未回显混为一谈。客户端事件协议可能随版本变化，HTTP 和 WebSocket 路径需分别验证；代码中存在 WebSocket 支持不代表当前部署已经验证该路径，也不能保证采到每个网络包。

### 字段含义

| 页面字段 | 来源 | 不能据此推断的事情 |
| --- | --- | --- |
| Codex App / Codex CLI 标签 | 捕获来源或可确认的会话客户端元数据 | 无法确认来源时不能猜测客户端 |
| Request | 客户端发送事件引用的请求 JSON 中的 `reasoning.effort`；历史记录保留其来源 | 不代表上游按此执行 |
| Response | 原始响应事件的服务端回显，以及客户端完成事件的状态与用量 | 不直接证明模型内部计算量；完成事件没有回显不代表服务端没有回显 |
| 用量 | 同一响应的用量字段，优先终态观测 | 不能把重复观测或生命周期阶段相加当成总用量 |
| Client（会话时间线） | 本地 `turn_context.effort` | 不代表真实请求正文 |
| HTTP 运行日志 | Codex 日志记录的档位、状态与时间 | HTTP 200 不代表档位生效 |

首页使用常驻事件监听，不再启动内存扫描。历史内存观测和历史取证文件仍可查看，并明确标为历史证据；这类旧记录可能缺少另一侧或调用标识。页面不会用配置值填补缺失证据。原始会话文件被删除时不会虚构工具调用或助手回答。

### 调用日志的聚合与核验

- 首页按调用观测展示，不要求证据已经归属到具体消息；消息归属缺失不影响查看捕获到的 Request 或 Response。
- 共享 `response_id` 的响应生命周期事件和重复观测合并为一条记录。终态优先，重复用量不会累加；互相矛盾的字段保留并标记冲突。
- 请求与响应必须共享 `capture_id` 才按同次调用核验；客户端事件的 `capture_id` 包含客户端、运行标识和 `inference_call_id` UUID。仅有相同会话、轮次或相近时间不足以配对；没有可靠调用标识的证据各自展示。
- 同一次响应在其他采集来源中被再次观察到时，可通过一致的 `response_id` 归入已有调用；会话标识冲突时不会强行并入。
- 两侧档位齐全且已精确配对时，判定为「回显一致」或「回显差异」。单侧缺失、未配对或字段不完整归为「待确认」；矛盾字段和标识冲突纳入差异统计，详情保留冲突原因。调用完成、失败等响应状态单独显示，不等同于档位判定。
- 一个聊天轮次可以触发多次调用，各调用独立核验。不同调用各自一致但档位不同，不计作请求/响应差异。

### 会话时间线的消息归属

- 用 `thread_id + turn_id` 或响应 `output_id` 归属轮次；强标识矛盾时标记冲突，留在未归属区。
- 响应没有 turn_id 时，用 `output_id` 匹配会话中的消息或工具调用条目。
- 仅能匹配 thread_id 的记录放在「未归属」区，不根据相近时间猜测。
- 不同后台会话的档位不参与当前会话的判断。
- 会话列表分别显示最新轮判定及历史曾有差异，统计不受是否点开详情影响。

### 时间

消息与工具调用的时间来自会话日志。捕获时间表示本服务看到数据的时刻，不能当作请求发起时间。完整响应有 `created_at` / `completed_at` 时显示对应服务端时间。证据导出保留原始时间字符串。

## 可选：HTTP 精确配对

在客户端主动接入本地转发器后，每条请求与响应以 `capture_id` 配对。默认不修改任何客户端配置，也不读取鉴权文件。

当前转发器仅支持 AnyRouter 的流式 Responses 接口：

```text
客户端 base_url: http://127.0.0.1:8766/relay/v1
转发上游:        https://anyrouter.top/v1/responses
```

将需要核验的 Codex provider 的 `base_url` 临时改为本地地址，保留原来的认证方式，重启或新建对应连接后测试。结束时恢复原来的 `base_url`。该修改由用户选择，网页不会自动执行。

限制：支持未压缩、`stream: true`、不超过 32 MB 的 JSON 请求。仅保存关键请求字段、响应生命周期、ID 与用量，不保存 Authorization、完整上下文或隐含推理正文。请求正文原字节转发。SSE 捕获超限会单独记录观测诊断，并继续转发原始字节；终态区分 completed、incomplete、failed 与网络异常。

## 代码结构

```text
server.py           HTTP 路由、本机访问限制、静态资源
observatory.py      会话索引、证据存储、精确关联与判定
call_log.py         调用观测聚合、响应生命周期、客户端筛选与分页统计
session_parser.py   轮次、消息、工具调用和工具返回解析
event_capture.py    常驻读取客户端调用与原始响应事件、字段归一化
observed_launcher.py 原始客户端启动器、事件写入与活跃接入进程状态
app_log_bridge.py  原生桌面本地debug日志桥接、片段拼接与最小响应字段
capture.py          旧版内存观测逻辑、历史字段归一化
relay.py            可选 HTTP 转发、SSE 解码、请求响应配对
sanitization.py     结构化字段、自由文本与 URL 的规则脱敏
web/dashboard.html 调用日志首页、App/CLI 标签与并列字段表格
web/dashboard.js   首页状态、筛选、分页、自动刷新与详情请求
web/dashboard-views.js 调用行、两侧详情与证据渲染
web/dashboard.css  调用日志的浅色样式与响应式布局
web/index.html     /sessions 会话时间线页面
web/app.js          会话时间线状态、请求和交互控制
web/dom.js          按稳定标识更新节点，保留阅读状态
web/views.js        会话、轮次与工具视图渲染
web/format.js       HTML 转义、时间格式、判定标签
web/style.css       会话时间线样式与响应式布局
tests/              关联、日志解析、流式解码和 HTTP 集成测试
data/               本机证据 SQLite 数据库，不加入版本控制
```

## 验证与维护

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py observatory.py session_parser.py capture.py relay.py
```

开发时可使用 Black 和 Prettier 格式化。代码可读性同时包括命名、控制流和职责划分，不能只依靠格式化。

服务仅监听 `127.0.0.1`，拒绝其他 Host 和跨源请求。数据保存在项目的 `data/` 中；停止服务并备份后可删除该目录清空证据，不影响 Codex 自身记录。

## 数据查询与来源

会话证据查询先按 thread_id 过滤，不再受全库 3000 条上限影响。证据总数由 COUNT(*) 统计；默认导出覆盖本会话全部证据。列表摘要按日志文件修改时间、大小和证据变化缓存，首次建立摘要会扫描已发现的日志。

可通过环境变量配置来源路径：

- `OBSERVATORY_SOURCE_PATHS`：本环境使用冒号分隔的 Codex SQLite 状态库路径；默认保留原有来源。
- `OBSERVATORY_LOGS_PATH`：HTTP 运行日志 SQLite 路径。
- `OBSERVATORY_ARCHIVE_ROOT`：历史取证目录，默认 `/tmp`。

`/api/status` 返回 sources，包括最近检查、最近成功和错误信息。路径可配置，但数据库表结构仍需兼容当前 Codex 日志协议。

调用日志接口：

- `/api/calls?client=Codex%20App&limit=25&offset=0`：返回调用观测列表。支持 `client=Codex App|Codex CLI|unknown`、搜索 `q`、`result=match|difference|pending`、会话 `thread_id` 和 `limit/offset`；空筛选值表示全部。首页每页请求 25 条，接口省略 `limit` 时默认 50，最多 500。
- 响应包含 `items`、完整筛选后的 `total`、`stats`、`client_counts`、`sessions` 和 `pagination`。`stats` 应用客户端、搜索和会话条件，忽略 `result`；`client_counts` 应用搜索、会话和状态条件，忽略当前客户端选择。
- `/api/call?id=…`：读取单条聚合记录及 Request、Response 两侧的字段白名单证据和捕获诊断；不存在的记录返回 404。

会话时间线接口保持可用：`/api/session?id=…&limit=20&offset=0` 从最新轮次往前分页，页内按时间顺序排列；`/api/tool` 与 `/api/evidence/item` 按需读取正文；`/api/evidence` 分页；`/api/export?id=…&scope=evidence|conversation` 明确导出范围。

规则脱敏覆盖常见敏感键、嵌套 JSON、环境变量、命令选项与 URL 凭据，不能保证识别所有自由文本秘密。最小证据导出采用顶层及嵌套字段白名单。

## 2026-10-08 CLI 入口漏采修复

00:32 的交互式 CLI 从旧 npm 命令入口连接了未启用观测的共享后台，未经过启动器。现在 npm 的 bin/codex 入口与 Bash/Zsh PATH 都接入观测启动器，原包代码和二进制保留不变。旧终端重新执行 codex 也会经过观测入口；已运行的旧 TUI 需要退出再启动。npm 更新若重建命令链接，需重新核对入口。

除 exec 测试外，已使用真实交互式 PTY 从原 npm 命令入口验证请求/响应配对，并完成本地模拟TUI两次调用验证。`coverage.py` 只读比较运行日志与原生采集元数据，检测漏采并向界面报告；不会把 runtime 档位补成 wire 证据。00:32 缺失的原始请求/响应无法事后恢复。

## 调用用途与后台失败

调用日志保留所有用途，可筛选主对话、标题生成、后台记忆合并和用途未确认。用途只依据原生thread/turn标识、turn_trigger或结构化标题输出schema判断，不根据模型名称或时间猜测父会话。后台同一轮次的尝试分别保留，显示尝试序号与总数。

原生失败诊断保留固定错误分类与HTTP状态码，不保存任意上游错误正文。已保存事件补齐安全诊断时保留原始证据ID与条目数，避免重放产生重复记录。辅助任务失败不等同于主对话失败；页面会分别显示用途、HTTP原因和请求/响应档位。

## 隐私与数据

- 仓库不包含任何模型凭据、API Key 或上游鉴权信息。可选转发器（`relay.py`）只按原字节转发请求，不读取或保存 `Authorization`；网页服务仅监听 `127.0.0.1` 并拒绝跨源请求。
- 代码中的来源路径不含个人标识：Windows 用户目录通过 `/mnt/c/Users/*` 通配发现，桌面日志目录可用 `OBSERVATORY_DESKTOP_LOG_ROOT` 覆盖，其余来源路径用 `OBSERVATORY_SOURCE_PATHS`、`OBSERVATORY_LOGS_PATH`、`OBSERVATORY_ARCHIVE_ROOT` 配置。
- `data/` 保存本机观测证据（SQLite 与事件日志），已在 `.gitignore` 中排除，不会进入版本控制。备份或分享前请自行评估其中内容。
- 原生追踪暂存目录 `data/events/` 在客户端运行期间可能短暂写入完整上下文与工具内容，仅限本机使用，不要分享该目录。
