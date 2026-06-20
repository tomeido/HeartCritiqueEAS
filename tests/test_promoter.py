"""promoter(캡처→공개 승격) 안전 게이트 회귀 테스트.

검증 대상(되돌릴 수 없는 박제이므로 게이트가 핵심):
  · PII 검출 시 자동 승격 차단(blocked_pii) + LLM 호출 안 함.
  · critique 카테고리는 기본 자동 승격 금지 → pending_review.
  · 정상 kindness 캡처는 승격되어 stories INSERT + citation 등록 + captured 표식 갱신.
  · 멱등: 이미 같은 origin 으로 승격된 스토리가 있으면 그것에 연결(중복 INSERT 안 함).
  · classify_category: 고발 어휘 → critique, 아니면 kindness.
"""

import types

import services.promoter as promoter


class _Result:
    def __init__(self, data=None):
        self.data = data if data is not None else []


class _Query:
    """supabase 체이닝 API 의 최소 흉내 — promote_one/_link_existing 경로만 커버."""
    def __init__(self, db, table):
        self.db = db
        self.table_name = table
        self._op = None
        self._payload = None
        self._eq = {}

    def select(self, *a, **k):
        self._op = "select"; return self

    def insert(self, row):
        self._op = "insert"; self._payload = row; return self

    def update(self, row):
        self._op = "update"; self._payload = row; return self

    def eq(self, k, v):
        self._eq[k] = v; return self

    def is_(self, k, v):
        self._eq[k] = ("is", v); return self

    def limit(self, n):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        db, t = self.db, self.table_name
        if self._op == "insert" and t == "stories":
            row = dict(self._payload)
            sid = f"story-{len(db.stories)+1}"
            row["id"] = sid
            db.stories.append(row)
            return _Result([{"id": sid}])
        if self._op == "select" and t == "stories":
            # _link_existing: origin_captured_url 로 기존 스토리 찾기
            url = self._eq.get("origin_captured_url")
            hits = [s for s in db.stories if s.get("origin_captured_url") == url]
            return _Result([{"id": hits[0]["id"]}] if hits else [])
        if self._op == "update" and t == "captured_posts":
            db.captured_updates.append(dict(self._payload))
            return _Result([])
        return _Result([])


class FakeDB:
    def __init__(self):
        self.stories = []
        self.captured_updates = []

    def table(self, name):
        return _Query(self, name)


def _patch_clean(monkeypatch, *, gen_no_fit=False):
    """LLM·PII·citation 등록을 결정적 stub 으로 격리."""
    monkeypatch.setattr(promoter, "pii_scan",
                        lambda text: {"hit": False, "kinds": [], "samples": []})
    monkeypatch.setattr(promoter, "register_citations", lambda sid, cites: None)

    def fake_gen(body, title, category):
        if gen_no_fit:
            return {"no_fit": True, "category": category, "body": "", "text": "",
                    "volatility_score": 0, "poetic_reason": "", "provider": "groq", "model": "x"}
        return {"no_fit": False, "category": category,
                "body": "익명화된 문학 본문.", "text": "헤더\n\n익명화된 문학 본문.",
                "volatility_score": 8, "poetic_reason": "사라지는 것을 붙든다",
                "provider": "groq", "model": "x"}
    monkeypatch.setattr(promoter, "generate_from_text", fake_gen)


def test_classify_category():
    assert promoter.classify_category("회장 갑질", "대기업 갑질 폭로") == "critique"
    assert promoter.classify_category("훈훈한 미담", "자리 양보 이야기") == "kindness"


def test_pii_blocks_promotion(monkeypatch):
    _patch_clean(monkeypatch)
    # 원본 본문에 PII → 차단. generate 가 호출되면 안 됨.
    monkeypatch.setattr(promoter, "pii_scan",
                        lambda text: {"hit": True, "kinds": ["mobile"], "samples": ["mobile:01**"]})
    called = {"gen": False}
    monkeypatch.setattr(promoter, "generate_from_text",
                        lambda *a, **k: called.__setitem__("gen", True) or {"no_fit": True})
    db = FakeDB()
    row = {"id": "c1", "url": "https://theqoo.net/1", "title": "t",
           "body_text": "연락처 010-1234-5678", "volatility_score": 9,
           "hard_deleted_at": "2026-06-20T00:00:00+00:00"}
    sid = promoter.promote_one(db, row, auto=True)
    assert sid is None
    assert called["gen"] is False
    assert db.captured_updates[-1]["promotion_status"] == "blocked_pii"
    assert not db.stories


def test_critique_auto_hold(monkeypatch):
    _patch_clean(monkeypatch)
    monkeypatch.setattr(promoter, "PROMOTER_AUTO_CRITIQUE", False)
    db = FakeDB()
    row = {"id": "c2", "url": "https://www.teamblind.com/kr/post/2", "title": "회장 갑질",
           "body_text": "대기업 회장 갑질 폭로 제보합니다", "volatility_score": 9,
           "hard_deleted_at": "2026-06-20T00:00:00+00:00"}
    sid = promoter.promote_one(db, row, auto=True)
    assert sid is None
    assert db.captured_updates[-1]["promotion_status"] == "pending_review"
    assert not db.stories


def test_kindness_promotes_and_registers(monkeypatch):
    _patch_clean(monkeypatch)
    registered = {}
    monkeypatch.setattr(promoter, "register_citations",
                        lambda sid, cites: registered.update({"sid": sid, "cites": cites}))
    db = FakeDB()
    url = "https://www.ppomppu.co.kr/zboard/view.php?id=freeboard&no=5"
    row = {"id": "c3", "url": url, "title": "훈훈한 미담",
           "body_text": "한 시민이 자리를 양보했다는 사연. " * 5, "volatility_score": 4,
           "hard_deleted_at": "2026-06-20T01:02:03+00:00"}
    sid = promoter.promote_one(db, row, auto=True)
    assert sid is not None
    assert len(db.stories) == 1
    s = db.stories[0]
    assert s["from_capture"] is True
    assert s["origin_captured_url"] == url
    assert s["captured_hard_deleted_at"] == "2026-06-20T01:02:03+00:00"
    # 결정적 volatility(captured) 우선 저장
    assert s["volatility_score"] == 4
    assert s["citations"] == [{"title": "훈훈한 미담", "uri": url}]
    assert registered["sid"] == sid
    assert db.captured_updates[-1]["promotion_status"] == "promoted"
    assert db.captured_updates[-1]["promoted_story_id"] == sid


def test_idempotent_links_existing(monkeypatch):
    _patch_clean(monkeypatch)
    db = FakeDB()
    url = "https://theqoo.net/square/9"
    # 이미 같은 origin 으로 승격된 스토리가 존재
    db.stories.append({"id": "story-existing", "origin_captured_url": url})
    row = {"id": "c4", "url": url, "title": "t", "body_text": "본문" * 50,
           "volatility_score": 3, "hard_deleted_at": "2026-06-20T00:00:00+00:00"}
    sid = promoter.promote_one(db, row, auto=True)
    assert sid == "story-existing"
    # 새 INSERT 없이 기존 것에 연결만
    assert len(db.stories) == 1
    assert db.captured_updates[-1]["promoted_story_id"] == "story-existing"


def test_no_fit_marks_skipped(monkeypatch):
    _patch_clean(monkeypatch, gen_no_fit=True)
    db = FakeDB()
    row = {"id": "c5", "url": "https://clien.net/x/1", "title": "t",
           "body_text": "내용" * 60, "volatility_score": 5,
           "hard_deleted_at": "2026-06-20T00:00:00+00:00"}
    sid = promoter.promote_one(db, row, auto=True)
    assert sid is None
    assert db.captured_updates[-1]["promotion_status"] == "skipped"
    assert not db.stories
