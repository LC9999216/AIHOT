# Stage 2 Live Validation Readiness 修复执行方案

**计划路径：** `D:\AI\Free token\Free Token Hunter\docs\plans\2026-09-17-stage2-live-validation-remediation.md`  
**实现目标仓库：** `D:\AI\Free token\Free Token Hunter-stage2-live-validation`  
**当前分支 / HEAD：** `codex/stage2-live-validation-readiness` / `2c63aaa96463342ee46f3daea2b13b783f173339`  
**起始基线：** `codex/stage2-hardening-final-fixes@72929bc95ff94b3fff728001e977407453020691`  
**建议修复分支：** `codex/stage2-live-validation-remediation`，从 `2c63aaa96463342ee46f3daea2b13b783f173339` 新建，不重写现有六个提交。  
**计划日期：** 2026-09-17  
**状态：** 仅计划；尚未授权实现、live ACL 变更、凭证使用、push、PR、merge 或 deploy。

## 一、目标与完成定义

本轮只修复已经复现或可由当前代码直接证明的 Stage 2 readiness 缺陷，不扩展产品范围。完成后应满足：

1. Pool Control 回读把 Provider 从 `UNKNOWN` 对账为 `PRODUCTION` 后，同一轮必须使用新状态执行 suspend-first；`NOT_FREE`、`UNCERTAIN`、`EXPIRED`、批准失效或绑定失效的 Provider 不得继续留在生产池。
2. Codex、OpenCode、通用 Agent 配置必须使用 `freellmpool==0.13.0` 实际支持的标准路由和公共代理认证，不得用虚构的 `/v1/{provider_id}/...` 路径，也不得在缺少 live Pool readback 时静默生成生产配置。
3. `hunter notifications drain` 必须真实投递并仅在投递成功后标记 sent；缺少适配器或投递失败时保留消息并返回非零退出码。
4. `hunter pool suspend/stop` 必须校验精确确认词，错误确认词不得发起控制 API 请求；`pool stop` 不得把失败返回为退出码 0。
5. Pool Control 本地维护命令必须有可复现的跨进程排他锁测试；`remove-provider` 不得删除仍被其他 Provider 引用的共享 `key_env`。
6. 外部 live-state 初始化必须 create-only / verify-only，不得静默覆盖已有 Registry、Evidence 或 History；密钥目录在录入 Key 前必须通过 Windows ACL / POSIX 权限检查。
7. 仓库保护数据、Stage 1 公共合约和现有提交历史保持不变；所有默认测试离线、确定性、无真实凭证。

## 二、当前已验证基线

- 目标分支与 HEAD：`codex/stage2-live-validation-readiness@2c63aaa96463342ee46f3daea2b13b783f173339`。
- 目标 worktree 当前干净。
- Fresh full suite：`645 passed, 1 skipped`；相关聚焦套件：`38 passed`。绿测不能覆盖下面列出的缺陷。
- `freellmpool==0.13.0` 已安装并精确固定。
- `data/providers.json` 有 2 个 Provider，均非 `FREE_CONFIRMED`；`data/evidence.json` 存在但 `items=[]`。
- 受保护数据相对 `72929bc` 无差异，SHA256 为：
  - `providers.json`: `B885DA24472E10286974AA98030ADB28EA5C66DCF61947B1040B2AECA9A41EBB`
  - `candidates.json`: `AA40A455096FE39B5E7195CD101BAAA45160B8248F9DF0932B62DFBB9819B61D`
  - `evidence.json`: `8B04685F9AA082FB693DC2C04BB566AD81A117BE365D4E4E4D2F2C10F19A6A05`
  - `history.jsonl`: `910D1811363F26A29225EFB27AF0DEFF32C6BBF717E9D0EEDD494D2CA71A0727`
- Windows 本机双进程锁最小复现中，第二个进程被正确阻止；但当前提交内的锁测试没有严格断言这一行为。
- 当前外部 `pool/staging` 和 `pool/production` 继承了 `Authenticated Users: Modify`、`Users: ReadAndExecute`，不得录入真实 Key。

## 三、工作边界

### 3.1 建议工作树与分支

实现获批后再执行：

```powershell
git -C "D:\AI\Free token\Free Token Hunter-stage2-live-validation" status --short --branch
git -C "D:\AI\Free token\Free Token Hunter-stage2-live-validation" rev-parse HEAD
git worktree add "D:\AI\Free token\Free Token Hunter-stage2-live-validation-remediation" -b codex/stage2-live-validation-remediation 2c63aaa96463342ee46f3daea2b13b783f173339
```

若目标分支或 worktree 已存在，停止并先检查，不删除、不覆盖、不复用未知工作树。

### 3.2 允许修改的主要文件

- `src/hunter/runtime/stage2.py`
- `src/hunter/cli_imports.py`
- `src/hunter/runtime/notify.py`
- `src/hunter/client_config.py`
- `src/hunter/pool_api.py`
- `src/hunter/pool_control.py`
- `src/hunter/pool_service.py`
- `src/hunter/runtime/locks.py`（仅在真实跨进程测试证明实现有缺陷时）
- `scripts/init_live_state.py`
- 与上述行为直接对应的 `tests/test_*.py`
- 必要的 Stage 2 运维说明 / progress 文档

### 3.3 受保护文件与禁止事项

未经新授权，不得：

- 修改 `data/providers.json`、`data/candidates.json`、`data/evidence.json`、`data/history.jsonl`；
- 修改 `AGENTS.md`、Stage 1 公共字段/枚举、官方性规则、证据优先级、评分权重/阈值、SSRF 限制、Registry 持久化语义；
- 改写 `main`、`codex/stage2-live-validation-readiness` 或现有提交；
- 删除、刷新或覆盖 `D:\AI\Free token\Free Token Hunter-stage2-live-state` 中的任何数据；
- 读取、创建、输入或使用真实 Provider Key、Feishu webhook、Pool bearer、代理 Key；
- 搜索泄漏凭证、绕过注册/验证码/手机/银行卡/地域/额度限制；
- push、创建 PR、merge、deploy、启动长期运行服务；
- 把离线 fake / fixture 结果写成 live 或 production 验证。

## 四、问题到修复阶段映射

| 优先级 | 问题 | 主要文件 | 核心失败测试 | 完成标准 |
|---|---|---|---|---|
| P0 | 对账后仍用旧 Provider 快照，失效 Provider 同轮未暂停 | `runtime/stage2.py` | Runtime=`UNKNOWN`、Pool live=`PRODUCTION`、Registry=`NOT_FREE`，单次 `run()` 后仍在 production | 同一轮 suspend，Pool readback 为空；不得依赖第二轮 |
| P0 | 客户端 URL 与 FreeLLMPool 0.13.0 不兼容；无 readback 时 fail-open | `client_config.py`, `cli_imports.py`, `pool_api.py` | 生成 `/v1/acme/responses`；无 Pool 环境仍写文件 | 标准 `/v1` 基址、实际 model IDs、统一 proxy auth；无 live readback 不写文件 |
| P0 | `notifications drain` 直接 mark-sent | `cli_imports.py`, `runtime/notify.py` | adapter 未调用但 pending 归零 | 先成功 send，后 mark-sent；失败/未配置保留 pending |
| P0 | Key 目录 ACL 允许普通本机用户读取/修改 | `init_live_state.py`，必要时新增最小权限 helper | Windows temp 目录 ACL 包含 `Users` / `Authenticated Users` | Key 写入前仅当前用户、SYSTEM、Administrators 可访问；无法验证则 fail closed |
| P1 | init 脚本覆盖已有 live data/history | `scripts/init_live_state.py` | destination 与 source 哈希不同后被覆盖 | 默认永不覆盖；verify 报告 mismatch；显式迁移另设授权门 |
| P1 | `--confirm` 值被忽略；stop 失败仍 exit 0 | `cli_imports.py` | `--confirm WRONG` 仍 stop；`halted=false` 返回 0 | 错误词不调用 API；失败非零；输出脱敏 |
| P1 | 锁测试无有效断言；provider-status 声称锁定但未锁 | `pool_service.py`, `test_pool_service.py` | “service running” 测试仍进入 getpass / 允许 0 或 1 | 真双进程锁；所有本地文件维护命令在服务运行时拒绝 |
| P2 | remove-provider 可能删除共享 key_env | `pool_control.py` | A/B 共用 key_env，删除 A 后 B 失去配置 | 仍有引用时保留 Key；返回结构化原因 |

## 五、分阶段实施方案

### 阶段 0：冻结基线并建立审计记录

复杂度：低。依赖：无。

1. 在新修复 worktree 中核对分支、HEAD、状态与 protected-data 哈希。
2. 记录 `python --version`、`freellmpool` 实际版本和当前测试基线。
3. 运行现有完整离线套件，确认缺陷是在绿测下存在，而不是环境故障。
4. 不运行 `init_live_state.py`，不接触真实外部 live-state。

建议命令：

```powershell
git status --short --branch
git rev-parse HEAD
python -c "import importlib.metadata as m; print(m.version('freellmpool'))"
python -B -m pytest -q
```

门禁：HEAD 必须等于 `2c63aaa...`，worktree 必须无未知改动，完整测试应至少保持当前 `645 passed, 1 skipped` 基线。

### 阶段 1：修复同轮 suspend-first

复杂度：中。依赖：阶段 0。

先写失败测试：

1. 使用真实 `PoolControlServer` + `PoolControlClient`，不要直接把 `PoolControl` fake 当作 Client。
2. 创建已批准 RuntimeProvider，但把 Runtime 的 `actual_pool_status` 人为设为 `UNKNOWN`。
3. 让 Pool readback 返回该 Provider 正在 `PRODUCTION`。
4. 把 Registry 状态改为 `NOT_FREE`（再参数化 `UNCERTAIN`、`EXPIRED`）。
5. 单次执行 `Stage2Runner.run()`，RED 断言：
   - `summary.suspended == [provider_id]`；
   - `list_production()` 不再包含该 Provider；
   - Runtime 持久化为非 production 状态；
   - 不发生 re-promotion；
   - 正常 suspend 时不需要 halt 全池。

最小修复：

- `_reconcile_credential_state()` 完成后重新从 `RuntimeStore` 读取 Provider，或让该方法返回对账后的权威列表；
- `_suspend_invalid_first()` 只接收对账后的列表，避免旧对象快照；
- 不改变 Registry / Approval 公共语义。

回归：现有 downgrade、suspend failure、pool/runtime split、幂等二次运行测试全部保持通过。

提交建议：

```text
fix(stage2): suspend invalid providers from reconciled pool state
```

### 阶段 2：修复 operator CLI 的确认与退出码

复杂度：低。依赖：阶段 0，可与阶段 1 独立开发但应串行提交。

先写失败测试：

- `pool suspend --confirm WRONG`：返回非零，`PoolControlClient.suspend()` 调用次数为 0；
- `pool stop --confirm WRONG`：返回非零，`stop_production()` 调用次数为 0；
- `pool stop --confirm STOP` 且响应 `halted=false`：退出 1；
- 响应 `halted=true,error=proxy_halt_failed`：退出 1 并保留结构化错误；
- 正常成功保持退出 0；stdout 不含 token/header/key。

最小修复：

- handler 内再次校验精确字面值，不能只依赖 argparse required；
- 在任何 env 读取、网络连接或 API 调用前拒绝错误确认词；
- stop 仅在 `halted is True` 且无错误时返回 0。

提交建议：

```text
fix(cli): enforce operator confirmations and truthful exit codes
```

### 阶段 3：把 notifications drain 改成真实投递

复杂度：中。依赖：阶段 0。

先写失败测试：

1. 无 `HUNTER_FEISHU_WEBHOOK_URL`：返回配置错误，pending 数量与内容不变。
2. adapter send 成功：调用一次后才 mark-sent；第二次运行不重复发送。
3. adapter 失败：消息保持 pending，attempts / next_retry_at 按既有规则更新，退出非零。
4. HTTP 200 但 Feishu 应用层返回失败：不得 mark-sent。
5. retry 尚未到期 / 超过最大次数：输出 `skipped_retry`，不得伪报 sent。
6. 并发 drain 仍由 OutboxStore 锁保护，不重复发送同一 event_id。

最小修复：

- CLI 构造 `WebhookFeishuAdapter` 与 `OutboxConsumer`，调用 `process_once()`；
- 删除直接循环 `mark_sent()` 的路径；
- 输出 `sent`、`failed`、`skipped_retry` 计数与 event IDs；
- 建议退出码：成功/空队列 `0`，发生投递失败 `1`，适配器未配置 `2`；
- 对照当前官方自定义机器人文档，使用被支持的 payload 字段和成功响应结构；不能把任意 HTTP 200 当成功。

范围说明：本阶段只修 webhook 通知，不把飞书 Base upsert 偷渡进本次修复；最终报告须写“Webhook 离线接线完成”，不能写“Feishu 全链路完成”。

提交建议：

```text
fix(notifications): deliver outbox before marking messages sent
```

### 阶段 4：重做 client-config 的权威输入与路由

复杂度：高。依赖：阶段 0；应在 live E2E 前完成。

#### 4.1 先固定真实接口事实

在写实现前，用本机安装的 `freellmpool==0.13.0` 和当前客户端官方/本地文档确认：

- FreeLLMPool 标准端点为 `/v1/responses`、`/v1/chat/completions`，不是 `/v1/{provider_id}/...`；
- Provider pinning 使用实际 `provider/model` model ID；
- Pool proxy 使用统一 proxy API key，绝不能把上游 Provider Key 暴露给客户端；
- Codex `base_url`、`wire_api`、`env_key` 的当前字段语义；
- OpenCode 的 baseURL、模型 ID 和环境变量引用语法。

把这些事实固化为测试 fixture / assertions，不依赖注释或猜测。

#### 4.2 失败测试

1. 所有客户端 base URL 均不能包含 `/{provider_id}` 路径段。
2. Codex 不能生成会重复追加 `/responses` 的 base URL。
3. OpenCode / Agent 必须把真实 production model IDs 写入模型目录，而不是虚构 `<provider>-default`。
4. 认证只引用统一 proxy-key 环境变量名，不能引用 Provider credential env。
5. 缺少 Pool Control URL/token、readback 失败、readback schema 异常或版本不兼容时：退出非零且三个输出文件均不创建/不更新。
6. Runtime 声称 production、但 Pool live catalog 不含该 Provider 时：不得生成该 Provider / model。
7. 用 FreeLLMPool 0.13.0 的实际 route matcher 或内存 HTTP server 验证生成端点不返回 route-level 404；不能只做 JSON/TOML/YAML parse 测试。

#### 4.3 最小设计

- 把客户端视为连接到一个 FreeLLMPool production proxy，而不是通过虚构 URL 连接到每个上游 Provider；
- 扩展 Pool Control 的脱敏 readback，只返回 live loaded Provider IDs 和公开 model IDs，不返回路径、Key、headers、环境值或上游响应；
- CLI 强制要求 Pool Control readback，移除生产 CLI 的 `None` fallback；纯生成函数可保留显式 fixture 模式，但不得由普通 CLI 静默触发；
- 先生成并验证全部三个文档，再原子替换输出；任一失败不得留下部分新配置；
- 维持 `freellmpool==0.13.0` 精确版本门禁。

提交建议：

```text
fix(client-config): generate configs from live pool catalog
```

### 阶段 5：修复本地维护命令与锁测试

复杂度：中。依赖：阶段 0。

先写失败测试：

- 子进程 A 持有 `.pool-control-service.lock`，子进程 B 执行 `register-provider`、`enter-key`、`provider-status`、`remove-provider`，全部明确返回 `pool_control_service_running`；
- 测试使用进程间 pipe/event 判断 lock ready，不使用不确定 sleep；
- `enter-key` 被锁阻断时，secret reader / getpass 调用次数必须为 0；
- 失败测试不得只断言“没有打印 sk-”或接受 `(0, 1)`；
- A/B 两个 Provider 共享同一 `key_env`，删除 A 后 B 仍 `key_configured=true`；
- 重新注册 Provider 改变 key_env 时，旧 Key 只在无剩余引用时删除。

最小修复：

- 所有直接读取/修改 staging/production 文件的本地命令使用相同 service lock；服务运行时的状态查询走 `hunter pool control-status` HTTP API；
- `remove_provider()` 删除 Key 前检查剩余 Provider 引用；返回 `key_removed` 与结构化保留原因；
- 如果真实双进程测试证明现有 `ProcessFileLock` 在目标 Windows 环境已经正确，不改锁实现，只修测试与调用点。

提交建议：

```text
fix(pool): enforce maintenance locking and shared-key safety
```

### 阶段 6：把 external live-state 改成安全 create/verify

复杂度：高（涉及 Windows ACL）。依赖：阶段 0；修改真实目录另需授权。

#### 6.1 测试先行

所有测试仅使用临时目录，不触碰 `D:\AI\Free token\Free Token Hunter-stage2-live-state`。

- destination 不存在：初始化四个数据文件和目录，内容与 source 一致；
- destination 已存在且哈希一致：no-op，不刷新 mtime；
- destination 已存在且哈希不同：保持原文件，返回 `mismatch` / 非零，不覆盖；
- history 已追加 live event：再次 init 不得截断；
- 拷贝中断：不得留下半文件；
- 输出 JSON 中 `copied` 是数组，不是字符串；报告完整 SHA256；
- Windows：ACL 中不得保留 `Authenticated Users` Modify 或 `Users` Read；
- POSIX：secret 目录模式为 `0700`、secret 文件为 `0600`；
- ACL/权限无法设置或验证：初始化失败且不允许进入 `enter-key` / `serve`。

#### 6.2 最小修复

- `init(worktree_root, state_root, mode)` 注入目标路径，默认路径只作为 CLI 默认值，便于可靠测试；
- 默认行为 create missing + verify existing，永不 refresh；
- 如未来确需 refresh，必须另设显式子命令、备份、逐文件确认和新的用户授权，本轮不实现；
- 初始复制采用同目录临时文件、fsync、`os.replace`；
- Windows 密钥目录关闭继承，仅保留当前用户、SYSTEM、Administrators；POSIX 使用最小权限；
- `enter-key` 和 Pool Control `serve` 自身也要验证 secret directory 权限，不能只依赖初始化脚本；
- 为既有 live-state 提供只读 `--verify` / `--dry-run`。实际 ACL apply 不在本轮自动执行。

提交建议：

```text
fix(live-state): prevent overwrite and enforce secret-directory ACLs
```

### 阶段 7：文档、完整回归与独立复核

复杂度：中。依赖：阶段 1–6 全绿。

1. 更新 Stage 2 progress/closeout，逐项写 completed / partial / unverified。
2. 把“无 evidence.json”改为“evidence.json 存在但 0 条 evidence”。
3. Windows 锁写成当前主机已通过真实双进程测试，并注明跨主机仍需 CI / 目标机复核。
4. 报告精确测试命令与结果，不使用 `618+`、`201+` 等模糊计数。
5. 做一次独立代码审查，重点检查：状态时序、API fail-closed、通知幂等、proxy auth、ACL、路径覆盖和秘密边界。

最终离线门禁：

```powershell
python -B -m pytest -q tests/test_stage2_runner.py
python -B -m pytest -q tests/test_pool_service.py tests/test_pool_control_fixes.py tests/test_pool_proxy.py
python -B -m pytest -q tests/test_runtime_outbox.py tests/test_runtime_notify.py
python -B -m pytest -q
python -B -m pytest -q
python -B -m compileall -q src
python -m hunter --help
python -m hunter pool --help
python -m hunter notifications --help
python -m hunter client-config --help
python -m hunter.pool_service --help
git diff --check 2c63aaa96463342ee46f3daea2b13b783f173339..HEAD
git status --short --branch
```

还要重新计算四个 protected-data SHA256，并确认完全等于本计划第二节基线。

## 六、Live validation 单独准入门

离线修复完成不等于 live 就绪。必须按以下顺序，逐门停止：

1. **代码门：** 阶段 1–7 全绿；独立审查无 HIGH/CRITICAL。
2. **状态目录门：** 对真实 external live-state 先只读 ACL/hash 检查；应用 ACL 需要用户明确授权。ACL 未通过时禁止录入 Key。
3. **Stage 1 事实门：** 在外部数据副本运行真实 Stage 1；必须合法得到至少一个带有效 OFFICIAL evidence 的 `FREE_CONFIRMED`。仍为 0 时停止，不造数据、不绕门禁。
4. **凭证门：** 用户对具体 Provider 和具体用途显式授权后，才可通过 Pool Control `getpass` 输入真实 Key；禁止 argv/env/Git/日志。
5. **Canary 门：** 先 staging health，再分别验证 chat、responses、streaming、tools；结果必须来自真实 FreeLLMPool 0.13.0。
6. **批准门：** 绑定当前 registry revision、evidence digest、score snapshot、policy version、真实 protocol 结果后由用户显式批准。
7. **Promotion 门：** promotion 后必须 live readback；任何 split 立即 halt，不生成客户端配置。
8. **客户端门：** 用生成配置分别完成 Codex、OpenCode、通用 Agent 的真实请求；parse 成功不能替代互操作测试。
9. **失效门：** 把外部测试副本中的 Provider 合法降级，验证同轮 suspend、通知、客户端移除、halt/resume。
10. **Feishu 门：** 在明确 webhook 授权后验证成功、应用层失败、429/重试和幂等；以官方自定义机器人响应为准。

任一门未通过即停止。不得以手改 Registry、伪造 evidence、禁用 approval binding、手写 production TOML 或跳过 ACL 的方式继续。

## 七、建议提交链

保持每个提交单一职责，顺序如下：

```text
fix(stage2): suspend invalid providers from reconciled pool state
fix(cli): enforce operator confirmations and truthful exit codes
fix(notifications): deliver outbox before marking messages sent
fix(client-config): generate configs from live pool catalog
fix(pool): enforce maintenance locking and shared-key safety
fix(live-state): prevent overwrite and enforce secret-directory ACLs
docs(stage2): record repaired gates and remaining live validation
```

每个行为提交均要求：先运行新增测试看到预期 RED，再做最小实现到 GREEN，再运行相关回归。不要为了整理代码做无关重构。

## 八、最终交付报告要求

最终报告必须包含：

- 仓库、worktree、分支、HEAD、起始点和完整提交链；
- 每个发现 → 修改文件 → 失败测试 → 修复后测试的映射；
- 所有实际执行命令、精确 passed/skipped/failed 数量；
- protected-data 新旧 SHA256；
- 是否接触 external live-state、ACL、凭证、网络与真实服务；
- completed / partial / unverified 分类；
- 仍未通过的 live gates 与停止原因；
- 明确的 push / PR / merge / deploy / credential-use 声明；
- 干净 Git 状态；
- 若任何安全门失败，不得写“Stage 2 完成”或“production ready”。

## 九、Agent handoff prompt

```text
请直接执行修复，不要重新规划，也不要修改本计划的范围。

计划文件：
D:\AI\Free token\Free Token Hunter\docs\plans\2026-09-17-stage2-live-validation-remediation.md

目标源 worktree / 分支 / HEAD：
D:\AI\Free token\Free Token Hunter-stage2-live-validation
codex/stage2-live-validation-readiness
2c63aaa96463342ee46f3daea2b13b783f173339

请从该 HEAD 新建：
- worktree: D:\AI\Free token\Free Token Hunter-stage2-live-validation-remediation
- branch: codex/stage2-live-validation-remediation

不要重写现有六个提交，不要修改 main。先核对 HEAD、Git 状态、AGENTS.md、计划文件和 protected-data hashes。

核心目标：按计划依次修复同轮 suspend-first、operator confirm/退出码、outbox 真实投递、FreeLLMPool 0.13.0 客户端配置、Pool 本地维护锁/共享 key_env，以及 external live-state 的 no-overwrite/ACL 安全。严格先写最小失败测试，运行确认 RED，再做最小实现到 GREEN；每阶段运行聚焦回归，最后运行完整 pytest、compileall、diff --check、CLI help、protected hash 和 Git status。

受保护内容：
- data/providers.json
- data/candidates.json
- data/evidence.json
- data/history.jsonl
- AGENTS.md
- Stage 1 公共合约/枚举/证据优先级/信任锚/评分/SSRF/Registry 持久化语义
- D:\AI\Free token\Free Token Hunter-stage2-live-state 的现有内容与 ACL（仅可只读检查；实际 ACL apply 需用户新授权）

禁止：真实凭证、真实 webhook、手改 Registry/evidence、门禁绕过、删除/覆盖 live state、push、PR、merge、deploy、修改 main、无关重构。不要把 fake/fixture/parse-only 测试写成 live 验证。

客户端配置必须先核对安装的 freellmpool==0.13.0 真实路由和当前客户端配置语义；不得继续使用 /v1/{provider_id}/... 假路由，也不得在缺少 Pool live readback 时生成生产配置。通知只能在 adapter 成功后 mark-sent。

最终交付按计划第八节输出：提交链、finding→file→test、精确命令结果、protected hashes、完成/部分/未验证、剩余风险、live gates，以及 push/PR/merge/deploy/credential-use 声明。任一安全门失败时停止并明确报告，不得宣称 Stage 2 完成。
```
