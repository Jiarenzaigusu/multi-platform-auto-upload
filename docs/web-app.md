# Web 发布台：本地执行代理架构与部署

## 1. 架构边界

系统由云端控制面和用户电脑上的本地执行面组成：

- 云端 FastAPI 负责应用认证、用户隔离、素材上传、任务排队、租约、结果展示和 AI 文案；
- 用户电脑预装自包含的“MPAU 本地执行助手”，主动通过服务器地址轮询并领取当前应用账号的任务；
- Microsoft Edge、Patchright `BrowserRuntime`、平台 Cookie、扫码、短信、验证码和浏览器日志都只在用户电脑上运行或保存；
- 云服务器不会创建 Edge、Browser、BrowserContext 或 Page，也不需要图形桌面、RDP 常驻会话；
- 视频和可选的天猫自定义封面会先上传到云端任务区，再由代理下载到用户电脑，因此云服务器仍承担素材带宽和临时存储，不承担浏览器 CPU/内存负载。

本系统面向公司内部可信运营人员，不会绕过平台扫码、密码、短信、验证码或风控，也不是平台开放 API。

```text
用户浏览器 ── HTTP 8788 ──┐
                          ├─ FastAPI ── auth.db / 任务 / 云端素材
mpau-agent ── HTTP 8788 ──┘                  （不启动 Edge）
     │
     ├─ 下载任务视频到本机临时目录
     ├─ 启动本机 Microsoft Edge
     ├─ 用户在本机完成登录和验证码
     └─ 回传状态、结果和最多 500 行任务日志
```

代理只建立出站连接，云服务器不需要反向连接用户电脑，也不需要给用户电脑开放入站端口。

## 2. 任务流程

### 2.1 助手配对与连接

1. 用户登录发布台网页；
2. 首次使用时下载并安装 Windows 助手 `MPAU-Agent-Setup.exe`；不需要 Python、项目代码或命令行；
3. 用户在网页生成 5 分钟有效的一次性配对码，并输入本地助手窗口；
4. 助手用配对码换取只允许访问代理接口的设备令牌，Windows 上通过当前用户 DPAPI 加密保存；
5. 助手写入当前 Windows 用户登录启动项，以后自动连接；
6. 页面通过 `/api/agent/status` 显示“本地代理在线”，助手空闲时约每 2 秒轮询任务。

设备令牌不能访问普通业务 API，不能替代网页 Session。重新配对会撤销该应用用户原设备令牌；账号禁用、密码重置、管理员撤销会话或网页解除配对也会使设备令牌失效。不同应用用户可以在各自电脑并行执行，不共享浏览器容量。

### 2.2 登录与 Cookie 校验

1. 页面创建 `login` 或 `check` 任务，云端仅把任务写入队列；
2. 本地代理领取任务后，在用户电脑创建或复用平台浏览器会话；
3. Edge 在用户电脑显示，用户在本机处理扫码、密码、短信和验证码；
4. 平台 Cookie 保存到该电脑的代理数据目录；
5. 代理把任务结果和日志回传云端，不上传 Cookie。

### 2.3 发布

1. 用户在页面填写发布参数并上传视频；天猫任务可同时上传一张自定义封面；
2. 云端保存视频和 `queued` 任务，但不调用上传器或浏览器；
3. 本地代理领取任务并获得 45 秒执行租约；
4. 代理通过受认证的素材接口把视频和可选封面下载到本机临时目录，下载和执行期间持续发送心跳；
5. 代理在本机 Edge 中执行天猫或京东发布流程；
6. 任务结束后，代理回传结果和日志，并删除本机临时视频；
7. 云端在任务进入终态后清理单条发布的受管上传目录。

批量素材保存在云端用户 `media/` 目录，由不同任务按需下载，不随单条任务结束删除。

### 2.4 租约、取消和不确定结果

- 同一个代理一次只领取一个任务；同一用户内，相同“平台 + 店铺标识”的任务保持 FIFO；
- 代理执行时约每 10 秒发送心跳，服务端执行租约默认 45 秒；
- 未领取任务在代理离线时继续保持 `queued`；
- 登录、校验或删除账号任务在租约过期后标记为 `failed`；
- 发布任务在执行中失联、提交后被中断或无法确认平台结果时标记为 `uncertain`（页面显示“结果待核对”）；
- `uncertain` 任务必须先到平台后台核对，确认前不要直接重试，以免重复发布；
- 用户请求取消时，代理会取消本机浏览器 Future。平台提交前可安全收敛为 `cancelled`，提交后无法确认则收敛为 `uncertain`。

云端重启不会把历史运行中的发布任务当作成功。没有有效本地代理租约的旧运行任务会保守收敛，要求人工核对。

## 3. 认证与权限

### 3.1 初始管理员

当 `auth.db` 中没有用户时，`GET /api/auth/status` 返回 `setup_required=true`。初始管理员只能从服务器视角的本机地址创建，并且要满足：

- 数据库仍然没有任何用户；
- 用户名为 3-64 位字母、数字、点、下划线或连字符；
- 密码为 10-256 个字符。

数据库使用事务原子关闭并发初始化窗口。创建成功后自动登录。

### 3.2 登录与注册

- 已有管理员直接使用用户名和密码登录；
- 初始管理员创建后，登录页开放自助注册；
- 自助注册固定创建启用状态的 `operator`，客户端不能指定或提升角色；
- 注册成功后自动登录；
- 管理员可在“用户与权限”中创建、禁用、改角色、重置密码或撤销会话；
- 最后一个启用的管理员不能被停用或降级。

| 功能 | `admin` | `operator` |
| --- | ---: | ---: |
| 查看和管理自己的任务、账号、素材、日志 | 是 | 是 |
| 登录平台、校验 Cookie、发布和取消 | 是 | 是 |
| 使用 AI 文案及自己的 LLM 配置 | 是 | 是 |
| 创建、禁用用户、改角色和重置密码 | 是 | 否 |

系统不提供只读角色。从旧版升级时，旧只读账号会保留用户 UUID，但会转换为已禁用的 `operator` 并撤销 Session，需要管理员确认后再启用。

### 3.3 Session 与 CSRF

- 密码以 Argon2id 哈希保存，不保存可逆密码；
- `mpau_session_v2` 是 `HttpOnly` 不透明 Cookie，数据库只保存其 SHA-256 哈希；
- `mpau_csrf_v2` 可由前端读取，写请求还必须发送 `X-CSRF-Token`；
- HTTP 直连 Cookie 使用 `SameSite=Lax`；
- Session 默认 12 小时，禁用用户、重置密码或撤销会话会使旧 Session 失效；
- 登录失败按用户名和客户端 IP 限速；安全事件写入审计表，但不记录密码或 Token。

本地助手使用独立 Bearer 设备令牌，不保存应用密码，也不复用浏览器 Cookie。令牌只在 `/api/agent/*` 代理接口生效；普通网页继续使用 Session 和 CSRF。

## 4. 数据边界

### 4.1 云服务器

```text
<MPAU_DATA_DIR>/
├─ system/
│  └─ auth.db
└─ users/<32位用户UUID>/
   ├─ runtime/                   # 任务状态、租约与实例锁
   ├─ uploads/                   # 单条发布临时视频
   ├─ media/                     # 批量视频素材
   ├─ job-logs/                  # 代理回传的任务日志
   ├─ cookies/                   # 本地执行工作区的账号 Cookie 目录
   ├─ platform-logs/             # 平台级回退日志
   └─ secrets/                   # 当前用户自己的 LLM 凭据
```

单条发布视频和可选封面进入 `uploads/<uuid>/`，任务进入成功、失败、取消或“结果待核对”后清理。批量素材属于用户维护的源文件，任务引用期间拒绝删除。

视频没有额外的应用层端到端加密；云服务器会暂存可读取的视频文件。HTTP 直连部署必须限制网络访问范围，并配合受控主机、持久盘权限、备份权限和保留策略。

### 4.2 用户电脑

Windows 默认路径：

```text
%LOCALAPPDATA%\MPAU-Agent\
├─ device.json
├─ connection.json              # DPAPI 保护的云端设备令牌
└─ users\<32位用户UUID>\
   ├─ cookies\{tmall,jd}\
   ├─ uploads\<job_id>\          # 正在执行任务的临时下载
   ├─ job-logs\
   └─ platform-logs\
```

Windows 助手默认将数据保存在当前用户的 Local AppData 目录。可通过 `MPAU_AGENT_DATA_DIR` 或代理的 `--data-dir` 参数覆盖。

平台 Cookie 和浏览器日志不上传云端。删除云端用户不会自动擦除已离线电脑上的数据；应在用户仍可连接时从页面创建“删除本地账号”任务，或由终端管理策略清理代理目录。

用户 UUID、账号名和素材路径都经过校验。批量 Excel 的视频路径只能是当前用户 `media/` 下的相对路径，绝对路径、`..` 和符号链接逃逸都会被拒绝。

## 5. 平台使用

### 5.1 店铺登录

1. 在用户电脑启动本地代理；
2. 页面显示“本地代理在线”后，选择平台并填写店铺标识；
3. 点击“登录 / 重新登录”；
4. 在当前电脑弹出的 Edge 中完成扫码、密码、短信或验证码；
5. 回到页面执行“校验 Cookie”。

相同“应用用户 + 平台 + 店铺标识”在本机复用浏览器会话。浏览器空闲后会保存状态并关闭；显示模式变化、账号删除、浏览器断线或代理停止也会关闭或重建会话。

### 5.2 单条发布

天猫支持自定义封面、标题、文案、标签、活动话题、音乐、最多 6 个商品 ID、定时发布和创作者声明。自定义封面支持 JPG、PNG 或 WebP，最大 20 MiB，宽高均需至少 720 像素；视频上传完成后，助手会执行“编辑封面 → 本地上传 → 应用封面”。京东支持标题、一个商品 ID、自主原创、定时发布和创作者声明。定时发布时间至少提前 2 小时。

首次使用某个平台或平台页面发生变化时，保留“流程验证”，确认页面字段、商品和声明无误后再执行正式发布。

### 5.3 批量发布

1. 在“批量视频素材”上传一个或多个视频；
2. 下载对应平台的当前 Excel 模板；
3. `视频路径` 填云端素材目录内的相对文件名，例如 `video.mp4`；
4. 每行填写有效的“创作者声明”；
5. 上传 Excel，服务端先校验全部行，再一次性创建最多 200 个任务。

任一行校验失败时整批不创建。单条任务失败不会阻止同一批后续任务。

## 6. AI 文案与 LLM

每个管理员或操作员分别保存自己的 DeepSeek、千问和豆包 API Key，并同时激活一个模型。状态接口不返回 Key；LLM Key 保存在云服务器当前用户的 `secrets/` 中，因此依赖服务器账号、数据盘权限和备份权限保护。

激活新模型时会先发送最小探测请求，成功后才保存并原子切换。商品读取服务的临时 Key 只存在于当前请求；携带凭据的外部请求禁止自动跟随重定向，公共 URL 读取会拦截本机、内网和保留地址。

## 7. 云服务器部署

云端不运行浏览器，可部署在 Linux 或 Windows Server。它不需要 Microsoft Edge、Desktop Experience、交互式桌面或 RDP 常驻会话。

### 7.1 服务器与网络

起步建议：

- 4-8 vCPU、8-16 GB RAM；按 API 请求、AI 请求和视频中转并发扩容；
- 系统盘与数据盘分离，视频容量按团队峰值、并发量和保留策略估算；
- FastAPI 直接监听 `0.0.0.0:8788`；
- 8788 只允许办公网、VPN 或受控来源访问，禁止对公网完全开放；
- 只运行一个 Uvicorn worker/项目实例，当前文件任务存储和租约不支持多进程共享；
- 为大视频上传与代理下载预留双向公网或专线带宽。

浏览器负载与云服务器规格无关，由每位用户电脑承担。云端容量重点观察磁盘、文件句柄、上传/下载带宽、API 延迟和任务数据库写入。

### 7.2 安装

Linux 示例：

```bash
cd /opt/multi-platform-auto-upload-main
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[web]'

cd webapp/frontend
corepack pnpm install --frozen-lockfile
corepack pnpm run build
cd ../..
```

Linux 生产环境使用 systemd：

```bash
sudo useradd --system --home /var/lib/mpau --shell /usr/sbin/nologin mpau
sudo install -d -o mpau -g mpau -m 0700 /var/lib/mpau/data /var/lib/mpau/releases
sudo install -d -m 0755 /etc/mpau
sudo install -m 0640 -o root -g mpau deploy/linux/mpau.env.example /etc/mpau/mpau.env
sudoedit /etc/mpau/mpau.env
sudo install -m 0644 deploy/linux/mpau-web.service /etc/systemd/system/mpau-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now mpau-web
```

`deploy/linux/mpau-web.service` 默认项目路径是 `/opt/multi-platform-auto-upload-main`，如果实际路径不同，需要同步修改 `WorkingDirectory` 和 `ExecStart`。服务日志查看：

```bash
sudo systemctl status mpau-web
sudo journalctl -u mpau-web -f
```

防火墙只允许办公网、VPN 或批准来源访问 TCP `8788`。例如使用 UFW 时，不要直接对公网开放：

```bash
sudo ufw allow from YOUR_ALLOWED_CIDR to any port 8788 proto tcp
```

Windows Server 示例：

```powershell
cd C:\Apps\multi-platform-auto-upload-main
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[web]"

corepack enable
cd webapp\frontend
pnpm install --frozen-lockfile
pnpm run build
cd ..\..

Copy-Item deploy\windows\mpau.env.example.ps1 deploy\windows\mpau.local.ps1
notepad deploy\windows\mpau.local.ps1
```

云端安装仍可能包含 Patchright Python 依赖，因为上传器与本地代理共用一个发行包；生产 `UserWorkspaceRegistry` 默认使用 `AgentTaskManager`，不会实例化 `BrowserRuntime` 或启动 Edge。

### 7.3 生产环境变量

Linux 直接使用 `deploy/linux/mpau.env.example` 作为 `/etc/mpau/mpau.env`；Windows 使用 PowerShell 环境变量或 `deploy/windows/mpau.local.ps1`。两者使用相同的 `MPAU_*` 配置。

| 变量 | 默认值 | 生产建议 |
| --- | --- | --- |
| `MPAU_DATA_DIR` | `<项目>/data` | 指向独立持久盘 |
| `MPAU_BIND_HOST` | `0.0.0.0` | FastAPI 监听地址；直连服务器使用 `0.0.0.0` |
| `MPAU_PORT` | `8788` | FastAPI 监听端口 |
| `MPAU_SESSION_SECONDS` | `43200` | 按公司会话策略设置 |
| `MPAU_ALLOW_REMOTE_BOOTSTRAP` | `false` | 首次远程创建管理员时临时设为 `true`，完成后改回 `false` |
| `MPAU_ALLOWED_HOSTS` | 本机地址 | 加入准确的服务器 IP 或域名 |
| `MPAU_ALLOWED_ORIGINS` | 本机开发地址 | 只保留实际 Origin，直连需包含 `http://服务器IP:8788` |
| `MPAU_MAX_UPLOAD_REQUEST_BYTES` | `21474836480` | 单个 HTTP 上传请求上限 |
| `MPAU_MAX_MEDIA_TOTAL_BYTES` | `107374182400` | 限制每用户批量素材与待执行上传总量 |
| `MPAU_MAX_MEDIA_FILES` | `1000` | 按素材保留策略调整 |

Windows 示例：

```powershell
$env:MPAU_DATA_DIR = "D:\MPAU\data"
$env:MPAU_BIND_HOST = "0.0.0.0"
$env:MPAU_PORT = "8788"
$env:MPAU_ALLOWED_HOSTS = "YOUR_SERVER_IP_OR_DOMAIN,127.0.0.1,localhost"
$env:MPAU_ALLOWED_ORIGINS = "http://YOUR_SERVER_IP_OR_DOMAIN:8788,http://127.0.0.1:8788,http://localhost:8788"
$env:MPAU_ALLOW_REMOTE_BOOTSTRAP = "false"
$env:MPAU_MAX_MEDIA_TOTAL_BYTES = "107374182400"
```

不要在云端配置 `MPAU_EDGE_PATH` 或浏览器并发参数。`MPAU_USER_WORKERS` 即使保留也不代表云端浏览器数量；本地代理目前按单任务串行执行。

普通终态任务自动保留 90 天或最近 2000 条，`uncertain` 任务保留到人工处理；任务日志默认保留 30 天。

### 7.4 初始管理员

初始管理员默认只接受服务器视角的本机来源。直连部署需要远程初始化时，临时设置 `MPAU_ALLOW_REMOTE_BOOTSTRAP=true`，在浏览器访问 `http://服务器IP:8788` 创建初始管理员。完成后停止服务，把 `MPAU_ALLOW_REMOTE_BOOTSTRAP=false`，再重新启动。

### 7.5 直接启动与后台运行

Linux 使用 `deploy/linux/mpau-web.service` 作为 systemd 服务；Windows Server 可用 `deploy/windows/start-mpau.ps1` 并配置为“无论用户是否登录都运行”的任务计划或公司批准的服务。云端服务没有交互式浏览器，后台会话不影响任务执行。

后台启动只保留一个 FastAPI 实例，不要并行启动第二个 Uvicorn 进程。

### 7.6 上线检查

```text
[ ] FastAPI 监听 0.0.0.0:8788
[ ] 防火墙仅允许办公网、VPN 或批准来源访问 8788
[ ] 首次管理员创建后已关闭 MPAU_ALLOW_REMOTE_BOOTSTRAP
[ ] Allowed Hosts/Origins 使用准确服务器 IP 或域名而非通配符
[ ] 只运行一个 Uvicorn worker/项目实例
[ ] /api/health 返回 execution_mode=local_agent
[ ] /api/readiness 不依赖 Edge 且返回 ready
[ ] 云端没有 Edge/浏览器进程
[ ] 数据目录位于持久盘并纳入备份
[ ] 两个测试用户的任务、素材和代理状态互不可见
[ ] 用户电脑能显示“本地代理在线”并完成登录、校验和流程验证
```

## 8. 用户电脑部署本地执行助手

普通用户电脑只需要：

- Windows 10/11；
- Microsoft Edge；
- 能访问发布台地址和平台站点；
- 允许用户桌面显示 Edge 并处理验证码。

普通用户不安装 Python、不下载项目代码，也不运行命令。管理员在 Windows 构建机安装 Python 3.12 和 Inno Setup 6，然后在项目根目录生成安装包：

```powershell
.\deploy\windows\build-mpau-agent.ps1
```

把生成的 `deploy\windows\output\MPAU-Agent-Setup.exe` 和 `agent-installer.json` 一起上传到 Linux 服务器的 `MPAU_AGENT_INSTALLER_PATH` 所在目录（默认 `/var/lib/mpau/releases/`），并保持文件名不变。新版本的 `local_agent/__init__.py` 版本号必须高于已发布版本，已安装助手才能检测并安装更新。

脚本使用 PyInstaller 生成自包含桌面程序，再用 Inno Setup 输出：

```text
deploy\windows\output\MPAU-Agent-Setup.exe
```

把该文件复制到云服务器默认位置，或设置 `MPAU_AGENT_INSTALLER_PATH` 指向它。网页检测到文件后显示“下载 Windows 执行助手”。公司也可以通过 Intune、组策略或终端管理平台静默预装，这样普通用户从第一次使用开始就只需要打开网页。

用户首次配对流程：

1. 登录发布台；
2. 离线提示中点击“生成一次性配对码”；
3. 打开本地执行助手，填写发布台地址和配对码；
4. 配对成功后系统托盘显示助手，页面在下一次轮询时显示在线；
5. 以后登录 Windows 时助手自动启动，用户日常只访问发布台网页。

Windows 登录时即使网络暂时不可用，助手也会在后台持续重连。管理员撤销设备或设备令牌失效时，助手会停止旧连接、清除本地令牌并重新显示配对窗口。

助手必须运行在当前用户的交互桌面，不能作为 Session 0 系统服务。升级时由管理员发布签名的新安装包或通过终端管理平台覆盖安装。换电脑时在新电脑重新配对即可撤销旧设备令牌；平台 Cookie 默认不迁移，应重新登录平台。

## 9. 运维、安全与恢复

### 9.1 健康检查

- `GET /api/health`：检查 HTTP 进程，返回 `execution_mode=local_agent`；
- `GET /api/readiness`：检查数据目录、认证初始化、工作区注册表、前端构建和维护错误，不检查云端 Edge；
- `GET /api/agent/status`：按当前登录用户返回其本地代理在线状态。

尚未创建初始管理员时 readiness 返回 `503 degraded` 属正常初始化状态。某位用户代理离线或平台 Cookie 失效不会让整个云服务 readiness 失败。

### 9.2 备份与恢复

至少每日备份完整 `MPAU_DATA_DIR`。一致性要求高时先停止 FastAPI，再复制 `auth.db`、任务状态、素材和 LLM 凭据。恢复步骤：

1. 停止 FastAPI；
2. 恢复到原 `MPAU_DATA_DIR`；
3. 恢复目录权限；
4. 只启动一个 FastAPI 进程；
5. 检查 readiness 和“结果待核对”任务。

云端备份不包含新产生的平台 Cookie。本机 Cookie 是否备份由公司终端安全策略决定；最安全的恢复方式是在新电脑重新登录平台。

### 9.3 安全注意事项

- 云端视频、LLM Key 和本机 Cookie 都是敏感数据，应使用最小权限账号、磁盘加密和受控备份；
- 不要把 `data/`、代理数据目录、Cookie、日志或 `.env` 提交到代码仓库；
- 只允许办公网、VPN 或批准来源访问 HTTP 服务，避免将发布台暴露到公网；
- 禁用或重置应用用户密码会使代理 Session 失效，但不会远程擦除离线电脑；
- 正式发布出现网络中断、浏览器崩溃或结果回传失败时，先在平台后台核对。

### 9.4 容量

云端容量由同时上传/下载的视频数量、素材大小、保留时长和 API 流量决定，不再以浏览器并发数估算。建议监控：

- 数据盘总量、`uploads/` 与 `media/` 增长；
- 入口上传带宽和代理下载带宽；
- API 延迟、5xx、任务排队时长和 `uncertain` 数量；
- 长期离线代理对应的排队任务。

本地电脑一次执行一个任务，Edge 的 CPU/内存由该电脑承担。多个应用账号如需在同一电脑运行多个代理，应使用不同 `--data-dir`，同时评估本机资源；通常更推荐每名操作员使用自己的应用账号与电脑。

## 10. 测试与发布前验证

```bash
.venv/bin/python -m pip install -e '.[web,test]'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m compileall -q local_agent uploader utils webapp
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider

cd webapp/frontend
corepack pnpm run build
```

验证至少应覆盖：公开注册只能创建 `operator`、多用户工作区隔离、本地代理连接/领取/心跳/完成、视频访问授权、删除本机账号任务、租约过期的保守状态，以及生产工作区的 `browser_runtime is None`。
