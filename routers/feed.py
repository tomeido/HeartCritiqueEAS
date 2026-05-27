"""RSS/Atom 피드 - 박제·격차·삭제 신호별 구독."""

from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Request
from fastapi.responses import Response

from services.db import get_db

router = APIRouter()


def _base_url(request: Request) -> str:
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or "localhost"
    )
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{host}"


def _make_atom(
    stories: list,
    base_url: str,
    feed_id: str,
    title: str,
    subtitle: str,
) -> str:
    feed_url = f"{base_url}/feed/{feed_id}.xml"
    home_url = base_url + "/"
    now_iso = datetime.now(timezone.utc).isoformat()
    updated = (
        max((s.get("created_at") or now_iso) for s in stories) if stories else now_iso
    )

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"  <title>{xml_escape(title)}</title>",
        f"  <subtitle>{xml_escape(subtitle)}</subtitle>",
        f'  <link href="{xml_escape(feed_url)}" rel="self" type="application/atom+xml"/>',
        f'  <link href="{xml_escape(home_url)}" rel="alternate" type="text/html"/>',
        f"  <id>{xml_escape(feed_url)}</id>",
        f"  <updated>{xml_escape(updated)}</updated>",
        "  <author><name>Heart &amp; Critique Agent</name></author>",
        '  <generator uri="https://github.com/tomeido/HeartCritiqueEAS">Heart &amp; Critique</generator>',
    ]

    for s in stories:
        sid = s["id"]
        entry_link = f"{base_url}/#story={sid}"
        category = s.get("category") or "unknown"
        category_label = (
            "따뜻한 선행" if category == "kindness" else "인류애가 흔들리는 사건"
        )

        body = s.get("body") or ""
        first_line = body.split("\n")[0].strip()
        entry_title = f"[{category_label}] {first_line[:80]}"
        if len(first_line) > 80:
            entry_title += "…"

        # 신호 라벨
        signal_parts = []
        if s.get("gap_score") == "extreme":
            signal_parts.append("🚨 언론 보도 0건")
        elif s.get("gap_score") == "high":
            signal_parts.append("🔍 보도 격차 큼")
        if s.get("arweave_tx_id"):
            signal_parts.append("🗄 Arweave 박제됨")

        # HTML content 구성
        body_html = (
            body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ).replace("\n", "<br/>")
        parts_html = []
        if signal_parts:
            parts_html.append(f"<p><strong>{' · '.join(signal_parts)}</strong></p>")
        parts_html.append(f"<p>{body_html}</p>")

        cites = s.get("citations") or []
        if cites:
            cite_items = []
            for c in cites:
                uri = c.get("uri", "")
                ctitle = c.get("title", uri)
                cite_items.append(
                    f'<li><a href="{xml_escape(uri)}">{xml_escape(ctitle)}</a></li>'
                )
            parts_html.append("<p>참고 출처:</p><ul>" + "".join(cite_items) + "</ul>")

        if s.get("arweave_url"):
            parts_html.append(
                f'<p><a href="{xml_escape(s["arweave_url"])}">🗄 Arweave 영구 박제 원본 보기</a></p>'
            )

        content_html = "".join(parts_html)
        published = s.get("created_at") or now_iso
        entry_updated = s.get("archived_at") or s.get("created_at") or now_iso
        summary_text = first_line[:200]

        lines.extend([
            "  <entry>",
            f"    <id>{xml_escape(entry_link)}</id>",
            f"    <title>{xml_escape(entry_title)}</title>",
            f'    <link href="{xml_escape(entry_link)}" rel="alternate" type="text/html"/>',
            f"    <published>{xml_escape(published)}</published>",
            f"    <updated>{xml_escape(entry_updated)}</updated>",
            f'    <category term="{xml_escape(category)}"/>',
            f"    <summary>{xml_escape(summary_text)}</summary>",
            f'    <content type="html">{xml_escape(content_html)}</content>',
            "  </entry>",
        ])

    lines.append("</feed>")
    return "\n".join(lines)


def _xml_response(xml: str) -> Response:
    return Response(content=xml, media_type="application/atom+xml; charset=utf-8")


@router.get("/feed/all.xml")
async def feed_all(request: Request):
    db = get_db()
    resp = (
        db.table("stories")
        .select("*")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    xml = _make_atom(
        resp.data or [],
        _base_url(request),
        "all",
        "Heart & Critique — 모든 이야기",
        "AI 사냥개가 커뮤니티 게시판에서 길어 올린 모든 이야기",
    )
    return _xml_response(xml)


@router.get("/feed/archived.xml")
async def feed_archived(request: Request):
    db = get_db()
    resp = (
        db.table("stories")
        .select("*")
        .not_.is_("arweave_tx_id", "null")
        .order("archived_at", desc=True)
        .limit(50)
        .execute()
    )
    xml = _make_atom(
        resp.data or [],
        _base_url(request),
        "archived",
        "Heart & Critique — 박제된 이야기",
        "인간 투표로 Arweave에 영구 박제된 이야기",
    )
    return _xml_response(xml)


@router.get("/feed/extreme.xml")
async def feed_extreme(request: Request):
    db = get_db()
    resp = (
        db.table("stories")
        .select("*")
        .in_("gap_score", ["extreme", "high"])
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    xml = _make_atom(
        resp.data or [],
        _base_url(request),
        "extreme",
        "Heart & Critique — 언론 격차 큰 이야기",
        "메이저 언론에는 안 보이지만 커뮤니티에서 회자되는 이야기 (검열 신호)",
    )
    return _xml_response(xml)


@router.get("/feed/deleted.xml")
async def feed_deleted(request: Request):
    db = get_db()
    checks_resp = (
        db.table("citation_checks")
        .select("story_id")
        .eq("status", "deleted")
        .execute()
    )
    story_ids = list({c["story_id"] for c in (checks_resp.data or []) if c.get("story_id")})
    stories = []
    if story_ids:
        resp = (
            db.table("stories")
            .select("*")
            .in_("id", story_ids)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
        stories = resp.data or []
    xml = _make_atom(
        stories,
        _base_url(request),
        "deleted",
        "Heart & Critique — 출처가 사라진 이야기",
        "원본 글이 이미 삭제·차단되어 사라지는 중인 이야기 (박제 가치 큼)",
    )
    return _xml_response(xml)
