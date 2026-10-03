// 站点身份和读者看得到的文案。FreeTokenHot 行业包。
// 网页和后端都读它；改完重新构建（docker compose up --build）即可生效。
// 域名不在这里：部署时用环境变量 SITE_URL 设置。

export const SITE = {
  /** 站名：导航、页面标题、分享图、RSS、MCP、后台都用它。 */
  name: "FreeTokenHot",
  /**
   * 行业词：拼进默认说法里，比如“免费 API 日报”“免费 API 动态”。
   */
  subject: "免费 API",
  /** 首页的完整标题（浏览器标签、搜索结果）。 */
  homeTitle: "FreeTokenHot — 免费 API 每日精选与日报",
  /** 一句话介绍：搜索引擎、分享卡片、RSS、llms.txt 会用。 */
  description: "每天发现、验证、精选真正免费的 AI API：哪些模型免费、多少免费额度、要不要绑卡，全部写清楚，每天早上出一份日报。",
  /** 首页左上角和侧边栏下面的一行小字。 */
  tagline: "今天又有哪些 API 可以免费薅",
  /** 界面语言（HTML lang、og:locale）。 */
  locale: "zh-CN",
  /** 默认域名，只在没设置 SITE_URL 时使用。 */
  defaultUrl: "http://localhost:3000",
  /**
   * MCP 工具名的前缀（小写字母、数字、下划线），工具会叫 freetokenhot_get_latest、freetokenhot_search……
   * 已经有人接入后就不要再改。
   */
  mcpPrefix: "freetokenhot",
  /** 对外联系邮箱（选填）：使用规则、llms.txt、响应头里会写。 */
  contactEmail: null as string | null,
  /** 页脚的一行小字（选填）。 */
  footerNote: "数据来自 FTH 免费 API 注册表，每日更新",
  /** 中国大陆网站的 ICP 备案号（选填），填了就显示在页脚并链接到工信部备案系统。 */
  icp: null as string | null,
  /** 结构化数据里的网站运营者（搜索引擎用）。 */
  organization: {
    name: "FreeTokenHot",
    /** 创始人（选填）：{ name, url, description }。 */
    founder: null as null | { name: string; url?: string; description?: string },
  },
  /** 抓取信源时报上的名字（User-Agent 里用），不要冒用别的站。 */
  crawlerName: "FreeTokenHotBot",
} as const;

/** 关于页的文案。数字（信源数、收录数、精选数、日报期数）来自站内实时统计，不用写在这里。 */
export const ABOUT = {
  kicker: `关于 ${SITE.name}`,
  /** 大标题：第一行正常颜色，第二行强调色。 */
  headline: ["全网免费的 AI API，", "每天只看这几条。"] as [string, string],
  /** 标题下面的一段话。{sources} 会换成实时的信源数。 */
  lead: `${SITE.name} 替你盯着 {sources} 个信源：发现、验证、打分、精选真正免费的 AI API，每天早上 8 点出一份日报。免费，不用注册。`,
  /** 信源河动画下面的四个环节。 */
  steps: {
    collect: "FTH 采集器每天扫 GitHub、Hacker News、Reddit、X 和各厂商官方文档，找新出的免费 API。",
    store: "每条资源都要经过验证：官方文档作证、免费额度写清、要不要绑卡标明；验证过的才进注册表。",
    select: "按免费力度打分：永久免费最高，体验额度最低；OpenAI 兼容、免绑卡、商用允许都加分。",
    publish: "每天 08:00 出日报：Top 免费榜、新增入库、7 天内到期预警；最值得薅的几条推到邮件和 RSS。",
  },
  /**
   * 作者块（选填），null 就不显示。
   * avatarSourceId：一个 X 账号信源的 id，头像取它的（选填）。
   * 二维码在后台“设置”里上传，或者放进 industry/brand/contact/；没有二维码就不显示那张卡片。
   */
  maker: null as null | {
    name: string;
    greeting: string[];
    avatarSourceId?: string | null;
    wechat?: { title: string; note: string };
    feishu?: { title: string; note: string };
  },
  /** 页面底部的版权与下架说明（结尾会接“反馈页”的链接）。 */
  copyright: `${SITE.name} 是聚合摘要和阅读索引，原文版权归各来源所有。如果你是来源方，希望更正、下架或调整展示方式，可以通过`,
} as const;

/** “免费 API 日报”这类说法：行业词和名词之间，英文词加空格，中文词不加。 */
export function withSubject(noun: string): string {
  return /[A-Za-z0-9]$/.test(SITE.subject) ? `${SITE.subject} ${noun}` : `${SITE.subject}${noun}`;
}
