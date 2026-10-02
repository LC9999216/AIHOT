# 下一步规划：从离线就绪到主线闭环（Roadmap）

**日期:** 2026-09-21
**状态:** 规划；各 Phase 执行前按项目惯例单独授权
**权威顺序:** 安全规则 → 根目录 `AGENTS.md` → `docs/CURRENT_STATUS.md`（release-ready 分支）→ 本规划
**现役代码分支:** `codex/stage2-release-ready` @ `005e2b0`（离线全量 728 passed, 1 skipped）

---

## 一、现状盘点

### 分支拓扑（旧 → 新，全部线性推进，无重写）

| 分支 | HEAD | 内容 |
|---|---|---|
| `main` | `d3780e1` | 仅 stage-1 初版，落后 10+ 提交 |
| `codex/stage2-hardening-fixes` | `ef1d769` | 主 worktree 所在；stage-2 加固 + runner e2e |
| `codex/stage2-hardening-final-fixes` | `72929bc` | closeout 离线验收 |
| `codex/stage2-live-validation-remediation` | `d46e222` | remediation 七项修复闭环（离线报告） |
| `codex/stage2-release-ready` | `005e2b0` | **现役**：生产 LLM 提取接线、ACL 门验证、stage-1 可靠确认修复 |

### 持久化数据（全程未动，受保护）

69 candidates / 0 evidence / 2 UNCERTAIN（AssemblyAI、OpenRouter）/ 0 FREE_CONFIRMED。

### 主线图对照结论

主线 12 个环节的实现已全部存在（Discovery → 证据验证 → LLM 提取 → Free Score → Registry → 飞书通知 → 人工建 Key → 密钥库 → Probe/Canary → VERIFIED → FreeLLMPool → Agent 配置）。

**真正缺口只有一个：整条链从未用真实数据跑通过一次。** 次要缺口：X / Reddit 发现适配器、Claude Code 客户端配置。

---

## 二、Phase 0 — 仓库卫生（先行，约 30 分钟）

1. 删除 `AGENTS.md.pre-astra-20260916.bak` —— 已验证与 HEAD 版本字节一致，纯冗余。
2. 处理 `AGENTS.md` 工作区那 1 行未提交改动（2026-09-16 astra 编辑，测试策略措辞）：
   认可则作为独立 docs commit 提交并注明来源；不认可则 `git checkout -- AGENTS.md` 恢复。不要悬挂。
3. 将两份未跟踪计划文档（`2026-09-16-stage2-security-hardening-closeout.md`、
   `2026-09-17-stage2-live-validation-remediation.md`）与本规划提交入库
   （`codex/stage2-hardening-fixes` 上追加纯 docs commit，不改写既有历史）。

---

## 三、Phase 1 — 五个 Live Gates（核心目标）

按 `docs/CURRENT_STATUS.md` 顺序逐项执行；任一 gate 失败即停、记录、不绕过；
绝不放宽 SSRF 校验，绝不手改 Registry / Evidence / History。

### 运行环境前置（Gate 1 的真正障碍，2026-09-21 实测）

本机 DNS 当前把所有域名解析到 `198.18.0.x` 保留段（TUN 代理 fake-IP 模式，与
CURRENT_STATUS 记录的 codex 沙箱同因）。已核实 `SafeFetcher` 无代理支持
（`fetcher.py` / `config.py` 无 proxy 逻辑），会按规范正确拒绝保留地址。

解法（按优先序，均为环境操作、零代码改动）：

a) 运行 stage-one 期间临时关闭 TUN/fake-IP 模式，或把系统 DNS 切为公共 DNS；
b) 在无 fake-IP DNS 的环境执行（另一台机器 / 云主机 / 直连网络）。

禁止：放宽保留地址校验；为 fetcher 添加代理转发路径（属于对规范的修改，需另行评审）。

### Gate 1 — 证据采集环境

- 动作：`nslookup developers.cloudflare.com` 返回公网地址后，对**外部数据副本**执行
  `hunter run-stage-one --data-dir <外部副本>`（联网采集证据）。
- 验收：外部副本 `evidence.json` 出现真实条目；无因保留地址产生的批量拒绝。

### Gate 2 — 真实 LLM 结构化提取

- 准备：提供 OpenAI 兼容 JSON 模型三元组环境变量
  `HUNTER_LLM_BASE_URL` / `HUNTER_LLM_API_KEY` / `HUNTER_LLM_MODEL`。
- 动作：`run-stage-one --require-llm --data-dir <同一外部副本>`。
- 验收：提取字段逐条带 quote/offset 证据；无效输出走 ≤1 次修复路径并留痕。

### Gate 3 — 确认门槛

- 目标：≥1 个真实 `FREE_CONFIRMED`。推荐 **OpenRouter**：
  信任锚已配置（`openrouter.ai`，reviewed 2026-09-01）、注册表已有 UNCERTAIN 记录、
  OpenAI 兼容、有免费模型、注册即得 key。
- 验收：`confirm_provider()` 八项硬门全过；Verification Confidence ≥ 80；历史事件落盘。
- 红线：若免费条款证据不足无法合法确认，记录并停下——不得手改数据凑数。

### Gate 4 — 人工步骤 + 金丝雀

- 用户注册 OpenRouter 并创建 API key（**唯一人工步骤**）。
- 先跑 `init_live_state.py`（create-only）与密钥目录 ACL 校验；
  Pool Control `freellmpool keys add`（getpass，禁 `--value`）录入 staging。
- 动作：staging health + chat / responses / streaming / tools conformance canaries。
- 验收：金丝雀全过，结果脱敏回传，Key 不出 Pool Control 边界。

### Gate 5 — 全链路与反向验证

- approval binding → promote → 池状态回读一致；
- 生成 Codex / OpenCode 配置指向 `127.0.0.1:8080`，真实调用一次；
- 反向验证：将该 provider 置 UNCERTAIN / 失效 → 下一轮 `run-stage-two`
  suspend-first 自动从生产池摘除并告警；
- 飞书 webhook 真投递（drain 成功才标记 sent）。
- 产出：`docs/` 下 live-validation 报告（每 gate 时间线、命令、退出码、证据摘要）。

### 用户需准备（一次性）

1. OpenAI 兼容模型 env 三元组（Gate 2）；
2. 飞书自定义机器人 webhook URL（Gate 5；环境变量注入，不入库）；
3. （可选）GitHub token（提升 discovery 配额）；
4. OpenRouter 账号与 API key（Gate 4，唯一人工步骤）。

---

## 四、Phase 2 — 合流发布（Gates 全过后）

1. `codex/stage2-release-ready` → PR → `main`；合并前重跑全量离线套件。
2. 清理 worktree：确认无未提交内容后，移除
   `C:/Users/HP/.codex/visualizations/.../free-token-hunter-release-ready`
   及已完成使命的 `-stage2-*` worktree。
3. 打 tag（如 `v0.2.0-live-validated`），更新 `docs/CURRENT_STATUS.md`。

---

## 五、Phase 3 — 补齐主线图缺口（各自需要新 plan 授权）

1. **X / Reddit 发现适配器**：沿用 collector 模式（只产 CandidateObservation，不做验证）。
   Reddit 可用公开 JSON 端点；X 无官方免费 API，无凭证时该适配器默认禁用
   （符合"缺可选凭证只禁用该适配器"原则）。
2. **Claude Code 客户端配置**：⚠️ 先核实 `freellmpool==0.13.0` 是否支持 Anthropic
   messages 协议（closeout 核实表只列了 chat / streaming / tools / responses，均为
   OpenAI 系）。若不支持，Claude Code 无法直接指向 pool 代理——核实结论出来前不规划实现。
3. **DSH / ZCode / 其他 Agent**：通用 Agent 配置已覆盖，Gate 5 一并验证。

---

## 六、Phase 4 — 常态化（可选，闭环之后）

- 定时执行 `run-stage-one` + `run-stage-two`（Windows 任务计划程序或手动周期）。
- autosuspend 计数 + 飞书告警即运营信号（不做 dashboard，AGENTS.md 禁止）。
- 逐步将 UNCERTAIN 池（AssemblyAI 等）与其他已锚定 provider 推进确认。

---

## 七、明确不做

- 不放宽 SSRF / 保留地址校验；不手改 Registry / Evidence / History；
- 不做 dashboard、多账号轮换、限额规避、CAPTCHA 自动化；
- 未获逐项授权前不 push / PR / merge / deploy / 使用真实凭证。
