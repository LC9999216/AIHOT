import { SITE } from "@aihot/industry/site";
import { pageMeta } from "../lib/seo";
import { prepareCopy } from "../lib/site-copy";
import copy from "@aihot/industry/pages/ranking.md?raw";
import { CopyPage, LegalFooterLinks } from "../features/copy/CopyPage";

const RANKING = prepareCopy(copy);

/** Shared caches may keep this page for five minutes. */
export function headers() {
  return { "Cache-Control": "public, max-age=0, s-maxage=300, stale-while-revalidate=600" };
}

export function meta() {
  return pageMeta({ title: "热度算法", description: `本站免费 API 热度榜的计算公式（${SITE.name} 公开算法）。`, path: "/ranking", image: "/og/pages/ranking.png" });
}

export default function RankingPage() {
  return (
    <CopyPage
      doc={RANKING.doc}
      rendered={RANKING.rendered}
      eyebrow={SITE.name}
      footer={<LegalFooterLinks links={[{ to: "/about", label: "关于" }, { to: "/feedback", label: "反馈页" }]} note={`热度算法 ${RANKING.doc.meta["版本"] ?? ""} · ${RANKING.doc.meta["生效日期"] ?? ""}`} />}
    />
  );
}
