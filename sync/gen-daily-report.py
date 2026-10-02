#!/usr/bin/env python3
"""FreeTokenHot 每日日报生成器（确定性，无需 LLM）。
结构：Top 免费榜 / 新增入库 / 7 天内到期预警 / 社交发现。
写入 reports(kind='daily', origin='manual')，同时生成 QQ 邮件 HTML/文本。
社交发现：读取 /root/fth-social-monitor/social_verified.json，取过去 24 小时内
官方交叉验证通过的条目，仅展示，不进入 provider 注册表。
"""
import json
import os
import subprocess
import datetime

DIR = "/root/fth-hot-site/sync"
COMPOSE = ["docker", "compose", "exec", "-T", "db", "psql",
           "-U", "aihot", "-d", "aihot"]
SOCIAL_VERIFIED = "/root/fth-social-monitor/social_verified.json"
TZ8 = datetime.timezone(datetime.timedelta(hours=8))


def psql(sql):
    r = subprocess.run(COMPOSE + ["-t", "-A", "-F", "\t", "-c", sql],
                       cwd="/root/fth-hot-site", capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("psql failed: " + r.stderr[:500])
    return r.stdout


def rows(sql):
    return [line.split("\t") for line in psql(sql).strip().split("\n") if line.strip()]


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def item(title, summary, pub_id, docs_url):
    return {"title": title, "summary": summary or "", "itemId": pub_id,
            "sourceName": "FTH 免费 API 注册表", "sourceUrl": docs_url or ""}


def social_discoveries(today):
    """过去 24 小时内官方验证通过的社交监控发现。文件缺失/损坏时返回空，不阻断日报。"""
    cutoff = datetime.datetime.combine(today - datetime.timedelta(days=1),
                                       datetime.time.min, tzinfo=TZ8)
    try:
        if not os.path.exists(SOCIAL_VERIFIED):
            return []
        entries = json.load(open(SOCIAL_VERIFIED, encoding="utf-8"))
    except Exception:
        return []
    items = []
    for e in entries:
        try:
            vat = datetime.datetime.fromisoformat(e.get("verified_at") or "")
        except Exception:
            continue
        if vat < cutoff:
            continue
        parts = ["【%s】%s" % (e.get("tier") or "免费", e.get("quota") or "")]
        if e.get("limits"):
            parts.append("限制：%s" % e["limits"])
        parts.append("有效期：%s" % (e.get("valid_until") or "待确认"))
        # itemId 留空：社交条目无站内详情页，空则标题渲染为纯文本，
        # 避免前端拼出 /items/<reddit_url> 这种坏链。原帖链接走 extraUrl（仅邮件版）。
        items.append({
            "title": e.get("provider_guess") or "未知",
            "summary": "。".join(parts),
            "itemId": "",
            "sourceName": "社交监控（Reddit）",
            "sourceUrl": e.get("official_source_url") or e.get("link") or "",
            "sourceLabel": "官方来源",
            "extraUrl": e.get("reddit_url") or "",
        })
    return items


def main():
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
    key = today.isoformat()

    top = rows(
        "SELECT e->>'title', e->>'heat', e->>'storyPublicId', a.id,"
        " a.raw->'fth'->>'docs_url', p.summary"
        " FROM hot_rankings, jsonb_array_elements(entries) e"
        " JOIN stories s ON s.public_id = (e->>'storyPublicId')::uuid"
        " JOIN story_signals ss ON ss.story_id = s.id"
        " JOIN articles a ON a.id = ss.article_id"
        " JOIN publications p ON p.article_id = a.id"
        " WHERE hot_rankings.published AND rule_version = 'fth-heat-v1'"
        " ORDER BY (e->>'rank')::int LIMIT 10;")

    new = rows(
        "SELECT DISTINCT p.title, p.summary, a.id, a.raw->'fth'->>'docs_url'"
        " FROM articles a JOIN publications p ON p.article_id = a.id"
        " WHERE a.created_at >= now() - interval '24 hours'"
        " ORDER BY 1 LIMIT 20;")

    exp = rows(
        "SELECT p.title, p.summary, a.id, a.raw->'fth'->>'expires',"
        " a.raw->'fth'->>'docs_url'"
        " FROM articles a JOIN publications p ON p.article_id = a.id"
        " WHERE (a.raw->'fth'->>'expires') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'"
        " AND (a.raw->'fth'->>'expires')::date BETWEEN '%s' AND '%s'::date + 7"
        " ORDER BY 4 LIMIT 20;" % (key, key))

    social = social_discoveries(today)

    sections = [
        {"label": "Top 免费榜",
         "items": [item(t[0], "热度 %s。%s" % (t[1], t[5] or ""), t[3], t[4]) for t in top]},
        {"label": "新增入库",
         "items": [item(t[0], t[1], t[2], t[3]) for t in new]},
        {"label": "7 天内到期预警",
         "items": [item(t[0], "有效期至 %s。%s" % (t[3], t[1] or ""), t[2], t[4]) for t in exp]},
        {"label": "社交发现",
         "items": social},
    ]
    lead_para = ("今日免费 API 热榜 Top 10：%s 以 %s 分领跑。" % (top[0][0], top[0][1])
                 if top else "今日暂无榜单数据。")
    if new:
        lead_para += " 新增入库 %d 家。" % len(new)
    if exp:
        lead_para += " %d 家免费额度 7 天内到期，请及时续领。" % len(exp)
    if social:
        lead_para += " 社交监控过去 24 小时新验证 %d 条免费发现。" % len(social)

    content = {
        "lead": {"title": "FreeTokenHot 每日免费 API 日报（%s）" % key,
                 "leadParagraph": lead_para},
        "sections": sections,
    }
    title = "FreeTokenHot 每日免费 API 日报 · %s" % key

    cjson = "'" + json.dumps(content, ensure_ascii=False).replace("'", "''") + "'"
    sql = ("INSERT INTO reports (kind, key, content, origin, model, window_start, window_end, generated_at)"
           " VALUES ('daily', '%s', %s::jsonb,"
           " 'manual', 'fth-deterministic-v1', '%s 00:00:00+08', '%s 23:59:59+08', now())"
           " ON CONFLICT (kind, key) DO UPDATE SET content=EXCLUDED.content,"
           " origin='manual', model='fth-deterministic-v1', generated_at=now(), updated_at=now();"
           % (key, cjson, key, key))
    r = subprocess.run(COMPOSE + ["-v", "ON_ERROR_STOP=1", "-c", sql],
                       cwd="/root/fth-hot-site", capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("report insert failed: " + r.stderr[:500])

    html = ["<h2>%s</h2>" % esc(title), "<p>%s</p>" % esc(lead_para)]
    txt = [title, "", lead_para, ""]
    for sec in sections:
        html.append("<h3>%s</h3><ol>" % esc(sec["label"]))
        txt.append("【%s】" % sec["label"])
        for it in sec["items"]:
            slabel = it.get("sourceLabel") or "官方文档"
            link = (' <a href="%s">%s</a>' % (esc(it["sourceUrl"]), esc(slabel))) if it["sourceUrl"] else ""
            extra = (' <a href="%s">原帖</a>' % esc(it["extraUrl"])) if it.get("extraUrl") else ""
            html.append("<li><b>%s</b>：%s%s%s</li>" % (esc(it["title"]), esc(it["summary"]), link, extra))
            tline = "- %s：%s" % (it["title"], it["summary"])
            if it.get("extraUrl"):
                tline += "（原帖：%s）" % it["extraUrl"]
            txt.append(tline)
        html.append("</ol>")
        txt.append("")
    open("%s/daily-%s.html" % (DIR, key), "w").write("\n".join(html))
    open("%s/daily-%s.txt" % (DIR, key), "w").write("\n".join(txt))
    print("report %s: top=%d new=%d expiring=%d social=%d"
          % (key, len(top), len(new), len(exp), len(social)))


if __name__ == "__main__":
    main()
