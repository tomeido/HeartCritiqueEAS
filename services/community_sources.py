"""수집 대상의 단일 목록과 공개 HTML 게시판 파서.

로그인·챌린지 우회 없이 읽을 수 있는 목록만 자동 수집한다. 신규 사이트는
2026-09-26 목록과 실제 글을 확인했다. 접근 가능 여부는 각 폴링에서 다시 판단한다.
기존 RSS/HTML 파서는 collector 에 있으며 이 모듈은 DB/네트워크에 의존하지 않는다.
"""

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit


def _source(id, name, domain, board, url, parser, *, enabled=True, note=""):
    return {
        "id": id, "name": name, "domain": domain, "board": board, "url": url,
        "kind": "rss" if parser == "rss" else "html", "parser": parser,
        "enabled": enabled, "note": note,
    }


COMMUNITY_SOURCES = [
    _source("ppomppu-deals", "뽐뿌", "ppomppu.co.kr", "뽐뿌게시판",
            "https://www.ppomppu.co.kr/rss.php?id=ppomppu", "rss"),
    _source("ppomppu-free", "뽐뿌", "ppomppu.co.kr", "자유게시판",
            "https://www.ppomppu.co.kr/rss.php?id=freeboard", "rss"),
    _source("ruliweb-news", "루리웹", "ruliweb.com", "뉴스",
            "https://bbs.ruliweb.com/news/rss", "rss"),
    _source("ruliweb-humor", "루리웹", "ruliweb.com", "유머 베스트",
            "https://bbs.ruliweb.com/best/humor", "ruliweb"),
    _source("mlbpark-bullpen", "엠엘비파크", "mlbpark.donga.com", "불펜",
            "https://mlbpark.donga.com/mp/rss.php", "rss", enabled=False,
            note="2026-09-26 robots.txt가 일반 수집기의 접근을 금지하여 수집을 보류했습니다."),
    _source("inven-news", "인벤", "inven.co.kr", "뉴스",
            "https://www.inven.co.kr/webzine/news/rss.php", "rss"),
    _source("inven-openissue", "인벤", "inven.co.kr", "오픈이슈갤러리",
            "https://www.inven.co.kr/board/webzine/2097", "inven"),
    _source("clien-park", "클리앙", "clien.net", "모두의공원",
            "https://www.clien.net/service/board/park", "clien"),
    _source("bobaedream-free", "보배드림", "bobaedream.co.kr", "자유게시판",
            "https://bobaedream.co.kr/list?code=freeb", "bobaedream"),
    _source("theqoo-hot", "더쿠", "theqoo.net", "HOT",
            "https://theqoo.net/hot", "theqoo"),
    _source("pann-ranking", "네이트판", "pann.nate.com", "톡커들의 선택",
            "https://pann.nate.com/talk/ranking", "pann", enabled=False,
            note="2026-09-26 robots.txt가 일반 수집기의 접근을 금지하여 수집을 보류했습니다."),
    _source("dcinside-best", "디시인사이드", "dcinside.com", "실시간 베스트",
            "https://gall.dcinside.com/board/lists/?id=dcbest", "dcinside"),
    _source("humoruniv-best", "웃긴대학", "humoruniv.com", "웃긴자료 일간 베스트",
            "https://web.humoruniv.com/board/humor/list.html?table=pds&st=day", "humoruniv"),
    _source("todayhumor-best", "오늘의유머", "todayhumor.co.kr", "베스트오브베스트",
            "https://www.todayhumor.co.kr/board/list.php?table=bestofbest", "todayhumor"),
    _source("slrclub-free", "SLR클럽", "slrclub.com", "자유게시판",
            "https://www.slrclub.com/bbs/zboard.php?id=free", "slrclub", enabled=False,
            note="2026-09-26 robots.txt가 일반 수집기의 게시판 접근을 금지하여 수집을 보류했습니다."),
    _source("ddanzi-free", "딴지일보", "ddanzi.com", "자유게시판",
            "https://www.ddanzi.com/free", "ddanzi", enabled=False,
            note="2026-09-26 robots.txt가 일반 수집기의 자유게시판 접근을 금지하여 수집을 보류했습니다."),
    _source("dogdrip-best", "개드립", "dogdrip.net", "개드립",
            "https://www.dogdrip.net/dogdrip", "dogdrip"),
    _source("82cook-free", "82쿡", "82cook.com", "자유게시판",
            "https://www.82cook.com/entiz/enti.php?bn=15", "cook82"),
    _source("fmkorea-best", "에펨코리아", "fmkorea.com", "포텐 터짐",
            "https://www.fmkorea.com/best", "fmkorea", enabled=False,
            note="2026-09-26 robots.txt는 /best 목록을 허용하지만 정규 원문 /글번호 경로는 일반 수집기에 금지하여 보류했습니다."),
    _source("quasarzone-free", "퀘이사존", "quasarzone.com", "자유게시판",
            "https://quasarzone.com/bbs/qb_free", "quasarzone",
            note="2026-09-26 공개 목록·본문과 robots 허용 경로를 확인했습니다."),
    _source("dmitory-issue", "디미토리", "dmitory.com", "이슈",
            "https://www.dmitory.com/issue", "dmitory", enabled=False,
            note="2026-09-26 공개 목록 HTTP 403 및 robots의 AI 에이전트 제한으로 수집을 보류했습니다."),
    _source("damoang-free", "다모앙", "damoang.net", "자유게시판",
            "https://damoang.net/free", "damoang", enabled=False,
            note="2026-09-26 공개 목록 HTTP 403 및 robots의 AI 에이전트·무단 수집기 제한으로 수집을 보류했습니다."),
    _source("arca-best", "아카라이브", "arca.live", "공개 베스트",
            "https://arca.live/b/live?mode=best", "arca", enabled=False,
            note="공개 목록 HTTP 403 보안 챌린지로 보류했습니다. 승인된 접근 경로가 필요합니다."),
    _source("etoland-humor", "이토랜드", "etoland.co.kr", "유머",
            "https://etoland.co.kr/b/etohumor06/list", "etoland", enabled=False,
            note="robots의 AI·아카이브 봇 제한과 변경된 글 주소 체계로 운영자 허용 범위 확인 전 수집을 보류했습니다."),
    _source("blind-public", "블라인드", "teamblind.com", "공개 게시판",
            "https://www.teamblind.com/kr/", "blind", enabled=False,
            note="약관에서 명시 허가 없는 크롤링·추출을 제한하므로 운영자 허가 전 수집을 보류했습니다."),
    _source("instiz-issues", "인스티즈", "instiz.net", "인티포털",
            "https://www.instiz.net/pt", "instiz", enabled=False,
            note="공개 목록 요청이 HTTP 403 보안 챌린지를 반환하여 자동 수집을 보류했습니다."),
]


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_DECORATION = {"list_comment_num", "reply_num", "reply_numbox", "comment_count", "reply_count", "con-comment"}


def _classes(attrs):
    return set(attrs.get("class", "").split())


class _ListLinks(HTMLParser):
    """제목 링크의 텍스트와 상위 요소만 저장하는 작은 스트리밍 파서.

    속성 순서·작은따옴표·중첩 제목 태그에 의존하지 않는다. 스크립트, 댓글 수,
    숨김 텍스트를 제목에 섞지 않으며 공지 행의 정보는 링크와 함께 보존한다.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.links = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        attrs = {k: v or "" for k, v in attrs}
        # HTML 목록은 종종 선택적인 </td>, </tr>, </li> 를 생략한다.
        if tag in {"tr", "td", "th", "li"}:
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    del self.stack[i:]
                    break
        if tag == "a":
            self._finish_link()
            self.active = {"attrs": attrs, "parents": tuple(self.stack), "text": []}
        if tag not in _VOID:
            self.stack.append((tag, attrs))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "a":
            self._finish_link()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.active is None:
            return
        for tag, attrs in self.stack:
            if (tag in {"script", "style"} or _classes(attrs) & _DECORATION
                    or attrs.get("aria-hidden") == "true" or "hidden" in attrs):
                return
        self.active["text"].append(data)

    def _finish_link(self):
        if self.active is not None:
            self.active["text"] = " ".join("".join(self.active["text"]).split())
            self.links.append(self.active)
            self.active = None

    def close(self):
        super().close()
        self._finish_link()


def _in_title(link, tag, css_class):
    return any(t == tag and css_class in _classes(a) for t, a in link["parents"])


def _is_notice(link):
    for tag, attrs in link["parents"]:
        if tag in {"tr", "li"} and (
                any("notice" in c.lower() for c in _classes(attrs))
                or "notice" in attrs.get("data-type", "").lower()):
            return True
    return False


def _safe_url(href, base):
    """출처의 정확한 호스트만 허용한 뒤 사이트별로 ID URL 을 재구성한다."""
    try:
        parsed = urlsplit(urljoin(base, href))
        host = urlsplit(base).hostname
        allowed = {host, host.removeprefix("www.")}
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in allowed
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 80, 443} or parsed.fragment):
            return None
        return parsed
    except ValueError:
        return None


def _one(query, key, pattern=r"[0-9]+"):
    values = query.get(key, [])
    return values[0] if len(values) == 1 and re.fullmatch(pattern, values[0]) else None


def _article_url(parser, link):
    attrs = link["attrs"]
    bases = {
        "dcinside": "https://gall.dcinside.com/board/",
        "humoruniv": "https://web.humoruniv.com/board/humor/",
        "todayhumor": "https://www.todayhumor.co.kr/board/",
        "slrclub": "https://www.slrclub.com/bbs/",
        "ddanzi": "https://www.ddanzi.com/",
        "dogdrip": "https://www.dogdrip.net/",
        "cook82": "https://www.82cook.com/entiz/",
        "fmkorea": "https://www.fmkorea.com/",
        "inven": "https://www.inven.co.kr/",
        "ruliweb": "https://bbs.ruliweb.com/",
        "quasarzone": "https://quasarzone.com/",
    }
    base = bases.get(parser)
    if not base or _is_notice(link):
        return None
    parsed = _safe_url(attrs.get("href", ""), base)
    if parsed is None:
        return None
    query = parse_qs(parsed.query)
    path = parsed.path
    if parser == "dcinside" and _in_title(link, "td", "gall_tit"):
        board, no = _one(query, "id", "dcbest|hit"), _one(query, "no")
        if path == "/board/view/" and board and no and "t" not in query:
            return f"https://gall.dcinside.com/board/view/?id={board}&no={no}"
    elif parser == "humoruniv" and _in_title(link, "td", "li_sbj"):
        no = _one(query, "number")
        if path == "/board/humor/read.html" and _one(query, "table", "pds") and no:
            return f"https://web.humoruniv.com/board/humor/read.html?table=pds&number={no}"
    elif parser == "todayhumor" and _in_title(link, "td", "subject"):
        no = _one(query, "no")
        if path == "/board/view.php" and _one(query, "table", "bestofbest") and no:
            return f"https://www.todayhumor.co.kr/board/view.php?table=bestofbest&no={no}"
    elif parser == "slrclub" and _in_title(link, "td", "sbj"):
        no = _one(query, "no")
        if path == "/bbs/vx2.php" and _one(query, "id", "free") and no:
            return f"https://www.slrclub.com/bbs/vx2.php?id=free&no={no}"
    elif parser == "ddanzi" and _in_title(link, "td", "title"):
        match = re.fullmatch(r"/free/([0-9]+)", path)
        if match:
            return f"https://www.ddanzi.com/free/{match[1]}"
    elif parser == "dogdrip" and "title-link" in _classes(attrs):
        match = re.fullmatch(r"/(?:dogdrip/)?([0-9]+)", path)
        if match:
            return f"https://www.dogdrip.net/{match[1]}"
    elif parser == "cook82" and _in_title(link, "td", "title"):
        no = _one(query, "num")
        if path == "/entiz/read.php" and _one(query, "bn", "15") and no:
            return f"https://www.82cook.com/entiz/read.php?bn=15&num={no}"
    elif parser == "fmkorea" and _in_title(link, "h3", "title"):
        match = re.fullmatch(r"/(?:best/)?([0-9]+)", path)
        no = match[1] if match else None
        if path == "/index.php" and _one(query, "mid", "best|best2"):
            no = _one(query, "document_srl")
        if no:
            return f"https://www.fmkorea.com/{no}"
    elif parser == "inven" and "subject-link" in _classes(attrs):
        match = re.fullmatch(r"/board/webzine/2097/([0-9]+)", path)
        if match and _in_title(link, "div", "board-list"):
            return f"https://www.inven.co.kr/board/webzine/2097/{match[1]}"
    elif parser == "ruliweb" and "subject_link" in _classes(attrs):
        match = re.fullmatch(r"/(?:best|community)/board/300143/read/([0-9]+)", path)
        if (match and _in_title(link, "td", "subject")
                and _in_title(link, "tr", "mode_list")
                and not _in_title(link, "tr", "best_top_row")):
            return f"https://bbs.ruliweb.com/community/board/300143/read/{match[1]}"
    elif parser == "quasarzone" and "subject-link" in _classes(attrs):
        match = re.fullmatch(r"/bbs/qb_free/views/([0-9]+)", path)
        if match and _in_title(link, "div", "v2-list-row"):
            return f"https://quasarzone.com/bbs/qb_free/views/{match[1]}"
    return None


def parse_html_source(parser: str, raw: bytes) -> list[dict]:
    """신규 공개 게시판에서 제목·정규 URL 을 추출한다(지원하지 않는 파서는 빈 목록).

    UTF-8 을 우선하고 EUC-KR/CP949 문서를 손실 없이 디코딩한다. 공지·댓글 링크,
    타 사이트 URL 을 제외하고 쿼리 순서·페이지 번호가 달라도 같은 글은 한 번만 준다.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp949", errors="replace")
    document = _ListLinks()
    document.feed(text)
    document.close()
    result = []
    seen = set()
    for link in document.links:
        url = _article_url(parser, link)
        title = link["text"]
        if not url or not title or url in seen:
            continue
        seen.add(url)
        result.append({"url": url, "title": title, "guid": url, "summary": None})
    return result
