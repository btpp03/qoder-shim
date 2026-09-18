# qoder-shim

把 **Qoder CLI CN** 包装成一个 **OpenAI 兼容的 HTTP 接口**，方便任何现有程序
（SDK、agent 框架、你自己的脚本）调用。

这是一个**协议适配器（protocol adapter）**，不是网络反向代理。下面的
「为什么不是反代」一节解释了这个区别 —— 那才是这个仓库真正想讲清楚的事。

```
your app / SDK  --HTTP-->  qoder_shim.py  --subprocess-->  qodercn -p ...  -->  Qoder 云端
                           (翻译请求)                        (官方 headless 模式)
```

---

## 为什么不是反代

"反代一个 API key" 通常指：某个服务提供 OpenAI 格式的接口，你在前面套一层
转发、加个鉴权。**Qoder 这个场景不成立**，原因如下（都是实测/查文档确认的）：

**1. Qoder 没有开放的 chat/completions 接口**

官方确实有 API，但在 `https://api.qoder.com.cn/api/v1/{cloud,forward}`，
管理的是 **Agents / Sessions / Events / Environments / Vaults** —— 一套
Agent-as-a-Service 编排接口，**没有 `/chat/completions` 这种形态**。

实测未认证访问：

```console
$ curl -s https://api.qoder.com.cn/api/v1/cloud/agents
{"code":"TOKEN_INVALID","message":"missing authorization token","timestamp":"..."}
```

返回 401 说明它确实是正规的 Bearer 认证接口，但**不是聊天接口**。
拿到 PAT 也填不进任何 OpenAI 客户端 —— 两边协议对不上。

**2. 它提供一个本地 CLI，且官方支持非交互调用**

```shell
qodercn -p "<prompt>" --output-format json
```

这正是 CI/CD 场景设计的用法。所以"让别的程序用上这个模型"根本不需要逆向，
**用官方给的口子就行**。

**3. 因此这层东西真正做的事情是「翻译」**

| 方向 | 转换 |
| --- | --- |
| 请求 | OpenAI `messages[]` 数组 → 一个 prompt 字符串 |
| 请求 | `model` 字段 → `--model` 参数 |
| 响应 | CLI 的 JSON 信封 → OpenAI `chat.completion` 对象 |

**理解这层翻译，比跑通它更有价值。** 任何"把非标准来源接进标准协议"的活儿，
都是这个套路。

---

## ⚠️ 重要限制（务必先读）

**① 这是子进程模型，不是真流式**
每个 HTTP 请求会起一个 CLI 进程。实测（fake CLI）约 47ms，但**真实调用要加上
CLI 启动 + 模型推理时间，是秒级**。不支持 token 级流式输出 ——
`stream: true` 的请求会返回一个**完整的非流式响应**（合法但不符合预期）。

**② 字段名是"最佳猜测"**
CLI 的 JSON 输出字段名（`result` / `response` / `text` / `content` / `message`）
我们是**逐个探测**的，命令行参数 `--model` 也是最可能随版本变化的点。
**本仓库的测试用 fake CLI，无法验证真实 CLI 的实际字段名。**
在有 CLI 的机器上第一次跑，请按下面「首次验证」一节核对。

**③ 必须显式指定模型**
CLI 默认走 **Auto 智能路由**，可能选到**收费模型**。想蹭 Qwen3.8-Flash 免费期，
必须显式传 `--model Qwen3.8-Flash`（本 shim 默认已带）。
用 `qodercn --list-models` 核对确切写法。

**④ 活动期限**
Qwen3.8-Flash 免费期：**2026-09-18 10:00 ~ 2026-09-30 23:59:59（UTC+8）**，
计费系数 0.1× → 0.0×。过期后 shim 照常工作，但会消耗 Credits。

**⑤ 使用条款**
把厂商 CLI 包装成对第三方开放的通用 API 端点，官方文档没有明确许可。
用于你自己的自动化，正是 `-p` 模式的设计目的。风险自行判断。

---

## 安装

### 1. 装 Qoder CLI CN

```shell
curl -fsSL https://static.qoder.com.cn/qoder-cli-cn/install.sh | bash
qodercn --version
```

> 建议先把脚本下载下来看一眼再执行，别直接 pipe 进 bash。
> 本项目不在 CI 里自动跑这一步 —— 那是供应商 CDN 的脚本。

### 2. 拿 PAT

打开 <https://qoder.cn/account/integrations> 创建 Personal Access Token
（`pt-` 前缀，**只在创建时显示一次**）。

```shell
export QODERCN_PERSONAL_ACCESS_TOKEN="pt-你的token"
```

CLI 会自动读取这个环境变量完成认证，不会弹交互式登录。

### 3. 跑起来

```shell
python3 qoder_shim.py
```

```
qoder-shim listening on http://127.0.0.1:8787
  base_url = http://127.0.0.1:8787/v1
  model    = Qwen3.8-Flash
```

**只需要 Python 3.9+，零第三方依赖。**

---

## 使用

任何 OpenAI 客户端都可以：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    api_key="dummy",          # shim 默认不校验；见下方 SHIM_AUTH_TOKEN
)

resp = client.chat.completions.create(
    model="Qwen3.8-Flash",
    messages=[{"role": "user", "content": "解释一下快速排序"}],
)
print(resp.choices[0].message.content)
```

curl：

```shell
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3.8-Flash","messages":[{"role":"user","content":"hi"}]}'
```

---

## 配置（全部通过环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QODER_CLI_BIN` | `qodercn` | CLI 可执行文件路径 |
| `QODERCN_PERSONAL_ACCESS_TOKEN` | *（必填）* | CLI 用的 PAT |
| `QODER_DEFAULT_MODEL` | `Qwen3.8-Flash` | 请求未指定 model 时用这个 |
| `SHIM_HOST` | `127.0.0.1` | 监听地址 |
| `SHIM_PORT` | `8787` | 监听端口 |
| `QODER_CLI_TIMEOUT` | `180` | 单次 CLI 调用超时（秒） |
| `SHIM_AUTH_TOKEN` | *（空）* | 设置后客户端需带 `Authorization: Bearer <值>` |
| `QODER_WORKDIR` | 当前目录 | CLI 的工作目录 |

> 所有配置都在**调用时**读取，不是 import 时。这样测试和包装脚本都能用环境
> 变量覆盖，不必改源码。

**别把 `SHIM_HOST` 设成 `0.0.0.0` 而不设 `SHIM_AUTH_TOKEN`** —— 那会变成一个
公开的免费算力中继。

---

## 测试

```shell
# 单元测试 + HTTP 端到端（用 fake CLI，不需要装 Qoder）
python3 test_shim.py

# 用真实的 openai SDK 驱动验证兼容性
pip install openai
python3 interop_check.py
```

两者都**不需要 Qoder CLI、不需要联网、不需要凭据** —— 它们用 fake CLI 验证
翻译逻辑本身。实测输出：

```
Ran 29 tests in 7.5s

OK
```

```
RESULT: ALL INTEROP TESTS PASSED
```

---

## 首次验证（有 CLI 的机器上）

fake CLI 无法证明真实字段名对得上。第一次部署时请核对：

```shell
# 1. 确认可以用 PAT 非交互调用
QODERCN_PERSONAL_ACCESS_TOKEN=pt-xxx qodercn -p "say hi" --output-format json

# 2. 核对模型名
qodercn --list-models

# 3. 核对 --model 参数是否存在
qodercn --help | grep -i model
```

如果输出的 JSON 字段不是 `result`，把实际字段名加进
`qoder_shim.py` 的 `extract_text()` 探测列表；如果 `--model` 不存在，
改 `build_cli_command()`。

---

## 文件

| 文件 | 作用 |
| --- | --- |
| `qoder_shim.py` | shim 本体（纯标准库，单文件） |
| `test_shim.py` | 29 个测试：纯函数 + HTTP 端到端 |
| `interop_check.py` | 用真实 `openai` SDK 验证协议兼容性 |
| `DEPLOYMENT.md` | 能部署在哪、不能部署在哪（含 Clawdi 的详细分析） |

---

## 部署到哪里

**唯一要求：一个能常驻进程 + 能开端口的主机。**

- ✅ 普通 Linux VPS（systemd unit）
- ✅ 支持长驻服务的容器平台
- ❌ **Clawdi（`clawdi.ai`）不行** —— 它只部署 Hermes/OpenClaw Agent 运行时，
  且官方明确把"前台运行进程"列为不持久。详见 [DEPLOYMENT.md](DEPLOYMENT.md)。
- ⚠️ 注意 `clawdi.com` 是无关的待售域名，真站在 `clawdi.ai`

系统级配置示例和公网暴露的安全注意事项都在 [DEPLOYMENT.md](DEPLOYMENT.md)。
