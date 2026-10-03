"""Synthetic body/error templates; no third-party post content or network."""

import pytest

from services.article_content import extract_article


CASES = [
    ("https://www.inven.co.kr/board/webzine/2097/123", '<div id="powerbbsContent">{body}</div>'),
    ("https://www.inven.co.kr/webzine/news/?news=123", '<div id="imageCollectDiv">{body}</div>'),
    ("https://bbs.ruliweb.com/community/board/300143/read/123", '<div class="board_main_view"><div class="view_content">{body}</div></div>'),
    ("https://bbs.ruliweb.com/news/read/123", '<section class="news_read"><div itemprop="articleBody">{body}</div></section>'),
    ("https://www.ppomppu.co.kr/zboard/view.php?id=freeboard&no=123", '<td class="board-contents" class="han">{body}</td>'),
    ("https://www.clien.net/service/board/park/123", '<div class="post_article">{body}</div>'),
    ("https://www.bobaedream.co.kr/view?code=freeb&No=123", '<div class="bodyCont">{body}</div>'),
    ("https://theqoo.net/hot/123", '<article itemprop="articleBody"><div class="rhymix_content xe_content">{body}</div></article>'),
    ("https://gall.dcinside.com/board/view/?id=dcbest&no=123", '<div class="writing_view_box"><div class="write_div">{body}</div></div>'),
    ("https://web.humoruniv.com/board/humor/read.html?table=pds&number=123", '<div id="wrap_body">{body}</div>'),
    ("https://www.todayhumor.co.kr/board/view.php?table=bestofbest&no=123", '<div class="viewContent">{body}</div>'),
    ("https://www.dogdrip.net/123", '<div class="document_123_0 rhymix_content xe_content">{body}</div>'),
    ("https://www.82cook.com/entiz/read.php?bn=15&num=123", '<div id="articleBody">{body}</div>'),
    ("https://quasarzone.com/bbs/qb_free/views/123", '<div class="view-content"><div id="new_contents">{body}</div></div>'),
]


@pytest.mark.parametrize("url,wrapper", CASES)
def test_only_article_is_captured_and_login_widget_does_not_block(url, wrapper):
    html = '<nav>광고 로그인 메뉴</nav><form>로그인 후 이용해주세요</form>'
    html += wrapper.format(body='<p>정상 본문 &amp; 내용</p><p>두번째 단락</p>')
    html += '<div class="comment_456_0 xe_content">댓글 내용</div>'
    result = extract_article(url, html)
    assert result['supported']
    assert result['status'] == 'live'
    assert result['body_text'] == '정상 본문 & 내용\n두번째 단락'
    assert result['parser_version'] == 'community-v1'


@pytest.mark.parametrize("url,wrapper", CASES)
def test_image_only_articles_and_media_urls_are_preserved(url, wrapper):
    html = '<img src="/avatar.jpg">' + wrapper.format(body='''
        <img src="data:image/gif;base64,blank" data-src="https://cdn.example/img.jpg#hash">
        <img src="https://cdn.example/img.jpg">
        <video poster="https://cdn.example/poster.jpg"><source src="https://cdn.example/clip.mp4"></video>
        <img src="javascript:alert(1)"><img src="file:///etc/passwd">
        <img src="https://password@cdn.example/secret"><img src="https://cdn.example:9999/no">
        <img hidden src="https://cdn.example/hidden.jpg">''')
    result = extract_article(url, html)
    assert result['status'] == 'live'
    assert result['body_text'] is None
    assert result['media_urls'] == ['https://cdn.example/img.jpg', 'https://cdn.example/poster.jpg', 'https://cdn.example/clip.mp4']


@pytest.mark.parametrize("url,wrapper", CASES)
def test_known_sites_never_capture_menu_as_missing_article(url, wrapper):
    result = extract_article(url, '<nav>메뉴</nav><p>인기 글 목록</p><div>사이트 소개</div>')
    assert result['status'] == 'unknown'
    assert result['body_text'] is None
    assert result['media_urls'] == []


@pytest.mark.parametrize("html,status", [
    ('<h1>Just a moment...</h1><p>Checking your browser</p>', 'blocked'),
    ('<h1>보안 시스템</h1><p>잠시 기다리면 자동으로 접속됩니다</p>', 'blocked'),
    ('<p>로그인 후에 이용해주세요.</p>', 'blocked'),
    ('<p>회원만 읽을 수 있습니다.</p>', 'blocked'),
    ('<script>alert("이미 삭제된 게시물입니다."); history.back();</script>', 'deleted'),
    ('<script>alert("게시물이 존재하지 않습니다.");</script>', 'deleted'),
    ('<script>alert("로그인이 필요합니다.");</script>', 'blocked'),
    ('<p>삭제된 게시글입니다.</p>', 'deleted'),
])
def test_error_templates_never_become_capture(html, status):
    for url in ['https://www.inven.co.kr/board/webzine/2097/123', 'https://community.example/post/1']:
        result = extract_article(url, html)
        assert result['status'] == status
        assert result['body_text'] is None


def test_deletion_related_article_and_comment_scripts_are_not_deletion_templates():
    html = '<div id="powerbbsContent">삭제된 글에 관한 안내를 정리한 정상적인 기사입니다.</div>'
    html += '<script>function delComment() {alert("이미 삭제된 댓글입니다.");}</script>'
    assert extract_article(CASES[0][0], html)['status'] == 'live'


@pytest.mark.parametrize("body,status", [('삭제된 게시물입니다.', 'deleted'), ('이 게시물은 운영자에 의해 블라인드 처리되었습니다.', 'blocked')])
def test_error_notice_in_article_container(body, status):
    html = CASES[0][1].format(body=body)
    assert extract_article(CASES[0][0], html)['status'] == status


def test_unknown_host_fallback_and_hidden_unclosed_script():
    result = extract_article('https://community.example/post/1', '<p>article body</p><script> unfinished;')
    assert result['status'] == 'live'
    assert result['supported'] is False
    assert result['body_text'] == 'article body'


def test_unknown_article_container_excludes_normal_site_login_notice():
    html = '<div>댓글 작성은 로그인 후에 이용해주세요</div><article>본문 내용</article>'
    assert extract_article('https://community.example/post/1', html)['body_text'] == '본문 내용'


def test_xe_comment_or_other_document_is_not_the_requested_article():
    html = '<div class="comment_123_0 xe_content">댓글</div><div class="document_456_0 xe_content">다른 글</div>'
    result = extract_article('https://www.dogdrip.net/123', html)
    assert result['status'] == 'unknown'


def test_hidden_article_and_external_hostname_lookalike():
    html = '<div id="powerbbsContent" style="display: none">숨긴 글</div>'
    assert extract_article(CASES[0][0], html)['status'] == 'unknown'
    assert not extract_article('https://inven.co.kr.evil.test/123', '<p>text</p>')['supported']


def test_relative_media_and_embeds_are_references_not_saved_claims():
    html = CASES[0][1].format(body='<img src="/image.png"><iframe src="https://video.example/embed/1"></iframe>')
    result = extract_article(CASES[0][0], html)
    assert result['media_urls'] == ['https://www.inven.co.kr/image.png', 'https://video.example/embed/1']


def test_deep_html_is_rejected_without_partial_body_or_media():
    html = CASES[0][1].format(body='early text<img src="/image.png">' + '<div>' * 300 + 'deep')
    result = extract_article(CASES[0][0], html)
    assert result['status'] == 'unknown'
    assert result['body_text'] is None and result['media_urls'] == []
    assert '제한' in result['reason']


def test_shallow_node_flood_and_many_article_regions_are_bounded(monkeypatch):
    from services import article_content
    monkeypatch.setattr(article_content, '_MAX_NODES', 16)
    html = CASES[0][1].format(body='early text' + '<br>' * 17)
    assert extract_article(CASES[0][0], html)['status'] == 'unknown'
    monkeypatch.setattr(article_content, '_MAX_NODES', 50_000)
    html = '<article>body</article>' * 65
    result = extract_article('https://community.example/post/1', html)
    assert result['status'] == 'unknown'
    assert result['body_text'] is None
