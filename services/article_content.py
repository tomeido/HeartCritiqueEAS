"""Conservative article extraction, independent of HTTP, storage, and the database.

Known communities require a body container. Navigation, comments and login widgets
cannot become a captured article when the body is absent. HTTP status and redirects
remain the caller's responsibility; ``deleted`` is a soft template signal, never a
404/410 confirmation. Media URLs are references, not a claim that bytes were saved.
"""

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

PARSER_VERSION = "community-v1"
_MAX_DEPTH = 256
_MAX_NODES = 50_000
_MAX_REGIONS = 64
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_IGNORE = {"script", "style", "noscript", "template", "svg", "head", "nav", "footer", "aside", "form", "button"}
_BLOCK = {"br", "p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "blockquote"}
_HOSTS = {
    "ppomppu.co.kr": "ppomppu", "ruliweb.com": "ruliweb", "inven.co.kr": "inven",
    "mlbpark.donga.com": "mlbpark", "clien.net": "clien", "bobaedream.co.kr": "bobaedream",
    "theqoo.net": "theqoo", "pann.nate.com": "pann", "dcinside.com": "dcinside",
    "humoruniv.com": "humoruniv", "todayhumor.co.kr": "todayhumor", "slrclub.com": "slrclub",
    "ddanzi.com": "ddanzi", "dogdrip.net": "dogdrip", "82cook.com": "cook82",
    "fmkorea.com": "fmkorea", "quasarzone.com": "quasarzone",
}
_DOCUMENT = re.compile(r"document_([0-9]+)(?:_[0-9]+)?$")
_DELETED = re.compile(
    r"(?:삭제(?:된|되었|됐|한)\s*(?:게시물|게시글|글)|"
    r"(?:게시물|게시글|글)(?:이|은|가)?\s*삭제(?:되었|됐|된)|"
    r"존재하지\s*않는\s*(?:게시물|게시글|글)|"
    r"(?:게시물|게시글|글)(?:이|을|은|가)?\s*존재하지\s*않|"
    r"(?:삭제되었거나|삭제됐거나)\s*(?:이동|존재)|"
    r"(?:post|article)\s+(?:has been\s+)?(?:deleted|removed|not found))", re.I)
_BLOCKED = re.compile(
    r"(?:로그인(?:을|이)?\s*(?:하셔야|해야|후에?|필요)|"
    r"(?:회원|로그인한\s*회원)만\s*(?:읽|열람|접근|볼)|"
    r"(?:읽기|열람|접근)\s*권한이\s*없|비공개\s*(?:게시물|게시글|글)|"
    r"블라인드\s*(?:처리된|처리되었습니다|된)|"
    r"(?:sign|log)\s*in\s+(?:is required|to (?:read|view|continue))|access\s+denied)", re.I)
_CHALLENGE = re.compile(
    r"(?:just a moment|checking your browser|verify (?:that )?you are human|"
    r"보안\s*시스템|자동입력\s*방지|잠시\s*기다리면\s*자동으로\s*접속)", re.I)
_ALERT = re.compile(r"\balert\s*\(\s*(['\"])(.*?)\1\s*\)", re.I | re.S)
_DELETION_NOTICE = re.compile(
    r"^(?:(?:오류|안내)\s*[:：]?\s*)?(?:(?:요청하신|해당|이미)\s*)?"
    r"(?:(?:삭제된|존재하지\s*않는)\s*(?:게시물|게시글|글)(?:입니다|이에요)?|"
    r"(?:게시물|게시글|글)(?:이|은|가)?\s*(?:삭제(?:되었|됐)습니다|존재하지\s*않습니다))"
    r"[.!\s]*$", re.I)
_ACCESS_NOTICE = re.compile(
    r"^(?:(?:오류|안내)\s*[:：]?\s*)?(?:로그인(?:을|이)?\s*(?:하셔야|해야|후에?|필요).{0,100}|"
    r"(?:읽기|열람|접근)\s*권한이\s*없.{0,80}|"
    r"(?:(?:이|해당)\s*)?(?:게시물|게시글|글)(?:은|이)?\s*(?:운영자에\s*의해\s*)?블라인드\s*처리되었습니다[.!\s]*)$", re.I)


class _ExtractionLimit(ValueError):
    """Abort pathological HTML rather than returning a partial successful body."""


def _classes(attrs):
    return set(attrs.get("class", "").split())


def _hidden(tag, attrs):
    style = re.sub(r"\s+", "", attrs.get("style", "").lower())
    return (tag in _IGNORE or "hidden" in attrs or attrs.get("aria-hidden") == "true"
            or "display:none" in style or "visibility:hidden" in style)


def _site(url):
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    return next((site for domain, site in _HOSTS.items()
                 if host == domain or host.endswith("." + domain)), None)


def _document_id(url):
    parsed = urlsplit(url)
    values = re.findall(r"[0-9]+", parsed.path)
    query = parse_qs(parsed.query)
    return next((query[key][0] for key in ("document_srl", "no", "num", "number") if key in query),
                values[-1] if values else None)


def _matches(site, tag, attrs, parents, article_id):
    """Selectors observed in public HTML; comments' XE containers are excluded."""
    classes, ident = _classes(attrs), attrs.get("id", "")
    parent_classes = set().union(*(_classes(a) for _, a in parents)) if parents else set()
    if site in {"dogdrip", "ddanzi", "fmkorea", "theqoo"}:
        docs = [m[1] for c in classes if (m := _DOCUMENT.fullmatch(c))]
        if docs:
            return (not article_id or article_id in docs) and bool(classes & {"xe_content", "rhymix_content"})
        return site == "theqoo" and "xe_content" in classes and (
            "read_body" in parent_classes or any(t == "article" and a.get("itemprop") == "articleBody" for t, a in parents))
    if site == "ruliweb":
        return ("view_content" in classes and "board_main_view" in parent_classes
                or attrs.get("itemprop") == "articleBody" and "news_read" in parent_classes)
    if site == "quasarzone":
        return ident == "new_contents" and "view-content" in parent_classes
    return {
        "ppomppu": "board-contents" in classes,
        "inven": ident in {"powerbbsContent", "imageCollectDiv"},
        "mlbpark": "ar_txt" in classes,
        "clien": "post_article" in classes,
        "bobaedream": ident == "bodyCont" or "bodyCont" in classes,
        "pann": ident == "contentArea",
        "dcinside": "write_div" in classes and "writing_view_box" in parent_classes,
        "humoruniv": ident == "wrap_body",
        "todayhumor": "viewContent" in classes,
        "slrclub": ident == "userct",
        "cook82": ident == "articleBody",
    }.get(site, False)


def _media_url(base, value):
    try:
        url = urljoin(base, value.strip())
        parsed = urlsplit(url)
        if (parsed.scheme in {"http", "https"} and parsed.hostname
                and not parsed.username and not parsed.password and parsed.port in {None, 80, 443}):
            return parsed._replace(fragment="").geturl()
    except ValueError:
        pass
    return None


class _ArticleParser(HTMLParser):
    def __init__(self, url, site):
        super().__init__(convert_charrefs=True)
        self.url, self.site = url, site
        self.article_id = _document_id(url)
        self.stack = []
        self.regions = []
        self.visible = []
        self.scripts = []
        self.headings = []
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if len(self.stack) >= _MAX_DEPTH or self.nodes > _MAX_NODES:
            raise _ExtractionLimit("HTML depth or node limit exceeded")
        # Some legacy boards repeat class attributes; retain the first value,
        # matching browser parsing instead of silently losing board-contents.
        attrs = dict(reversed([(k, v or "") for k, v in attrs]))
        hidden = _hidden(tag, attrs) or any(e[2] for e in self.stack)
        parents = [(t, a) for t, a, _ in self.stack]
        match = _matches(self.site, tag, attrs, parents, self.article_id) if self.site else tag in {"article", "main"}
        if match and not hidden:
            if len(self.regions) >= _MAX_REGIONS:
                raise _ExtractionLimit("HTML article region limit exceeded")
            self.regions.append({"depth": len(self.stack), "text": [], "media": [], "closed": False})
        active = [r for r in self.regions if not r["closed"]]
        if not hidden:
            if tag in _BLOCK:
                self.visible.append("\n")
                for region in active:
                    region["text"].append("\n")
            if tag in {"img", "video", "audio", "source", "iframe", "embed"}:
                # Lazy-load references precede placeholder src. Do not use images
                # from navigation/avatars outside the selected body container.
                refs = [next((attrs[k] for k in ("data-original", "data-src", "data-lazy-src", "src")
                              if attrs.get(k) and not attrs[k].startswith("data:")), "")]
                if attrs.get("poster"):
                    refs.append(attrs["poster"])
                if not refs[0] and attrs.get("srcset"):
                    refs[0] = attrs["srcset"].split(",")[0].strip().split(" ")[0]
                for ref in refs:
                    url = _media_url(self.url, ref) if ref else None
                    if url:
                        for region in active:
                            if url not in region["media"]:
                                region["media"].append(url)
        if tag not in _VOID:
            self.stack.append((tag, attrs, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for region in self.regions:
                    if not region["closed"]:
                        if tag in _BLOCK:
                            region["text"].append("\n")
                        if region["depth"] >= i:
                            region["closed"] = True
                del self.stack[i:]
                return

    def handle_data(self, data):
        if any(t == "script" for t, _, _ in self.stack):
            self.scripts.append(data)
        if any(t in {"title", "h1"} for t, _, _ in self.stack):
            self.headings.append(data)
        if any(e[2] for e in self.stack):
            return
        self.visible.append(data)
        for region in self.regions:
            if not region["closed"]:
                region["text"].append(data)


def _clean(parts):
    return "\n".join(line for raw in "".join(parts).splitlines()
                     if (line := " ".join(raw.split())))


def extract_article(url: str, html: str) -> dict:
    """Return body and media references only if the article is plausibly present.

    Unsupported hosts get a conservative visible-text fallback (supported=False).
    Known sites never fall back to an entire page if their body selector is missing.
    Image/embedded-media-only bodies count as live but may have body_text=None.
    """
    site = _site(url)
    result = {"supported": bool(site), "body_text": None, "media_urls": [],
              "status": "unknown", "reason": "본문 영역을 확인하지 못했습니다.",
              "parser_version": PARSER_VERSION}
    try:
        parser = _ArticleParser(url, site)
        parser.feed(html)
        parser.close()
    except _ExtractionLimit:
        return {**result, "reason": "HTML 구조 복잡도 제한을 초과했습니다."}
    except (ValueError, AssertionError):
        return result
    candidates = [(_clean(r["text"]), r["media"]) for r in parser.regions]
    candidates = [(text, media) for text, media in candidates if text or media]
    visible = _clean(parser.visible)
    if candidates:
        body, media = candidates[0]
        # Error templates sometimes occupy the normal article container. Only a
        # short, standalone notice qualifies; normal article discussions do not.
        if not media and len(body) <= 160 and (_DELETION_NOTICE.fullmatch(body) or _ACCESS_NOTICE.fullmatch(body)):
            status = "deleted" if _DELETION_NOTICE.fullmatch(body) else "blocked"
            return {**result, "status": status, "reason": "본문 영역의 삭제 안내" if status == "deleted" else "본문 영역의 접근 제한 안내"}
        return {**result, "status": "live", "body_text": body or None, "media_urls": media,
                "reason": "사이트 본문 영역 확인" if site else "일반 문서 본문 영역 확인"}
    headings = " ".join(parser.headings)
    if _CHALLENGE.search(headings) or (len(visible) < 3000 and _CHALLENGE.search(visible)):
        return {**result, "status": "blocked", "reason": "보안 챌린지 안내"}
    # Ignore comment-management scripts and avoid scanning arbitrary JS functions.
    alerts = [m[2] for m in _ALERT.finditer(" ".join(parser.scripts)) if "댓글" not in m[2]]
    if any(_DELETED.search(msg) for msg in alerts) or (len(visible) < 1500 and _DELETED.search(visible)):
        return {**result, "status": "deleted", "reason": "본문 없는 삭제 안내"}
    if any(_BLOCKED.search(msg) for msg in alerts) or (len(visible) < 1500 and _BLOCKED.search(visible)):
        return {**result, "status": "blocked", "reason": "로그인 또는 열람 제한 안내"}
    if not site and visible and not re.fullmatch(r"(?:로딩\s*중|loading)[.\s…]*", visible, re.I):
        return {**result, "status": "live", "body_text": visible,
                "reason": "미지원 사이트 가시 텍스트; 사이트별 검증 없음"}
    return result
