"""분석/초안: 근거 검증, 재생성, 자막 없을 때 fallback"""
from __future__ import annotations

from app.agent import analyze, check_draft, make_draft
from app.config import load_config
from app.store import Store
from tests.fakes import FakeLLM, default_draft, fake_transcript, make_video, no_transcript


def setup(tmp_path):
    cfg = load_config()
    cfg.db_path = tmp_path / "t.db"
    cfg.disclosure = "(협찬)"
    return cfg, Store.from_config(cfg)


def test_evidence_not_in_transcript_is_dropped(tmp_path):
    cfg, _ = setup(tmp_path)
    a = analyze(make_video(1), cfg, FakeLLM(), [], fake_transcript)
    kws = [e["keyword"] for e in a["evidence"]]
    assert kws == ["답장 텀", "읽씹"]                     # 자막에 없는 '의무가 아니라 성의'는 제외
    assert all(e["verified"] for e in a["evidence"])
    assert a["source"] == "transcript"


def test_no_transcript_uses_video_input_with_gemini(tmp_path):
    cfg, _ = setup(tmp_path)
    llm = FakeLLM()
    a = analyze(make_video(1), cfg, llm, [], no_transcript)
    assert a["source"] == "video" and llm.video_urls[0].endswith("vid00000001")
    assert all(not e["verified"] for e in a["evidence"])   # 영상 직접 분석은 '확인 불가'로 표시


def test_no_transcript_no_video_falls_back_to_metadata(tmp_path):
    cfg, _ = setup(tmp_path)
    cfg.ai.use_video_input = False
    llm = FakeLLM(evidence=[{"time": "", "quote": "연락 텀, 읽씹, 마음이 식는 신호", "keyword": "마음이 식는"}])
    a = analyze(make_video(1), cfg, llm, [], no_transcript)
    assert a["source"] == "metadata" and a["evidence"][0]["keyword"] == "마음이 식는"
    assert llm.video_urls[0] is None


def test_hallucinated_everything_falls_back_to_title(tmp_path):
    cfg, _ = setup(tmp_path)
    llm = FakeLLM(evidence=[{"time": "01:00", "quote": "없는 말", "keyword": "없는말입니다"}])
    a = analyze(make_video(1), cfg, llm, [], fake_transcript)
    assert len(a["evidence"]) == 1 and "제목 사용" in a["source_note"]


def test_draft_passes_first_try(tmp_path):
    cfg, store = setup(tmp_path)
    r = make_draft(make_video(1), cfg, FakeLLM(), store, [], fake_transcript)
    assert r.ok and r.attempts == 1 and r.checks == []
    assert store.get_draft("vid00000001")["comment"] == r.comment


def test_bad_drafts_are_regenerated_with_feedback(tmp_path):
    cfg, store = setup(tmp_path)
    bad1 = "너무 짧은 댓글 운명원"                                                   # 길이·근거
    bad2 = "영상에서 답장 텀 얘기 공감돼요. 운명원에서 연애사주 봤는데 소름 돋게 맞더라고요 진짜로요"  # 지어낸 후기
    llm = FakeLLM(drafts=[bad1, bad2])
    r = make_draft(make_video(1), cfg, llm, store, [], fake_transcript)
    assert r.ok and r.attempts == 3 and len(r.history) == 2
    writer_prompts = [p for p in llm.prompts if "[규칙]" in p]
    assert "이전 초안의 문제" in writer_prompts[1] and "자였습니다" in writer_prompts[1]
    assert "직접 이용해 본 것처럼" in writer_prompts[2]


def test_disclosure_is_appended_if_model_forgets(tmp_path):
    cfg, store = setup(tmp_path)
    llm = FakeLLM(drafts=[default_draft(1).replace(" (협찬)", "")])
    r = make_draft(make_video(1), cfg, llm, store, [], fake_transcript)
    assert r.comment.endswith("(협찬)") and r.ok


def test_gives_up_after_max_attempts_but_returns_last_draft(tmp_path):
    cfg, store = setup(tmp_path)
    cfg.ai.max_attempts = 2
    r = make_draft(make_video(1), cfg, FakeLLM(drafts=["짧음 운명원", "또 짧음 운명원"]), store, [], fake_transcript)
    assert not r.ok and r.comment and any(c["code"] == "LENGTH" for c in r.checks)


def test_no_evidence_check(tmp_path):
    cfg, store = setup(tmp_path)
    ev = [{"keyword": "답장 텀"}]
    text = "영상 보고 연애가 참 어렵다는 생각이 들었어요. 저도 요즘 고민이 많아서 운명원 연애사주 한번 보고 싶네요 (협찬)"
    assert [c["code"] for c in check_draft(text, ev, cfg, store)] == ["NO_EVIDENCE"]
    assert check_draft(text.replace("영상 보고", "답장 텀 얘기 보고"), ev, cfg, store) == []


def test_recent_comments_are_passed_to_writer(tmp_path):
    cfg, store = setup(tmp_path)
    store.finish(store.reserve("vid00000009", "유튜브1", "예전에 게시한 댓글 문장"), True)
    llm = FakeLLM()
    make_draft(make_video(1), cfg, llm, store, [], fake_transcript)
    assert "예전에 게시한 댓글 문장" in [p for p in llm.prompts if "[규칙]" in p][0]


def test_disclosure_off(tmp_path):
    """disclosure가 비어 있으면 검사/자동 추가/프롬프트 안내 모두 없음"""
    cfg, store = setup(tmp_path)
    cfg.disclosure = ""
    text = default_draft(1).replace(" (협찬)", "")
    llm = FakeLLM(drafts=[text])
    r = make_draft(make_video(1), cfg, llm, store, [], fake_transcript)
    assert r.ok and r.comment == text and "협찬" not in r.comment
    assert "광고 표시" not in [p for p in llm.prompts if "[규칙]" in p][0]
