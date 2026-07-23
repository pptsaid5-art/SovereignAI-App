# -*- coding: utf-8 -*-
"""
web_search_local.py
====================
بحث ويب اختياري، يصير بالكامل من جهاز المستخدم مباشرة إلى محرك البحث —
لا يمر عبر أي سيرفر تابع لنا (لا Railway ولا غيره).

مبدأ التصميم:
--------------
- الخيار مُعطَّل افتراضياً (opt-in). المستخدم يفعّله بنفسه من الإعدادات.
- عند التفعيل، طلب HTTP يخرج مباشرة من هذا الجهاز إلى DuckDuckGo فقط
  (لا يحتاج مفتاح API، لا تسجيل، لا اشتراك).
- هذا يكسر مبدأ "لا شيء يخرج من الجهاز" جزئياً وبوعي — لذا الواجهة
  تعرض تحذيراً واضحاً صريحاً قبل أول تفعيل، ويبقى قرار المستخدم دائماً.
- نتائج البحث تُستخدم فقط كسياق إضافي يُحقن للنموذج المحلي (Ollama)،
  تماماً كآلية RAG، لكن مصدرها الويب بدل ملفات الشركة.
"""

import httpx
from typing import List, Dict
from urllib.parse import quote_plus

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
MAX_RESULTS = 5
REQUEST_TIMEOUT_SECONDS = 10.0


async def search_web(query: str, max_results: int = MAX_RESULTS) -> List[Dict[str, str]]:
    """
    يرسل طلب بحث مباشر من هذا الجهاز إلى DuckDuckGo (بدون أي وسيط).
    يرجع قائمة نتائج: [{"title": ..., "snippet": ..., "url": ...}, ...]
    يرجع قائمة فارغة بصمت لو فشل الاتصال (بدون ما يوقف باقي المحادثة).
    """
    if not BS4_AVAILABLE:
        return []

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                DUCKDUCKGO_HTML_URL,
                data={"q": query},
                headers={
                    "User-Agent": "Mozilla/5.0 (SovereignAI-Node local search)"
                },
            )
            if response.status_code != 200:
                return []

            soup = BeautifulSoup(response.text, "html.parser")
            results = []

            for result_div in soup.select("div.result")[:max_results]:
                title_tag = result_div.select_one("a.result__a")
                snippet_tag = result_div.select_one("a.result__snippet, div.result__snippet")

                if not title_tag:
                    continue

                title = title_tag.get_text(strip=True)
                url = title_tag.get("href", "")
                snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

                if title and url:
                    results.append({"title": title, "snippet": snippet, "url": url})

            return results
    except Exception as e:
        print(f"[WebSearch] فشل البحث المباشر من الجهاز: {str(e)}")
        return []


def format_search_context(results: List[Dict[str, str]]) -> str:
    """يحوّل نتائج البحث لنص جاهز للحقن بالـ system prompt، بأسلوب مشابه لسياق RAG."""
    if not results:
        return ""

    blocks = []
    for r in results:
        blocks.append(f"[المصدر: {r['url']}]\nالعنوان: {r['title']}\n{r['snippet']}")

    return "\n\n---\n\n".join(blocks)
