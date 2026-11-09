"""Read-only look at the comments of a brand's Facebook page and Instagram account.

`comments-report` answers "where are the comments and are we allowed to read/answer them" before anything
is built on top. It never posts, hides or likes anything.
"""

from __future__ import annotations

from .platforms.meta import GraphClient, GraphError, check_token_scopes

COMMENT_SCOPES = ["pages_read_user_content", "pages_manage_engagement", "instagram_manage_comments"]


def _pages(g: GraphClient, path: str, fields: str, max_items: int):
    """Items of a Graph list edge, newest first, following the cursors, at most `max_items`."""
    after, seen = None, 0
    while seen < max_items:
        params = {"fields": fields, "limit": "100"}
        if after:
            params["after"] = after
        body = g.call("GET", path, params=params)
        items = body.get("data") or []
        for item in items:
            yield item
            seen += 1
        paging = body.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not items or not paging.get("next") or not after:
            return


def facebook_totals(g: GraphClient, page_id: str, max_posts: int) -> dict:
    return _totals(_pages(g, f"{page_id}/published_posts",
                          "id,created_time,comments.summary(true).limit(0)", max_posts),
                   lambda p: ((p.get("comments") or {}).get("summary") or {}).get("total_count") or 0,
                   "created_time")


def instagram_totals(g: GraphClient, ig_user_id: str, max_posts: int) -> dict:
    return _totals(_pages(g, f"{ig_user_id}/media", "id,timestamp,comments_count", max_posts),
                   lambda m: m.get("comments_count") or 0, "timestamp")


def _totals(items, count, date_field: str) -> dict:
    out = {"posts": 0, "with_comments": 0, "comments": 0, "oldest": None, "top": []}
    for item in items:
        n = count(item)
        out["posts"] += 1
        out["comments"] += n
        out["with_comments"] += 1 if n else 0
        out["oldest"] = (item.get(date_field) or out["oldest"] or "")[:10]
        out["top"].append((n, item["id"]))
    out["top"] = sorted(out["top"], reverse=True)[:5]
    return out


def report(app, secrets, brand_id: str, max_posts: int = 3000) -> list[str]:
    brand = app.brands[brand_id]
    version = app.settings.meta.graph_version
    lines: list[str] = []
    seen_tokens: set[str] = set()
    for platform in ("facebook", "instagram"):
        cfg = brand.platforms.get(platform)
        if cfg is None or cfg.provider != "direct" or not cfg.enabled or not secrets.has(cfg.token_secret):
            lines.append(f"- {platform}: not configured as a direct platform")
            continue
        token = secrets.get(cfg.token_secret)
        g = GraphClient(token, version)
        if cfg.token_secret not in seen_tokens:
            seen_tokens.add(cfg.token_secret)
            lines.append(f"{cfg.token_secret}:")
            scopes = check_token_scopes(token, version, COMMENT_SCOPES)
            lines += ["  " + line for line in scopes if "permission" in line and any(s in line for s in COMMENT_SCOPES)]
        try:
            t = (facebook_totals(g, cfg.page_id, max_posts) if platform == "facebook"
                 else instagram_totals(g, cfg.ig_user_id, max_posts))
        except GraphError as exc:
            lines.append(f"✗ {platform}: {exc}")
            continue
        lines.append(f"✓ {platform}: {t['posts']} post(s) scanned (back to {t['oldest'] or '-'}), "
                     f"{t['with_comments']} with comments, {t['comments']} top-level comments in total")
        lines.append("  most commented: " + ", ".join(f"{n} ({pid})" for n, pid in t["top"]))
    return lines
