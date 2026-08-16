# -*- coding: utf-8 -*-
"""财联社电报（7x24 快讯）适配器。

补充信息源，不替代 `src/search_service.py` 的通用新闻搜索——SearXNG 覆盖面更广，
财联社电报时效性更强，两者互补使用。数据经由 akshare 的 ``stock_info_global_cls``
获取，本文件只做字段归一化，不直连财联社的私有接口。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def fetch_cls_telegraph(limit: int = 10) -> List[Dict[str, Any]]:
    """获取财联社最新电报快讯，归一化为通用新闻记录。

    Fail-open：任何异常或空结果都返回空列表，由调用方决定如何降级，不向上抛出。
    """
    if limit <= 0:
        return []
    try:
        import akshare as ak

        df = ak.stock_info_global_cls(symbol="全部")
    except Exception as exc:  # noqa: BLE001 - 上游库/网络异常都视为不可用
        logger.warning("[财联社电报] 获取失败: %s", exc)
        return []

    if df is None or df.empty:
        return []

    records: List[Dict[str, Any]] = []
    for _, row in df.head(limit).iterrows():
        title = str(row.get("标题") or "").strip()
        content = str(row.get("内容") or "").strip()
        if not title and not content:
            continue
        pub_date = str(row.get("发布日期") or "").strip()
        pub_time = str(row.get("发布时间") or "").strip()
        published_date = " ".join(part for part in (pub_date, pub_time) if part)
        records.append(
            {
                "title": title or content[:60],
                "snippet": content,
                "source": "财联社",
                "published_date": published_date,
                "url": "",
            }
        )
    return records
