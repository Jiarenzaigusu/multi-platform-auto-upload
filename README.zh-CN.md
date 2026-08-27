# 天猫与京东多人视频发布台

这是一个供公司内部运营团队使用的 Web 发布系统。云端 FastAPI 负责认证、业务功能、素材和任务队列；用户电脑只需预装一次“MPAU 本地执行助手”，由本机 Microsoft Edge 自动操作天猫/淘宝光合和京东京麦。配对完成后，普通用户日常只需要打开发布台网页。

项目不是平台开放 API，也不会绕过扫码、密码、短信、验证码或风控。登录和人工验证直接出现在用户自己的电脑上，云服务器不会启动 Edge、Patchright Browser 或 BrowserContext。

## 多用户架构

系统运行一个 FastAPI 控制进程，并为每个应用用户按不可变 UUID 延迟创建一个 `UserWorkspace`。每个工作区独立拥有：

- 任务状态与任务队列；
- 云端任务、上传视频、批量素材和回传日志；
- 用户电脑上的天猫、京东 Cookie；
- 用户电脑上的 `BrowserRuntime` 及平台浏览器会话池；
- LLM API Key、激活模型和 AI 文案服务；
- 浏览器端文字草稿和 IndexedDB 视频草稿。

两个用户即使填写相同的店铺标识，也会解析到不同云端任务目录和不同本机 Cookie 目录。每个应用账号同时只允许连接一台本地代理；不同用户的浏览器负载由各自电脑承担，不占用云服务器 CPU 和内存。

登录应用的用户仍可访问自己的云端任务和上传素材，但新登录产生的平台 Cookie 只保存在用户电脑。视频和可选的天猫自定义封面创建任务时先上传到云端，代理领取后再下载到本机临时目录，任务终态后删除单条发布的云端和本机临时副本。

## 角色

| 角色 | 权限 |
| --- | --- |
| 管理员 `admin` | 用户管理、发布、AI 文案、自己的 LLM 配置、自己的任务与素材 |
| 操作员 `operator` | 发布、AI 文案、自己的 LLM 配置、自己的任务与素材 |

初始管理员只能在服务器本机、数据库中还没有任何用户时创建。初始化完成后，运营人员可以在登录页自助注册，服务端固定授予 `operator`，注册成功后自动登录；管理员继续使用已有账号直接登录，也可以在“用户与权限”页面创建或管理账号。密码使用 Argon2id 哈希，登录使用服务端不透明 Session，写请求同时校验 `SameSite=Lax` Cookie、Origin 和 CSRF Token。

系统只接受 `admin` 和 `operator`。从含旧只读角色的版本升级时，原只读账号会被改为已禁用的操作员并撤销全部 Session，必须由管理员确认后手动启用。

## 支持范围

| 平台 | 支持字段 |
| --- | --- |
| 天猫 / 淘宝光合 | 视频、可选自定义封面、标题、文案、标签、最多 6 个商品 ID、活动话题、音乐、定时发布、创作者声明 |
| 京东 / 京东京麦 | 视频与图文、标题/正文、最多 10 个商品 ID、参与话题、自主原创、定时发布、创作者声明 |

创作者声明是单条和批量发布的必填项。Excel 必须使用当前模板并包含“创作者声明”列，不再对缺少该列的旧模板自动补默认值。

## 云端控制台安装

云服务器准备 Python 3.10-3.12、Node.js 20+ 和 Corepack；不需要安装 Microsoft Edge：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[web]'

cd webapp/frontend
corepack pnpm install --frozen-lockfile
corepack pnpm run build
cd ../..

mpau-web
```

打开 <http://127.0.0.1:8788>。第一次启动会显示初始管理员表单。默认数据根目录是项目内的 `data/`；生产服务器必须通过 `MPAU_DATA_DIR` 改到独立持久盘。

Linux 生产环境建议使用仓库提供的 systemd 配置：

```bash
sudo useradd --system --home /var/lib/mpau --shell /usr/sbin/nologin mpau
sudo install -d -o mpau -g mpau -m 0700 /var/lib/mpau/data /var/lib/mpau/releases
sudo install -d -m 0755 /etc/mpau
sudo install -m 0640 -o root -g mpau deploy/linux/mpau.env.example /etc/mpau/mpau.env
sudoedit /etc/mpau/mpau.env
sudo install -m 0644 deploy/linux/mpau-web.service /etc/systemd/system/mpau-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now mpau-web
sudo systemctl status mpau-web
```

把 `YOUR_SERVER_IP` 换成服务器实际 IP，并确认 unit 中的项目路径与服务器一致。查看日志使用 `journalctl -u mpau-web -f`；防火墙只放行办公网、VPN 或批准来源到 TCP `8788`。

## 用户电脑安装本地执行助手

普通用户不需要 Python、项目代码、虚拟环境或命令行。管理员可在 Windows 构建机生成 Windows 安装包：

```powershell
.\deploy\windows\build-mpau-agent.ps1
```

生成的 `deploy\windows\output\MPAU-Agent-Setup.exe` 放到 Linux 服务器的 `/var/lib/mpau/releases/MPAU-Agent-Setup.exe`，或通过 `MPAU_AGENT_INSTALLER_PATH` 指定。构建脚本会同时生成 `agent-installer.json` 版本清单（版本号、SHA-256、大小），两者必须一起上传到同一目录。用户登录网页后下载并安装一次，在网页生成配对码并输入 Windows 助手窗口。Windows 助手使用 DPAPI 保存设备令牌，并随当前 Windows 用户登录自动启动，不保存应用密码。

### Windows 助手自动更新

版本号唯一来源是 `local_agent/__init__.py` 的 `__version__`，构建时自动同步到安装包。管理员发布新版后（把新的 `MPAU-Agent-Setup.exe` 和 `agent-installer.json` 放到服务器同一目录），已配对的 Windows 助手会在启动后和每 6 小时自动检查更新；网页顶部也会在助手在线但版本落后时显示“助手有新版本”提示。用户双击“MPAU 本地执行助手”可打开状态窗口，点击“安装新版本”后会显示下载百分比、完成 SHA-256 校验、退出、静默安装并重启；托盘右键菜单也保留“检查并安装新版本”入口。若有发布任务正在执行，需等待任务完成后再更新。

以后用户只打开云服务器域名；普通功能全部由云端响应，只有平台登录、Cookie 校验和发布任务会自动在用户电脑打开 Edge。Cookie、浏览器会话和平台日志默认保存在 `%LOCALAPPDATA%\MPAU-Agent\users\<user_uuid>\`。

电脑开机时如果网络尚未就绪，助手会在后台持续等待云端恢复，不需要用户运行命令。管理员撤销设备或设备令牌失效后，助手会清除旧连接并重新显示配对窗口。

开发前端可运行：

```bash
# 终端 1
.venv/bin/python -m uvicorn webapp.api.main:app --reload --host 127.0.0.1 --port 8788

# 终端 2
cd webapp/frontend
corepack pnpm run dev
```

Vite 会把 `/api` 代理到本机 `8788`，认证 Cookie 仍保持同源。

## 公司云服务器直接部署方案

云端不执行浏览器自动化，可部署在 Windows Server 或 Linux。FastAPI 可以作为普通后台服务运行，不需要交互式桌面或 RDP 常驻会话。

```text
公司电脑
  └─ HTTP 8788
      └─ 单个 FastAPI 进程（认证、素材、任务租约）

用户 A 电脑 → MPAU 执行助手 → Edge A ─┐
                                    ├─ HTTP → 云端任务队列
用户 B 电脑 → MPAU 执行助手 → Edge B ─┘
```

建议配置：

- 按 Web/API 与视频中转负载选型；起步可使用 4-8 vCPU、8-16 GB 内存，并按并发视频上传扩容；
- 视频素材放在独立持久盘，容量按团队保留策略配置；
- FastAPI 作为普通后台服务直接监听 `0.0.0.0:8788`；
- 防火墙只允许办公网、VPN 或指定 IP 访问 8788，不要对公网完全开放；
- 只运行一个 Uvicorn worker，不要用多进程共享数据目录。

仓库提供：

- `deploy/linux/mpau-web.service`：Linux systemd 服务；
- `deploy/linux/mpau.env.example`：Linux 生产环境变量示例；
- `deploy/windows/start-mpau.ps1`：Windows Server 启动云端 FastAPI；
- `deploy/windows/build-mpau-agent.ps1`：构建自包含 Windows 安装包；
- `deploy/windows/start-mpau-agent.ps1`：开发环境启动桌面助手；
- `deploy/windows/mpau.env.example.ps1`：生产环境变量示例。

详细安装、首次初始化和后台启动见 [Web 发布台与服务器部署文档](docs/web-app.md)。

## 运行配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MPAU_DATA_DIR` | `<项目>/data` | 系统数据库与全部用户目录；生产必须放持久盘 |
| `MPAU_BIND_HOST` | `0.0.0.0` | FastAPI 监听地址；直连服务器使用 `0.0.0.0` |
| `MPAU_PORT` | `8788` | FastAPI 监听端口 |
| `MPAU_SESSION_SECONDS` | `43200` | 应用登录 Session 有效期 |
| `MPAU_ALLOW_REMOTE_BOOTSTRAP` | `false` | 首次远程创建管理员时临时设为 `true`，完成后改回 `false` |
| `MPAU_ALLOWED_HOSTS` | `127.0.0.1,localhost` | 允许的 Host；生产加入服务器 IP 或域名 |
| `MPAU_ALLOWED_ORIGINS` | 本机开发地址 | 允许发起写请求的完整 Origin，直连需包含 `http://服务器IP:8788` |
| `MPAU_AGENT_INSTALLER_PATH` | `deploy/windows/output/MPAU-Agent-Setup.exe` | 网页提供下载的 Windows 安装包 |
| `MPAU_MAX_UPLOAD_REQUEST_BYTES` | `21474836480` | 单个 HTTP 上传请求上限 |
| `MPAU_MAX_MEDIA_TOTAL_BYTES` | `107374182400` | 每个用户批量素材与待执行上传的总容量上限 |
| `MPAU_MAX_MEDIA_FILES` | `1000` | 每个用户批量素材库最多保留的文件数 |

### Windows 助手浏览器显示尺寸

本地助手启动 Edge 的可见浏览器时，会自动读取 Windows 主屏的可用工作区，统一设置 Chromium 的 DPI 比例和窗口位置。天猫、京东、抖音、小红书现在全部使用 Edge；浏览器会尽量以 `1480x1000` 的逻辑窗口打开，如果屏幕较小，会自动缩小浏览器外壳以保证窗口和发布流程涉及的页面区域完整可见。每个平台的 Playwright 页面仍使用固定逻辑 viewport（天猫/抖音/小红书 `1280x900`，京东 `1440x900`），并固定 `device_scale_factor=1`，因此系统显示缩放为 125% 或 150% 时不会改变自动化脚本的 DOM 布局和截图坐标体系。无头任务不依赖物理屏幕，保持 1:1 比例。

生产环境示例：

```powershell
$env:MPAU_DATA_DIR = "D:\MPAU\data"
$env:MPAU_BIND_HOST = "0.0.0.0"
$env:MPAU_PORT = "8788"
$env:MPAU_ALLOWED_HOSTS = "YOUR_SERVER_IP_OR_DOMAIN,127.0.0.1,localhost"
$env:MPAU_ALLOWED_ORIGINS = "http://YOUR_SERVER_IP_OR_DOMAIN:8788,http://127.0.0.1:8788,http://localhost:8788"
$env:MPAU_ALLOW_REMOTE_BOOTSTRAP = "false"
$env:MPAU_MAX_MEDIA_TOTAL_BYTES = "107374182400"
```

## 数据目录

```text
<MPAU_DATA_DIR>/
├─ system/
│  └─ auth.db                    # 用户、Session 和安全审计
└─ users/<user_uuid>/
   ├─ runtime/                   # state.json 与任务管理器锁
   ├─ cookies/{tmall,jd}/        # 本地执行工作区的账号 Cookie 目录
   ├─ uploads/                   # 单条发布临时副本，终态自动清理
   ├─ media/                     # 用户上传的批量视频素材
   ├─ job-logs/                  # 每任务独立日志
   ├─ platform-logs/             # 平台级回退日志
   └─ secrets/                   # 用户自己的 LLM 凭据
```

POSIX 系统上目录使用 `0700`，敏感文件使用 `0600`。云端 LLM API Key 和用户电脑上的平台 Cookie 都是明文应用凭据，依赖各自操作系统权限保护；不要把 `data/`、代理数据目录、Cookie、`runtime/` 或日志提交到代码仓库。

用户电脑另有独立的 `MPAU_AGENT_DATA_DIR`，其中保存平台 Cookie 和浏览器日志。删除云端用户不会自动擦除用户电脑上的代理目录，应通过账号删除任务或终端管理策略清理。

普通成功、失败和取消任务最多保留 90 天或最近 2000 条；`uncertain` 任务不会自动删除，需人工核对。任务日志默认保留 30 天，异常中断留下的临时下载目录会在后续启动时清理。

## 使用要点

1. 先在服务器本机创建初始管理员；运营人员随后可在登录页注册自己的 `operator` 账号，管理员直接登录。
2. 在“LLM 适配器”中配置自己的 DeepSeek、千问或豆包 API Key；同一用户同时只激活一个模型。
3. 首次使用时从网页安装本地执行助手并输入一次性配对码；以后助手自动启动，页面显示“本地代理在线”后即可创建任务。
4. 首次使用店铺先执行“登录 / 重新登录”，在自己电脑弹出的 Edge 中完成平台验证，再执行 Cookie 校验。
5. 单条发布默认使用浏览器文件选择框直传 Windows 助手：视频、封面和图文图片只写入当前电脑的助手素材目录，不经过云端服务器；服务器仅保存一次性素材 ID、文件名、大小和 SHA-256。
6. 单条发布不需要填写视频绝对路径，也不会把单条素材上传到服务器；天猫和京东视频均可选自定义封面（京东 JPG/PNG 最大 5 MiB，天猫 JPG/PNG/WebP 最大 20 MiB）。京东图文支持 1-20 张 JPG/PNG 图片与正文。
7. 批量发布 Excel 中的视频路径、封面路径或图片文件夹路径填写当前用户电脑上的绝对路径，由配对的本地执行助手读取；京东与天猫的视频/图文模板彼此独立。
8. 退出网页不取消任务；关闭本地代理会使未领取任务继续排队，已领取发布任务在租约超时后进入“结果待核对”。
9. AI 文案读取天猫商品时会通过本地执行助手复用登录态，因此助手必须在线且至少有一个有效天猫账号。

每个本地代理一次只领取一个任务，同一账号保持 FIFO；不同用户由各自电脑并行执行。代理通过心跳续租，连接中断后未领取任务继续排队，已领取发布任务会保守标记为“结果待核对”，避免重复发布。

## 验证

```bash
.venv/bin/python -m pip install -e '.[web,test]'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m compileall -q local_agent uploader utils webapp
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider

cd webapp/frontend
corepack pnpm run build
```

## 项目结构

```text
webapp/auth/                 # 用户、Argon2id、Session、CSRF 与管理接口
webapp/workspaces/           # 用户路径与工作区生命周期
webapp/api/                  # 发布、素材、批量任务与浏览器调度
webapp/ai_copy/              # AI 文案领域与商品读取
webapp/llm_adapter/          # 用户级模型凭据和激活路由
webapp/frontend/src/         # 登录、用户管理与业务界面
local_agent/                 # 用户电脑代理、云端客户端和本机任务执行器
uploader/                    # 天猫与京东浏览器自动化
deploy/linux/                # Linux systemd 服务与环境示例
deploy/windows/              # Windows Server 启动与打包示例
tests/                       # 单元测试和多用户 ASGI 集成测试
```

## 免责声明

本项目仅用于公司自有商家账号运营和合法自动化测试。请遵守平台规则，不要上传违规内容或滥用自动化能力。
