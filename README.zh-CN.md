# Luma

[English](README.md)

**许可：个人非商业使用免费；商业使用须事先取得 [meliwanx](https://github.com/meliwanx) 的书面授权。** 本项目属于公开源码项目，详见[许可证](#许可证)。

Luma 是一个可以自己部署的 AI 助理，支持多个客户端。你可以在网页、桌面应用（macOS / Windows）或 iPhone 上和它对话。它用大模型做规划、调用工具，在每个用户独立的云端沙箱里运行代码或操作浏览器，最后只把结果交给你。

推理和执行全部发生在你的服务器和沙箱里，客户端只是同一个账号的不同入口。换设备或者关掉客户端，正在运行的任务都不会中断。

> 状态：公开源码早期版本，只发布源代码。不提供预构建的 Docker 镜像、桌面安装包或应用商店版本，全部需要从本仓库自行构建。服务端、网页端和桌面端可以正常使用；iOS 客户端可以从源码构建。

## 功能

- **一个主聊天，加按主题拆分的旁聊。** 主聊天长期保留，旁聊按话题新建，可以全文搜索所有对话。切换会话或断开连接后，回复会在后台继续生成，回来时自动接上。
- **带工具的 Agent 循环。** 每次请求都有轮次和工具调用次数的上限，工具可以并发执行；结果太大时截断，但保持结构完整；次数用完时强制给出最终答案。中间调用工具的过程不展示给你，你只看到一条干净的回答。
- **云端沙箱（可选）。** 通过腾讯云 Agent Runtime 的 E2B 兼容接口接入，提供：
  - 每用户独立的代码执行（Python / Shell）；
  - 长时间运行的后台任务；
  - 文件的导入和导出；
  - 端口预览；
  - 通过 CDP 控制的 Chromium 浏览器，并能实时查看画面。

  沙箱闲置时会暂停而不是销毁，工作区会备份，下次可以恢复。
- **MCP 连接器。** 可以在设置里添加远程 MCP 服务，也可以直接在对话里粘贴配置。只允许 HTTPS 地址，并做了防 SSRF 处理。令牌用 Fernet 加密保存，模型永远看不到。服务端自带的使用说明在需要时才会读取。
- **权限。** 只读操作和沙箱内的操作直接执行；对外的写操作需要你确认，除非你设置了始终允许；删除、批量、支付这类操作每次都要确认。
- **记忆、任务、目标、例程。** 长期记忆可以编辑，另外支持任务跟进和定时例程。
- **点子、动态和主动消息。**
  - 点子：每天根据你的情况给出「我可以帮你做什么」的建议。
  - 动态：按你关心的话题写资讯，引用的链接会校验，必须是真实打开过的网页。
  - 主动消息：每天最多几条，只在你设定的时间段内发送。
- **资源库。** 你上传的文件和助理生成的文件（导出的文件、截图）都在这里，可以预览，也能跳回生成它的那段对话。
- **语音输入（可选）。** 先把语音转成文字，再由大模型整理：去掉口头禅，还原列表和标点。
- **用量统计。** 每次模型调用都记录 token 数、首字延迟和总耗时。普通用户看自己的数据，管理员看汇总。
- **账号。** 自带注册、登录、修改密码、登录设备管理和删除账户，见[账号安全](#账号安全)。

## 架构

```
 Web (React) ─┐
 桌面端        ├──►  FastAPI  ──►  大模型（OpenAI 兼容）
 (Electron)   │      │  │   ──►  腾讯云 Agent Runtime 沙箱（代码、浏览器）
 iOS (Flutter)┘      │  │   ──►  MCP 服务（HTTPS）
                     │  └──►  PostgreSQL（业务数据、迁移）
                     └─────►  Redis（会话、事件流、限流）
```

| 目录 | 内容 |
| --- | --- |
| `backend/` | FastAPI 应用、Agent 循环、工具、Alembic 迁移、测试 |
| `apps/web/` | React + Vite 网页端（也由后端直接提供） |
| `apps/desktop/` | Electron 桌面壳，含托盘和全局快捷键 |
| `apps/flutter/` | Flutter 客户端（以 iOS 为主） |
| `docs/` | 架构说明 |
| `scripts/` | 备份、恢复和校验脚本 |

## 快速开始（Docker）

需要先装好 Docker Engine 和 Compose 插件。

```bash
git clone https://github.com/meliwanx/luma.git
cd luma
cp .env.example .env
chmod 600 .env
# 编辑 .env：填写 DB_PASSWORD、REDIS_PASSWORD、AUTH_SESSION_SECRET、
# LUMA_SECRETS_KEY 和 LLM_* 这几项（生成密钥的命令写在文件里）。

docker compose up -d --build --wait   # 从本仓库源码构建镜像并启动
docker compose exec luma python -m app.cli create-admin --username admin
```

然后打开 <http://127.0.0.1:8000>，用 `admin` 登录。

默认只监听 `127.0.0.1`。请**先创建首个管理员，再开放端口**。要对公网提供服务，请在前面加一层 TLS 反向代理，并相应设置 `LUMA_BIND_HOST`、`AUTH_COOKIE_SECURE=true` 和 `CORS_ORIGINS`。

如果暂时没有模型服务，可以设置 `LUMA_PROVIDER=local` 来体验：回复是固定内容，其余界面都能正常使用。

## 配置

所有配置都通过 `.env` 里的环境变量完成，完整清单和注释见 [`.env.example`](.env.example)。最先需要填的是这些：

| 变量 | 作用 |
| --- | --- |
| `DB_PASSWORD`、`REDIS_PASSWORD` | 内置 PostgreSQL 和 Redis 的密码 |
| `AUTH_SESSION_SECRET` | 会话签名密钥 |
| `LUMA_SECRETS_KEY` | 加密连接器令牌用的 Fernet 密钥。务必备份：丢了它，已保存的令牌就无法解密 |
| `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` | 任意 OpenAI 兼容的对话接口 |
| `AUTH_REGISTRATION` | 首个账号之后的注册方式：`invite`（默认）、`open` 或 `closed` |
| `AUTH_INVITE_CODE` | `invite` 模式下注册所需的邀请码 |
| `AUTH_BOOTSTRAP_TOKEN` | 可选：通过 API 而不是命令行创建首个管理员 |
| `LUMA_BIND_HOST`、`LUMA_PORT` | 对外监听的地址和端口（默认 `127.0.0.1:8000`） |
| `AGENT_RUNTIME_ENABLED`、`E2B_DOMAIN`、`E2B_API_KEY`、`AGENT_RUNTIME_*_TOOL` | 腾讯云 Agent Runtime 沙箱（可选） |
| `FILE_STORAGE` | `local`（默认，存在 Docker 卷里）、`cos` 或 `fileservice` |
| `MCP_BLOCKED_HOSTS`、`BROWSER_BLOCKED_HOSTS` | 填你服务器自己的公网地址，防止工具反过来访问服务器本身 |

### 云端沙箱

没配置沙箱时，代码执行和浏览器功能都不可用。使用腾讯云 Agent Runtime 的步骤：

1. 在控制台创建沙箱工具：代码解释器和浏览器各一个，或者只建一个 All-In-One。
2. 创建一个 API Key。
3. 设置以下环境变量：

```
AGENT_RUNTIME_ENABLED=true
AGENT_RUNTIME_API_MODE=e2b
E2B_DOMAIN=ap-hongkong.tencentags.com   # 你所在地域的接入域名
E2B_API_KEY=...
AGENT_RUNTIME_AIO_TOOL=...              # 或 AGENT_RUNTIME_CODE_TOOL / AGENT_RUNTIME_BROWSER_TOOL
```

地域请选网络能访问到用户所需网站的那个。用户或模型给出的命令绝不会在 Luma 主机上执行；不配置沙箱时，相关工具直接不可用。

## 账号安全

- 密码用 Argon2id 哈希，没有 Argon2 时退回 scrypt。长度要求 8–128 位，常见弱口令会被拒绝。
- 登录失败时不会透露账号是否存在；失败次数按账号和 IP 分别限流。
- 会话保存在服务端（Redis）。浏览器用 HttpOnly cookie，App 用 Bearer 令牌。7 天不使用自动过期，最长不超过 30 天；修改密码后，其他设备会被退出登录。
- **首个管理员**：在服务器 shell 里执行 `python -m app.cli create-admin` 创建。另一种方式是设置 `AUTH_BOOTSTRAP_TOKEN`（至少 16 位），在数据库还是空的时候调用 `POST /api/v1/auth/register`，并带上 `bootstrap_token` 字段；目前自带的客户端还没有填写这个令牌的输入框。不通过命令行、也没有有效令牌的话，任何人都无法抢注首个账号。初始化完成后请删掉这个令牌。
- 所有查询都只限当前登录用户的数据。管理员可以把用户设为或取消管理员、禁用用户，但不能对自己这样做。

## 运维

- **数据库迁移**：启动时自动执行，用 PostgreSQL 咨询锁保证同一时间只有一个进程在迁移。
- **健康检查**：`GET /health` 会报告数据库和 Redis 是否可以连接，容器本身也配置了健康检查。
- **备份与恢复**：`scripts/backup.sh` 生成带时间戳的 `pg_dump`，并把文件卷打包；`scripts/restore.sh` 用来恢复这两部分。
- **镜像**：不发布预构建镜像。`docker compose build` 会用 `Dockerfile` 在本地构建名为 `luma:local` 的镜像；要在别处使用，请打标签后推送到你自己控制的镜像仓库。`.github/workflows/ci.yml` 只运行后端测试和网页端构建。

## 客户端

- **网页端**：后端直接在 `/app` 路径提供。本地开发用 `cd apps/web && npm install && npm run dev`。
- **桌面端**：`cd apps/desktop && npm install && npm start`。用环境变量 `LUMA_SERVER_URL` 或 `config.local.json` 指定服务器地址。打包和代码签名见 [apps/desktop/README.md](apps/desktop/README.md)。
- **iOS**：见 [apps/flutter/README.md](apps/flutter/README.md)。构建时设置 `API_BASE_URL`，并换成你自己的 Bundle ID 和开发者团队。

## 限制

- 沙箱只对接过腾讯云 Agent Runtime 的 E2B 兼容接口，其他服务商没有测试过。
- 没有使用运行时快照和持久卷，因为实测时服务商还不支持。持久化依靠沙箱的暂停和恢复，再加上工作区备份。
- 推送通知需要你自己的 APNs 或 FCM 凭据。
- 界面文字以中文为主。
- 不能操作用户自己的电脑，所有操作都在云端沙箱里进行。
- 模型客户端走 OpenAI Chat Completions 协议（含流式输出和工具调用），这部分由针对模拟服务的测试覆盖。对从源码构建的 Docker 部署做的冒烟测试确认了 `LLM_*` 配置能被正确读取，并确认 `LUMA_PROVIDER=local` 在没有模型服务时可以正常使用；但没有对任何具体厂商做真实推理测试，你选用的模型服务是否兼容，请自行验证。

## 开发

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# 测试需要 PostgreSQL 和 Redis，服务配置可以参考 .github/workflows/ci.yml。
.venv/bin/python -m unittest discover -s tests
```

## 参与贡献

欢迎提 Issue 和 Pull Request。修改行为的改动请附上测试，不要把密钥提交进仓库；提交前请跑一遍后端测试，并在 `apps/web` 下执行 `npm run build`。

贡献者保留自身版权。合并外部贡献前，维护者须取得明确授权，允许按本项目许可分发，并在需要时授予独立商业许可；提交贡献本身不转移版权，也不自动授权维护者另行商业许可。

## 许可证

[Luma 个人非商业使用许可协议 1.0](LICENSE.zh-CN.md) · [英文正式文本](LICENSE)

- **免费使用**：自然人本人的个人非商业自部署、学习、修改和不收取费用的分享，须保留许可及版权声明。
- **须事先书面授权**：公司内部业务、为雇主或客户工作、有偿专业工作、商业产品集成、为商业目的提供的托管/SaaS/API 服务及有偿实施或支持。不向最终用户直接收费，不等于非商业使用。
- **申请商业授权**：通过[许可申请 Issue](https://github.com/meliwanx/luma/issues/new)联系 [meliwanx](https://github.com/meliwanx)。提交申请或未获回复不构成授权，使用范围、费用和责任条款由书面协议确定。

由于限制商业使用，本项目属于**公开源码（source-available）**，不属于 [OSI 定义的开源软件](https://opensource.org/osd)。第三方依赖继续遵循各自许可。

此前按 MIT 发布的版本仍可依原条款使用，本次变更不追溯撤销已授予的权利。旧许可及适用提交保留在 [licenses/LEGACY-MIT.txt](licenses/LEGACY-MIT.txt)。新协议适用于随其发布的版本及新增贡献，但不影响其他独立授权。
