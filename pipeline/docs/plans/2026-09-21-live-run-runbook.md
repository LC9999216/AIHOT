# Live Validation Runbook（2026-09-21）

**执行环境（已就绪）：**
- 代码：worktree `D:\AI\Free token\Free Token Hunter-live-run`，分支 `codex/live-validation-fixes`（基线 `codex/stage2-release-ready@005e2b0` + GBK 修复 `f948f13`）
- venv：`D:\AI\Free token\Free Token Hunter-live-run\.venv`（含 `freellmpool==0.13.0`）
- 外部数据副本：`D:\AI\Free token\Free Token Hunter-stage2-live-state\data`（受保护基线 + discover 新增的 20 条 HN 观察/11 个候选）
- 离线基线：743 passed, 1 skipped（POSIX 位测试在 Windows 跳过，正常）
- 所有命令在 Git Bash 中、以 `D:\AI\Free token\Free Token Hunter-live-run` 为工作目录执行

**已完成并验证：**
1. Gate 2 fail-closed：`--require-llm` 缺 LLM 配置时报错明确、exit 2、不写盘（2026-09-21 实测）。
2. `init_live_state.py --mode verify` exit 0（GBK 修复后）。
3. 发现层 live：`hunter discover` 在 TUN 下通过 HN 采到 20 条真实观察。
4. 受保护仓库 `data/` 四文件哈希全程未动。

---

## Gate 1 — 证据采集（需先调整 DNS）

前置：临时关闭 TUN/fake-IP 模式，或系统 DNS 切为 `223.5.5.5` / `1.1.1.1`。

```bash
nslookup openrouter.ai   # 必须返回公网地址（非 198.18.x），再继续
.venv/Scripts/hunter collect-evidence --data-dir "D:/AI/Free token/Free Token Hunter-stage2-live-state/data"
```

验收：`evidence.json` 出现真实条目，无保留地址批量拒绝。

## Gate 2 — 真实 LLM 提取【需要你】

前置（Git Bash，会话内注入，不入库）：

```bash
export HUNTER_LLM_BASE_URL="https://<你的OpenAI兼容端点>/v1"
export HUNTER_LLM_API_KEY="<key>"
export HUNTER_LLM_MODEL="<json能力模型名>"
```

```bash
.venv/Scripts/hunter run-stage-one --require-llm --as-of "$(date -Iseconds)" \
  --data-dir "D:/AI/Free token/Free Token Hunter-stage2-live-state/data"
```

验收：提取字段带 quote/offset 证据；出现 ≥1 个 `FREE_CONFIRMED`（Gate 3 一并达成）。

## Gate 3 — 确认

包含在 Gate 2 运行内。若免费条款证据不足，停止记录，不手改数据。

## Gate 4 — 人工注册 + 金丝雀【需要你，唯一人工步骤】

1. 你注册 OpenRouter（openrouter.ai）并在控制台创建 API key。
2. 密钥录入（交互式 getpass，禁止 `--value`）：

```bash
.venv/Scripts/hunter pool ...   # 具体子命令以 `hunter pool --help` 输出为准
```

3. staging health + chat/responses/streaming/tools conformance canaries（`run-stage-two` 会执行）。

## Gate 5 — 全链路 + 反向验证【需要你：飞书 webhook】

```bash
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/<你的token>"
.venv/Scripts/hunter run-stage-two --data-dir "D:/AI/Free token/Free Token Hunter-stage2-live-state/data"
```

验收项：promote 回读一致 → Codex/OpenCode 配置指向 `127.0.0.1:8080` 并真实调用一次 →
把该 provider 置 UNCERTAIN 后下一轮 suspend-first 自动摘除 → 飞书收到通知。

## 凭证安全规则（不变）

- 所有 key 仅经环境变量/getpass 注入；不写入任何文件、历史、日志。
- Gate 2/4/5 每步开始前会再次提醒你。
