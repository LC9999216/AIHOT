// 可选模块。FreeTokenHot 不需要 AI 行业那两个监控模块，全部关闭。
// 关掉以后：导航里不再出现入口，对应的定时任务不再运行，页面与接口返回 404。

export const FEATURES = {
  /** 模型榜：汇总公开评测，按公开方法 v15 计算共识排名（/leaderboard）。每天抓 4 次评测来源。 */
  leaderboard: false,
  /** Codex 重置监控：盯 OpenAI Codex 负责人在 X 上的额度重置公告（/codex-reset）。需要 SocialData。 */
  codexResetMonitor: false,
} as const;
