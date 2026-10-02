# Free Token Hunter 剩余工作任务说明书

**日期:** 2026-09-21 晚
**性质:** 交接级任务规范 —— 任何执行者（人或 agent）凭本文件 + 引用文件即可继续
**权威顺序:** 安全规则 → 根目录 `AGENTS.md` → `docs/CURRENT_STATUS.md` → 各 gate 计划 → 本说明书

---

## 一、当前位置（一句话）

主线前半段（发现→验证→评分→注册表）已用**真实互联网数据完整跑通并确认了 2 个 provider**（AssemblyAI、Deepgram，均置信 85）；后半段（飞书→人工建 Key→密钥库→Probe→FreeLLMPool→Agent 接入）代码全部就绪但**尚未执行**；OpenRouter（池化首选候选）的确认还差最后一步诊断。

### 数据现状（外部 live-state 副本）

| 项 | 值 |
|---|---|
| Providers | **AssemblyAI FREE_CONFIRMED**（conf 85 / free_score 30 / rev 3）、**Deepgram FREE_CONFIRMED**（conf 85 / 30 / rev 1）、OpenRouter UNCERTAIN（rev 2，已被处理但未确认） |
| Candidates | 80（含 HN 实时发现的 11 个新候选） |
| Evidence | 105 条 = 8 OFFICIAL + 94 LIKELY_OFFICIAL + 3 REJECTED（TA-008 判定） |
| 仓库受保护 `data/` | 四文件哈希与基线逐字一致，全程未动 |

### 今日修复（全部已提交并推送 GitHub，749 测试全绿）

| 提交 | 内容 |
|---|---|
| `f948f13` | 中文 Windows `icacls` GBK 输出导致 ACL 校验崩溃 → `errors="replace"` 解码 + fail-closed |
| `4d48daa` | gitignore `.venv` |
| `cfcfc9c` | **TA-008**：锚定域名的软 404 错误壳页判 REJECTED（带 3 个防误伤测试） |
| `9dd5d41` | 矛盾裁决前同 URL 快照去重（取最新 retrieved_at；内容寻址 ID 保留审计痕迹） |

今日另修复两个**环境**问题（无代码改动）：LLM 提取超时 30s→600s（ark 上 deepseek-v4-flash 单次真实提取 67-107s+）；运行时 `unset HTTP_PROXY/HTTPS_PROXY`（死代理端口 7890 会坑掉走默认 urllib 通道的 GitHub 采集）。

---

## 二、未完成任务清单（按执行顺序）

### T0 — 第六次流水线收尾诊断【先做，~30 分钟】

**背景:** run 6（含 TA-008 + 同 URL 去重两项修复）17:59 启动，18:30 落盘了状态转换（Deepgram 新确认、OpenRouter rev 2）后进程死亡，输出因 Python 缓冲未刷出而丢失，OpenRouter 未确认的**具体原因不可知**。

**动作:**
1. 确认无残留进程后，以**无缓冲模式**重跑（幂等，已有数据不会重复）：
   ```bash
   cd "D:\AI\Free token\Free Token Hunter-live-run"
   set -a && . ./.env.live && set +a
   unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
   PYTHONUNBUFFERED=1 .venv/Scripts/hunter run-stage-one --require-llm \
     --as-of "$(date -Iseconds)" \
     --data-dir "D:/AI/Free token/Free Token Hunter-stage2-live-state/data" \
     > /tmp/run7.out 2>&1; echo "EXIT=$?" >> /tmp/run7.out
   ```
2. 读取 `grep openrouter /tmp/run7.out` 的 `candidate_outcome` 行。

**验收/分支:**
- 若 `FREE_CONFIRMED` → 直接进 T2（Gate 4）。
- 若给出新的确定性拒绝原因（如置信不足、模糊措辞 -20）→ 如实记录，评估是否值得继续（OpenRouter 免费层措辞确实模糊："free models" + 充值提升限额，LLM 判 ambiguous 属合理）。
- 若 LLM 提取再次失败 → 检查 ark 端点连通/耗时（见风险 R1）。

**注意:** OpenRouter 的 OFFICIAL 证据现为 `/pricing`（最新快照，经同 URL 去重）+ `/docs/api_reference/limits`（次优先级），同级冲突机制上已不可能再触发。

### T1 — Route A：FlClash 修复 + 5 个 LLM 候选补采【广度，非关键路径】

**背景:** groq/mistral/huggingface/jina-ai/google-gemini 五个已锚定候选证据为零——TUN 关闭后直连被墙（10/10 URL 失败）；FlClash 开 TUN 后 DNS 仍 fake-ip（198.18.x），SafeFetcher 按规范拒绝。用户两次尝试启用 redirHost 覆写均未到达内核。

**动作:**
1. **用户侧**: FlClash → 工具 → 覆写 → 顶部"覆写DNS"总开关打开 + 该覆写绑定到当前配置 + 重启核心。自查：`nslookup huggingface.co` 返回非 198.18.x。
2. **机器侧**（DNS 确认真实后）：
   ```bash
   for cid in groq mistral huggingface jina-ai google-gemini; do
     .venv/Scripts/hunter collect-evidence --candidate-id "$cid" \
       --data-dir "D:/AI/Free token/Free Token Hunter-stage2-live-state/data"
   done
   ```
   然后重跑 T0 的流水线命令。
3. `ai.google.dev` 为 Google 域名，即使代理通也大概率受地区限制，失败属预期，不阻塞其他候选。

**验收:** 至少 1 个新候选获得 OFFICIAL 证据；重跑后有新增确认。groq（OpenAI 兼容 + 免费 tier）是最有价值的池化后备。

### T2 — Gate 4：人工注册 + 密钥录入 + 金丝雀【关键路径】

**前置:** 至少一个 **OpenAI 兼容 LLM API** 的 provider 处于 FREE_CONFIRMED（预期 OpenRouter；若 T0/T1 后仍无，则此 gate 被 T1 的 groq/mistral 结果决定）。AssemblyAI/Deepgram 虽已确认但是语音 API，通不过 chat/tools 金丝雀——**不能**用于本 gate。

**动作:**
1. **【用户·唯一人工步骤】** 注册已确认的 provider（如 openrouter.ai），创建 API key。注意 OpenRouter 免费模型部分需要账户内有少量余额，注册后看 Credits 页。
2. 密钥录入——**用户亲自在交互终端执行**（getpass，禁 `--value`，key 不经过 agent）：
   ```bash
   cd "D:\AI\Free token\Free Token Hunter-live-run"
   .venv/Scripts/hunter pool --help    # 确认 key 录入子命令名
   ```
3. 机器侧执行 staging 校验 + 金丝雀（`hunter run-stage-two` 会按序执行）。

**验收:** staging health + chat/responses/streaming/tools 四项 conformance 全过；密钥目录 ACL 校验通过（GBK 修复已解决中文 Windows 崩溃）；key 不出 Pool Control 边界。

### T3 — Gate 5：全链路闭环 + 反向验证 + 飞书【关键路径】

**动作（`hunter run-stage-two` 全生命周期 + 单项验证）:**
1. approval binding → promote → 池状态回读一致（readback-verified）。
2. 生成 Codex / OpenCode 客户端配置指向 `127.0.0.1:8080`，用真实 agent 发一次请求。
3. **反向验证**: 将该 provider 置 UNCERTAIN（通过合规方式：证据变化或手工审批测试通道，**不得直接改数据文件**）→ 下一轮 run-stage-two 必须 suspend-first 自动摘除并告警。
4. 飞书 webhook 真投递（`.env.live` 里的 FEISHU_WEBHOOK_URL；drain 仅投递成功后标记 sent）。

**验收:** 上述四项全部通过 = **主线端到端闭环达成**（互联网→发现→验证→通知→人工→密钥→Probe→池→Agent 真实调用）。

### T4 — Phase 2：合流发布

1. `codex/live-validation-fixes` → PR → `main`（合并前全量离线套件，预期 749+ passed）。
2. 清理 worktree：`Free Token Hunter-live-run`（保留至 T4 完成）、`-stage2-final-fixes`、`-stage2-live-validation`、`-stage2-live-validation-remediation`、`C:/Users/HP/.codex/visualizations/.../free-token-hunter-release-ready`（确认干净后 `git worktree remove`）。
3. 打 tag `v0.2.0-live-validated`；更新 `docs/CURRENT_STATUS.md`。
4. 产出最终 live-validation 报告（每 gate 时间线、命令、退出码、证据摘要）——本说明书的姊妹篇。

### T5 — Phase 3：主线图缺口（各自需新 plan 授权）

| 项 | 说明 | 前置 |
|---|---|---|
| X / Reddit 发现适配器 | 只产 observation 不验证；Reddit 公开 JSON 端点；X 无官方免费 API → 无凭证时该适配器默认禁用 | 新 plan 文档 |
| Claude Code 客户端接入 | ⚠️ **先核实** `freellmpool==0.13.0` 是否支持 Anthropic messages 协议（closeout 核实表只有 chat/streaming/tools/responses，均 OpenAI 系）。不支持则记录为已知限制 | 核实结论 |
| DSH / ZCode 等 | 通用 Agent 配置已覆盖，T3 时一并验证 | — |

### T6 — Phase 4：常态化（可选）

定时执行 run-stage-one + run-stage-two；autosuspend 计数 + 飞书告警作为运营信号；逐步把 UNCERTAIN 池和 groq/mistral 等锚定候选推进确认。**不做 dashboard**（AGENTS.md 禁止）。

---

## 三、已知风险与操作注意

| # | 风险 | 对策 |
|---|---|---|
| R1 | ark（火山引擎）上 deepseek-v4-flash 单次提取 67-107s+，方差大 | `.env.live` 已设 `HUNTER_LLM_TIMEOUT_SECONDS=600`；仍超时则重跑（幂等） |
| R2 | Git Bash 的 `HTTP_PROXY=127.0.0.1:7890` 在代理端口无监听时坑掉 GitHub 采集 | 每次运行前 `unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy`（hunter 的 LLM 客户端不受影响，它显式忽略代理） |
| R3 | DNS fake-ip（TUN 模式）触发 SafeFetcher 合法拒绝 | 采证据时要么关 TUN（Cloudflare 系可直连），要么 redir-host 模式 |
| R4 | 后台运行丢失输出（Python 缓冲） | 一律加 `PYTHONUNBUFFERED=1`（T0 教训） |
| R5 | LLM 提取输出不稳定（run 4 时 deepgram ungrounded、run 6 通过） | 属设计行为（≤1 次修复后拒绝）；重跑即可能通过 |

**安全红线（不可逾越）:** 密钥只经环境变量/getpass；`.env.live` 已被 gitignore，**永不提交、永不贴聊天**；不手改 Registry/Evidence/History；不放宽 SSRF；OpenRouter key 只在 T2 由用户亲自录入。

## 四、环境速查

- **代码 worktree:** `D:\AI\Free token\Free Token Hunter-live-run`，分支 `codex/live-validation-fixes`（已推送 origin）
- **文档分支:** 主 worktree `codex/stage2-hardening-fixes`（已推送 origin）
- **venv:** `...live-run\.venv`（含 `freellmpool==0.13.0`，749 passed 基线）
- **外部数据:** `D:\AI\Free token\Free Token Hunter-stage2-live-state\data`（live 运行专用，仓库 `data/` 是受保护基线）
- **凭证:** `...live-run\.env.live`（LLM 三元组 + GitHub/OpenRouter/飞书 + 超时，均经环境变量注入）
- **完整命令模板:** `docs/plans/2026-09-21-live-run-runbook.md`
