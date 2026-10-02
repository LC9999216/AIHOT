# src/hunter/verification/official_finder.py
# WHY: 全自动官方域判定(用户拍板零人工)。搜索是enhancer, 路径猜测是一等公民 (B-6)
from typing import List, Dict

PATH_FALLBACKS = ["/pricing", "/plans", "/docs", "/api", "/rate-limits", "/billing", "/blog"]

def candidate_pages(domain: str, max_pages: int = 3) -> List[str]:
    urls = [f"https://{domain}{p}" for p in PATH_FALLBACKS]
    return urls[:max_pages]

def is_official_domain(candidate_domain: str, page_url: str) -> bool:
    """判断页面是否属于candidate域名或其子域(fully automatic, no human trust anchor)。"""
    if not candidate_domain or not page_url:
        return False
    import urllib.parse
    host = urllib.parse.urlparse(page_url).netloc.lower()
    dom = candidate_domain.lower().lstrip(".")
    return host == dom or host.endswith("." + dom)
