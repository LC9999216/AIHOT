# Stage 2 安全准入方案（FreeLLMPool 隔离区）

**分支:** `codex/stage2-hardening`  
**基线:** `main@d3780e1`（372 passed）  
**日期:** 2026-09-12  
**权威顺序:** 安全规则 → 根目录 `AGENTS.md` → 本计划 → 下载目录参考稿（不可直接执行）

本文件是 Stage 2 的最终批准方案。下载目录中的 `AGENTS_STAGE2.md` / `CODEX_TASKS_011_020.md` 仅作待修订参考，不覆盖本计划或 `AGENTS.md`。

---

## 1. 目标链路

```text
FREE_CONFIRMED
→ 飞书同步
→ 通知用户
→ 用户人工注册并创建 Key
→ Key 直接进入 FreeLLMPool 隔离区
→ 真实健康检查与协议测试
→ 确定性准入判断
→ 用户显式批准
→ 加入生产 API Pool
→ Codex / OpenCode / Agent 使用
```

Hunter 永不持有 Provider Key。Key 只存在于 Pool Control 管理的 FreeLLMPool 配置目录。

---

## 2. 已核实的外部接口（禁止臆造）

对 `freellmpool==0.13.0` wheel 的公开接口核实结果：

| 能力 | 0.13.0 事实 | 本方案用法 |
|---|---|---|
| 版本 | PyPI `0.13.0` | 精确固定，不兼容则 fail closed |
| 用户目录 | `FREELLMPOOL_CONFIG` → `providers.toml`；`FREELLMPOOL_CONFIG_FILE` → `config.toml` | staging / production 使用两套独立目录 |
| 密钥写入 | `freellmpool keys add` 用 `getpass`；`--value` 存在但 Hunter/Pool Control **禁止使用** | TTY/getpass only |
| 健康检查 | `freellmpool providers health` / `run_healthcheck()` | Pool Control 内部调用，结果脱敏后返回 |
| 协议金丝雀 | `freellmpool conformance run`：chat / streaming / tools / responses 等 | Pool Control 内部调用 |
| 代理 | `freellmpool proxy --host 127.0.0.1 --port 8080` | 仅 production 监听；staging 不绑定客户端端口 |
| 模型 `enabled=False` | 只跳过 auto 路由，仍可被精确 pin | **不是**真正不可路由 staging |
| 公开 Python API | `Pool`, `configured_providers()`, `load_catalog()` | 只用公开 API / CLI，不 import 私有模块 |

结论：0.13.0 **没有**真正不可路由的 disabled staging。保持三个独立进程、**双实例**：

```text
Hunter 进程
  └─ 不挂载任何 FreeLLMPool 配置，环境中无 Provider Key

Pool Control 进程（loopback + 独立认证）
  ├─ staging FreeLLMPool（配置+Key；不监听客户端端口）
  └─ production FreeLLMPool（仅已批准 Provider；127.0.0.1:8080）
```

若无法安全 promotion，停止并报告。禁止把 Key 回传 Hunter。

---

## 3. 阶段划分与提交策略

每个阶段独立 checkpoint。前一阶段测试失败不得继续。不修改、不提交、不 reset `main`。

| 阶段 | 提交信息 | 内容 |
|---|---|---|
| 0 | （本文件，随阶段一或单独提交） | 批准方案 |
| 1 | `fix(stage1.5): provenance, dns-pinning, github token, llm adapter` | 安全修复 |
| 2 | `feat(runtime): journaled runtime, feishu, outbox` | Runtime / 飞书 / 通知 |
| 3 | `feat(pool): isolated FreeLLMPool control plane` | 秘密区 |
| 4 | `feat(eligibility): health, protocol, approval binding` | 健康/协议/审批 |
| 5 | `feat(stage2): orchestrate, autosuspend, client configs` | 编排与客户端 |

---

## 4. 阶段一：Stage 1.5 安全修复

先写失败测试，再做最小修改。不改 Stage 1 评分权重、状态枚举、官方性优先级、SSRF 端口/方案限制。

### 4.1 Evidence provenance

新增只读结构 `EvidenceProvenance`（`extra=forbid`）：

```text
retrieval_method
original_url
final_url
redirect_chain
http_status
content_sha256
retrieved_from_origin
retrieved_at
```

规则：

- 只能由 `SafeFetcher` 从 `FetchResult` 生成。
- 导入路径（seed / GitHub / HN / 搜索 / 手工 JSON ingest）必须剥离 provenance 与 `OFFICIAL`。
- `retrieved_from_origin=true` 才有资格成为 `OFFICIAL`。
- 第三方 snippet、README、搜索摘要、LLM 文本永远不是 first-party content。

`Evidence` 增加可选 `provenance`。现有空 `data/evidence.json` 与测试构造保持可加载（缺省 `None`）。

### 4.2 官方性每次重算

`OfficialEvidenceValidator.evaluate()`：

- **不再**因已有 `OFFICIAL` 而短路。
- `REJECTED` 仍保持（显式拒绝不应被锚点翻盘）。
- `OFFICIAL` 仅当：trust anchor 匹配 **且** provenance.retrieved_from_origin 为 true **且** `final_url` 主机通过锚点。
- 伪造 `OFFICIAL`、缺少 provenance、导入自声明 provenance 一律失败。

### 4.3 DNS rebinding

`SafeFetcher` 生产路径：

1. 解析主机全部 A/AAAA。
2. 任一非公网地址 → 拒绝。
3. 选定已验证的具体公网 IP 建立连接（IPv4 与 IPv6 都覆盖）。
4. 原域名只用于 `Host` 与 TLS SNI。
5. 每个重定向重新解析并重新验证。
6. 不把连接交给操作系统二次解析。

测试覆盖：检查时公网、连接时私网/回环/IPv6 链路本地；公网 → 恶意重定向到 IPv6 私网。

### 4.4 GitHub Collector

- `GITHUB_TOKEN` 只作为 `Authorization: Bearer` 发给 `api.github.com`。
- 自定义 redirect handler：Location 主机不在官方 API 域 → 中止，不转发 Authorization。
- token 不得进入 observation metadata、日志、异常 `str()`、history、CLI 输出。
- 异常包装时对 URL/reason 做红acted。

### 4.5 真实 LLM adapter

`run-stage-one` 生产路径注入 `OpenAICompatibleClient`：

- 无 tools、无 secrets、无任意网络（只打配置的 base URL）。
- 配置来源：`HUNTER_LLM_BASE_URL` + `HUNTER_LLM_API_KEY`（兼容 `OPENAI_BASE_URL` / `OPENAI_API_KEY`）。
- 缺少配置 → fail closed（不确认任何 Provider）。
- 测试仍注入 Fake extractor，保持离线确定性。

### 4.6 ProviderSetup

`Provider.setup`：

```text
signup_url
api_key_url
setup_instructions_url
```

三个字段均为可选 URL，必须有 grounded citation 才写入。无证据则为 null。不从第三方 registry 复制。

### 4.7 验收

```text
python -m pytest
```

必须 ≥ 当前 372 passed，且新增回归全绿。

允许无外部凭据时继续写后续离线代码；真实 Stage 2 激活前必须完成一次真实官方证据 → `FREE_CONFIRMED`。没有这次验证时，交付只能写“代码及离线验收完成”。

---

## 5. 阶段二：Runtime、飞书、通知

### 5.1 持久化

```text
data/runtime_providers.json     当前 Runtime 快照（稳定 provider_id 排序）
data/runtime_history.jsonl      有意义变更
data/notification_outbox.json   待发送/已发送 outbox
data/.runtime_txn.json          崩溃恢复 journal（gitignore）
```

语义对齐 Registry：

1. 写 journal（old/new revision + 完整 event）
2. 原子替换 snapshot
3. 按 event_id 幂等追加 history
4. 删除 journal
5. no-op 不写文件、不升 revision

崩溃恢复：新 revision 已在 → 补 history 并丢 journal；旧 revision 仍在 → 丢未应用 journal；其他 → fail closed。

### 5.2 Runtime 记录分离

不要把证据状态机与运行时揉在一起。Runtime 记录至少分离：

| 平面 | 含义 |
|---|---|
| credential_status | 是否已在 Pool 配置 Key（Hunter 只见枚举，不见值） |
| health_status | 最近一次健康检查 |
| protocol_status | chat / responses / streaming / tools |
| expected_pool_status | Hunter 计算的期望 |
| actual_pool_status | Pool Control 回报的实际 |
| approval_status | 绑定批准是否仍然有效 |

对外仍可投影 `runtime_status` / `pool_status` 以兼容参考枚举，但存储不得塌缩。

禁止字段：任何 raw key、Authorization、cookie、完整上游响应。

### 5.3 飞书

- 以 `provider_id` 幂等 upsert。飞书不是事实源。
- 写入前读取真实字段结构；完整分页查重；同表串行；单批 ≤ 200。
- Bot 最小权限，只访问指定 Base 与指定会话。
- 载荷只含公开信息、官方链接、状态。严禁 Provider Key。
- 429 / 权限失败 typed + 可重试；不把错误正文中的 token 记入 history。

### 5.4 通知 outbox

- 所有消息先入 outbox，event_id 作为飞书幂等键。
- 用户选择“所有可设置项通知”：每种有效状态变化默认通知。
- 重复运行与 no-op 不通知、不写 history。
- 高优先级：生产暂停失败、Pool 阻断。

---

## 6. 阶段三：FreeLLMPool 独立秘密区

### 6.1 进程与目录

```text
Hunter
  data/, config/          无 Key，无 FreeLLMPool 挂载

Pool Control
  GET  /providers/{id}/status
  GET  /pool/status                 # sanitized loaded provider IDs only
  POST /providers/{id}/probe
  POST /providers/{id}/promote
  POST /providers/{id}/suspend
  仅精确绑定 127.0.0.1，独立 Bearer（HUNTER_POOL_CONTROL_TOKEN）；
  拒绝重定向和系统代理
  响应不得包含 Key、请求头、环境变量、完整上游响应

staging FreeLLMPool
  FREELLMPOOL_CONFIG       = <staging>/providers.toml
  FREELLMPOOL_CONFIG_FILE  = <staging>/config.toml
  不执行 `proxy`，不监听客户端端口

production FreeLLMPool
  独立目录
  由 Pool Control 服务进程以公开 `freellmpool.proxy.serve` API 创建、
  reload、halt 和读取已加载 Provider
  仅已批准 Provider
```

用户通过 Pool Control 交互式终端 `getpass` 输入 Key。禁止 CLI `--value`、禁止 argv、禁止 Hunter 环境变量。

### 6.2 Promotion

Promotion = 把 staging 中已批准 Provider 的**配置副本**写入 production 目录（Key 仍只在 Pool Control 边界内复制），然后校验 production 实际状态。Hunter 只看到 `promoted=true` 或错误码。

若复制/热加载无法在不暴露 Key 的前提下完成：停止，报告 blocker，不降级。

### 6.3 Hardened service boundary

- Hunter 只通过 `PoolControlClient` 调用回环控制 API；它不接收 staging /
  production 路径，也不读取 Pool TOML。
- Pool Control 服务持有并重载 production FreeLLMPool proxy。配置文件回读是
  必要条件；`/pool/status` 返回的 live loaded-provider readback 才是运行态
  对账依据。
- containment halt 在 proxy 停止前原子持久化。HTTP API 无远程 resume；只有
  停止服务后运行 `hunter-pool-control resume --confirm RESUME` 才能清除 latch。
- 控制 API 只接受 `127.0.0.1`，拒绝重定向和环境代理，且不会返回 Provider Key。

---

## 7. 阶段四：健康、兼容、准入、审批

### 7.1 健康检查

- 每次一个固定、非敏感、低 token 请求（对齐 0.13.0 canary：`Reply with exactly OK.` / max_tokens 很小）。
- 明确超时，不自动重试，不并发轰炸。
- 分类：认证失败、限流、额度耗尽、模型不存在、网络错误、未知。
- `429` **不得**直接判定 Key 无效。

### 7.2 协议测试（独立于健康）

必须分别记录：

- Chat Completions
- Responses API
- Streaming
- Tool calling

健康通过 + Responses 失败 ⇒ Codex 拒绝。

### 7.3 客户端准入 profile

| 客户端 | 必须通过 |
|---|---|
| Codex | Responses + Streaming + 所需 tool 协议 |
| OpenCode | 按实际配置的 OpenAI Compatible 或 Responses profile |
| 通用 Agent | 按声明的协议能力 |

### 7.4 审批

```text
python -m hunter pool approve \
  --provider-id <id> \
  --expected-revision <revision>
```

批准绑定：

- Provider ID
- Registry revision
- evidence digest
- Free Score 快照
- eligibility policy version
- 健康及协议结果
- 批准人与时间

任一绑定变化 → 旧批准立即失效。批准前 production 必须对该 Provider 不可路由。

---

## 8. 阶段五：编排、自动暂停、客户端

`hunter run-stage-two` 顺序：

1. 获取单实例锁，恢复未完成事务。
2. 读取**全部** RuntimeProvider，不只是当前 `FREE_CONFIRMED`。
3. 与 Stage 1 Registry 对账。
4. 对已变成 `UNCERTAIN` / `EXPIRED` / `NOT_FREE`、健康失效或批准失效的生产 Provider **先暂停**。
5. 持久化状态、历史、outbox。
6. 导入新的 `FREE_CONFIRMED`。
7. 飞书幂等同步并发送通知。
8. 执行已请求的健康与协议测试。
9. 重新计算 eligibility。
10. 仅对拥有当前有效批准的 Provider 执行 promotion。
11. 再次核对实际 Pool 状态，输出结构化摘要。

暂停失败：阻断生产入口或停止 production Pool，发高优先级告警，不得继续暴露未知 Provider。

调度：Windows Task Scheduler；单实例；上一轮未结束则跳过并记录；明确退出码。Docker Compose 仅可选，不是当前前提。

客户端模板（认证只引用环境变量，不写入模板）：

- Codex 自定义 Provider：`/v1/responses`
- OpenCode Provider
- 通用 OpenAI-compatible Agent

---

## 9. 测试矩阵

离线必须覆盖：

- 伪造 `OFFICIAL` 被拒绝
- DNS rebinding、恶意重定向、IPv6 SSRF
- GitHub Token 不泄漏、不跨域跟随
- Runtime/outbox 每个崩溃点恢复
- Canary Key 不出现在 Hunter 文件/环境/日志/历史/异常/飞书 payload
- Key 已配置但未批准时生产端绝对不可使用
- 健康通过但 Responses 失败时 Codex 被拒绝
- Provider 失效后同一轮暂停并通知
- 飞书分页、重复记录、429、权限失败、幂等发送
- 连续运行两次无重复记录/历史/消息/revision
- 全部离线测试通过

可选 live E2E（明确凭据）：飞书、FreeLLMPool、Codex、OpenCode。无凭据时不得宣称真实链路已上线。

---

## 10. 禁止事项

- 不自动注册、处理 CAPTCHA、邮箱或短信验证
- 不搜索、收集或导入泄漏 Key
- 不把 Key 存进 Hunter、飞书、Git、测试夹具或日志
- 不绕过额度、地域、手机、银行卡或账号限制
- 不修改 Stage 1 评分权重、状态枚举、官方性优先级、SSRF 限制
- 不实现多账号轮换、负载均衡、Dashboard、Redis、PostgreSQL
- 不 fork FreeLLMPool，不 monkey-patch 其私有模块

---

## 11. 数据迁移与兼容性

- `Evidence` 新增可选 `provenance`：旧记录视为 `retrieved_from_origin=false`，不能确认。
- `Provider` 新增可选 `setup`：缺省全 null。
- 现有 `data/providers.json` 继续可加载；FREE_CONFIRMED 若其证据无 provenance，下一次 `run-stage-one` 不得静默继承（需重新 fetch）。
- Runtime 文件为全新文件，无旧格式。
- `.gitignore` 增加 `data/.runtime_txn.json` 与 `.vendor-inspect/`。

---

## 12. 风险与门禁

| 风险 | 处理 |
|---|---|
| 无真实官方 fetch → FREE_CONFIRMED 的 live 验证 | 代码可完成；**不得**声称 Stage 2 已上线 |
| 无飞书 / FreeLLMPool / LLM 凭据 | 仅离线验收 |
| FreeLLMPool 无原生 unroutable staging | 双实例；promotion 失败则 blocker |
| `keys add --value` 存在于上游 CLI | Pool Control 永不调用该参数 |
| 官方性重算可能使旧测试依赖“已有 OFFICIAL 短路” | 更新测试；生产行为以 provenance 为准 |

未通过任一安全门禁时，不得声称 Stage 2 已完成。
