"""Live-list-shaped fixtures for new community collectors; no network or database."""

import pytest

from services.community_sources import COMMUNITY_SOURCES, parse_html_source


CASES = [
    ("dcinside", '<td class="gall_tit ub-word">{anchor}</td>',
     "/board/view/?page=2&id=dcbest&no=123&_dcbest=1",
     "https://gall.dcinside.com/board/view/?id=dcbest&no=123"),
    ("humoruniv", '<td width="295" class="li_sbj">{anchor}</td>',
     "read.html?table=pds&number=123&page=2",
     "https://web.humoruniv.com/board/humor/read.html?table=pds&number=123"),
    ("todayhumor", '<td class="subject">{anchor}</td>',
     "/board/view.php?table=bestofbest&no=123&s_no=123&page=2",
     "https://www.todayhumor.co.kr/board/view.php?table=bestofbest&no=123"),
    ("slrclub", '<td class="sbj">{anchor}</td>',
     "/bbs/vx2.php?id=free&no=123&page=2",
     "https://www.slrclub.com/bbs/vx2.php?id=free&no=123"),
    ("ddanzi", '<td class="title">{anchor}</td>',
     "https://www.ddanzi.com/free/123?page=2", "https://www.ddanzi.com/free/123"),
    ("dogdrip", '<h5 class="ed title">{anchor}</h5>',
     "/dogdrip/123?sort_index=popular&page=2", "https://www.dogdrip.net/123"),
    ("cook82", '<td class="title">{anchor}</td>',
     "read.php?bn=15&num=123&page=2", "https://www.82cook.com/entiz/read.php?bn=15&num=123"),
    ("fmkorea", '<h3 class="title" data-title-ellipsis="true">{anchor}</h3>',
     "/best/123?page=2", "https://www.fmkorea.com/123"),
    ("inven", '<div class="board-list"><td class="subject">{anchor}</td></div>',
     "/board/webzine/2097/123?p=2", "https://www.inven.co.kr/board/webzine/2097/123"),
    ("ruliweb", '<table class="board_list_table"><tr class="mode_list"><td class="subject">{anchor}</td></tr></table>',
     "/best/board/300143/read/123?m=humor&t=now", "https://bbs.ruliweb.com/community/board/300143/read/123"),
    ("quasarzone", '<div class="v2-list-row"><p class="tit">{anchor}</p></div>',
     "/bbs/qb_free/views/123?page=2", "https://quasarzone.com/bbs/qb_free/views/123"),
]


def anchor(href, title="새 <b>소식</b> &amp; 이야기", attrs=""):
    return f'<a class="title-link subject-link subject_link" href="{href}" {attrs}>{title}</a>'


def wrapped(wrapper, href, title="새 <b>소식</b> &amp; 이야기", attrs=""):
    return wrapper.format(anchor=anchor(href, title, attrs))


@pytest.mark.parametrize("parser,wrapper,href,canonical", CASES)
def test_titles_canonical_links_and_duplicate_items(parser, wrapper, href, canonical):
    html = "<table><tr>" + wrapped(wrapper, href) + "</tr><tr>"
    html += wrapped(wrapper, canonical, "중복") + "</tr></table>"
    assert parse_html_source(parser, html.encode()) == [{
        "title": "새 소식 & 이야기", "url": canonical, "guid": canonical, "summary": None,
    }]


@pytest.mark.parametrize("parser,wrapper,href,canonical", CASES)
def test_notices_and_fragment_comment_links_are_excluded(parser, wrapper, href, canonical):
    notice = wrapped(wrapper, href).replace('class="mode_list"', 'class="mode_list notice"')
    html = '<table><tr class="notice pinned">' + notice + "</tr>"
    html += "<tr>" + wrapped(wrapper, href + "#comment", "댓글 20개") + "</tr></table>"
    assert parse_html_source(parser, html.encode()) == []


@pytest.mark.parametrize("parser,wrapper,href,canonical", CASES)
@pytest.mark.parametrize("host", ["attacker.test", "www.dogdrip.net.attacker.test", "127.0.0.1"])
def test_off_host_article_links_are_never_collected(parser, wrapper, href, canonical, host):
    from urllib.parse import urlsplit
    target = urlsplit(canonical)
    malicious = f"https://{host}{target.path}?{target.query}"
    assert parse_html_source(parser, wrapped(wrapper, malicious).encode()) == []


@pytest.mark.parametrize("parser,wrapper,href,canonical", CASES)
def test_empty_or_hidden_titles_do_not_consume_capture_budget(parser, wrapper, href, canonical):
    html = wrapped(wrapper, href, '<img src="icon.gif"><span aria-hidden="true">장식</span>')
    assert parse_html_source(parser, html.encode()) == []


def test_dcinside_notice_data_type_and_comment_query():
    template = '<td class="gall_tit">{anchor}</td>'
    html = '<tr data-type="icon_notice">' + wrapped(template, "/board/view/?id=dcbest&no=1") + "</tr>"
    html += "<tr>" + wrapped(template, "/board/view/?id=dcbest&no=2&t=cv", "[10]") + "</tr>"
    html += "<tr>" + wrapped(template, "/board/view/?id=other&no=3") + "</tr>"
    assert parse_html_source("dcinside", html.encode()) == []


def test_humoruniv_cp949_decoding_without_comment_count():
    html = """<meta charset="euc-kr"><tr><td class='li_sbj'>
        <a style='color:black' href='read.html?number=456&amp;table=pds' class='li'>
            <span id='title_chk_pds-456'>한글 제목과 &quot;인용&quot;</span>
            <span class='list_comment_num'> [99]</span>
        </a></td></tr>"""
    items = parse_html_source("humoruniv", html.encode("cp949"))
    assert items[0]["title"] == '한글 제목과 "인용"'
    assert items[0]["url"].endswith("table=pds&number=456")


def test_slr_help_and_notice_board_are_not_free_posts():
    html = '<td class="sbj"><a href="/bbs/vx2.php?id=help&no=32">관리규정</a></td>'
    html += '<td class="sbj"><a href="/bbs/vx2.php?id=notice&no=277">공지</a></td>'
    assert parse_html_source("slrclub", html.encode()) == []


def test_82cook_notices_and_sidebar_do_not_mix_with_free_board():
    html = '<tr class="noticeList"><td class="title"><a href="read.php?bn=15&num=1">공지</a></td></tr>'
    html += '<li><a href="/entiz/read.php?num=2" title="인기글">다른 영역</a></li>'
    html += '<td class="title"><a href="read.php?bn=14&num=3">다른 게시판</a></td>'
    assert parse_html_source("cook82", html.encode()) == []


def test_fmkorea_query_permalink_dedup_and_comment_decoration():
    html = '<h3 class="title"><a href="/index.php?mid=best2&amp;document_srl=123&amp;sort_index=pop">'
    html += '소식 <span class="reply_count">[100]</span></a></h3>'
    html += '<h3 class="title"><a href="/best/123">중복</a></h3>'
    html += '<a class="pc_voted_count" href="/best/789">추천 200</a>'
    items = parse_html_source("fmkorea", html.encode())
    assert len(items) == 1
    assert items[0]["title"] == "소식"
    assert items[0]["url"] == "https://www.fmkorea.com/123"


@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "file:///dogdrip/123", "https://evil@www.dogdrip.net/123",
    "https://www.dogdrip.net:9000/123", "https://www.dogdrip.net:invalid/123",
])
def test_unsafe_url_forms(href):
    assert parse_html_source("dogdrip", anchor(href).encode()) == []


def test_duplicate_query_ids_are_rejected():
    html = '<td class="gall_tit"><a href="/board/view/?id=dcbest&no=123&no=456">글</a></td>'
    assert parse_html_source("dcinside", html.encode()) == []


def test_unknown_parser_and_empty_input_are_safe():
    assert parse_html_source("unknown", b"<html><a href='/123'>Title</a></html>") == []
    assert parse_html_source("dcinside", b"") == []


def test_catalog_preserves_existing_sites_and_describes_paused_sites():
    ids = [s["id"] for s in COMMUNITY_SOURCES]
    assert len(ids) == len(set(ids))
    existing = {"ppomppu.co.kr", "ruliweb.com", "mlbpark.donga.com", "inven.co.kr",
                "clien.net", "bobaedream.co.kr", "theqoo.net", "pann.nate.com"}
    # Preserve catalog entries while newly observed access limits pause polling.
    assert existing <= {s["domain"] for s in COMMUNITY_SOURCES}
    for source in COMMUNITY_SOURCES:
        assert source.keys() >= {"id", "name", "domain", "board", "url", "kind", "parser", "enabled", "note"}
        assert source["kind"] in {"rss", "html"}
        assert source["url"].startswith("https://")
        if not source["enabled"]:
            assert source["note"]
    instiz = next(s for s in COMMUNITY_SOURCES if s["domain"] == "instiz.net")
    assert not instiz["enabled"]
    assert "403" in instiz["note"]


def test_ruliweb_board_scope_pinned_and_sidebar_links():
    row = '<tr class="mode_list {extra}"><td class="subject">{anchor}</td></tr>'
    html = row.format(extra="best_top_row", anchor=anchor('/best/board/300143/read/1'))
    html += row.format(extra="", anchor=anchor('/best/board/300007/read/2'))
    html += anchor('/best/board/300143/read/3')
    assert parse_html_source("ruliweb", html.encode()) == []


def test_new_sources_ignore_other_boards_and_advertising_links():
    html = '<div class="board-list">' + anchor('/board/webzine/2097/5') + '</div>'
    html += '<div class="board-list">' + anchor('/board/webzine/2098/6') + '</div>'
    html += anchor('/board/webzine/2097/7')
    assert len(parse_html_source('inven', html.encode())) == 1
    html = '<div class="v2-list-row">' + anchor('/bbs/qb_free/views/5') + '</div>'
    html += '<div class="v2-list-row">' + anchor('/bbs/qb_saleinfo/views/6') + '</div>'
    html += anchor('/bbs/qb_free/views/7')
    assert len(parse_html_source('quasarzone', html.encode())) == 1


def test_restricted_sources_are_cataloged_without_enabling_requests():
    sources = {s['id']: s for s in COMMUNITY_SOURCES}
    for ident in ('slrclub-free', 'ddanzi-free', 'fmkorea-best', 'mlbpark-bullpen',
                  'pann-ranking', 'dmitory-issue', 'damoang-free', 'arca-best',
                  'etoland-humor', 'blind-public', 'instiz-issues'):
        assert sources[ident]['enabled'] is False
        assert sources[ident]['note']
    for ident in ('inven-openissue', 'ruliweb-humor', 'quasarzone-free'):
        assert sources[ident]['enabled'] is True
