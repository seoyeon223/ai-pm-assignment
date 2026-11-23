"""댓글 내용 규칙. AI 초안이든 직접 고친 문장이든 게시 전에 여기서 검사."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .config import Config

URL_RE = re.compile(
    r"(https?://|www\.|[A-Za-z0-9-]+\.(com|net|org|kr|co|io|me|ly|gl|be|link|shop|site)\b)",
    re.IGNORECASE,
)
HASHTAG_RE = re.compile(r"#\S")

# 브랜드를 써 본 것처럼 쓰는 표현. '영상 봤는데' 같은 말까지 막지 않도록 브랜드가 있는 문장에서만 본다.
FAKE_REVIEW_RE = re.compile(
    r"(봤는데|봤더니|봤어요|봤음|받아\s*봤|받았는데|받았더니|받고\s*왔|다녀\s*왔|가\s*봤|"
    r"이용해\s*봤|상담\s*받|보고\s*왔|맞더라|맞았어|맞췄|소름\s*돋게\s*맞|후기\s*남)"
)
SENTENCE_SPLIT_RE = re.compile(r"[.!?~…\n]+|ㅠ{2,}|ㅜ{2,}")


@dataclass
class Violation:
    code: str
    message: str


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text or "")).strip()


def char_len(text: str) -> int:
    return len(normalize(text))


def validate_comment(text: str, cfg: Config) -> list[Violation]:
    t = normalize(text)
    out: list[Violation] = []
    n = len(t)
    if not (cfg.min_chars <= n <= cfg.max_chars):
        out.append(Violation("LENGTH", f"{n}자 — {cfg.min_chars}~{cfg.max_chars}자여야 합니다."))
    if URL_RE.search(t):
        out.append(Violation("LINK", "링크(주소)는 넣을 수 없습니다."))
    if HASHTAG_RE.search(t):
        out.append(Violation("HASHTAG", "해시태그는 넣을 수 없습니다."))
    if cfg.require_brand and cfg.brand and cfg.brand not in t:
        out.append(Violation("NO_BRAND", f"'{cfg.brand}'이(가) 들어가야 합니다."))
    if cfg.disclosure and cfg.disclosure not in t:
        out.append(Violation("NO_DISCLOSURE", f"광고 표시 '{cfg.disclosure}'이(가) 들어가야 합니다."))
    if cfg.brand:
        for sent in SENTENCE_SPLIT_RE.split(t):
            if cfg.brand in sent and FAKE_REVIEW_RE.search(sent):
                out.append(Violation("FAKE_REVIEW",
                                     f"'{cfg.brand}'을(를) 직접 이용해 본 것처럼 쓴 표현이 있습니다: “{sent.strip()}”"))
                break
    return out


def _grams(s: str, n: int = 3) -> set[str]:
    s = re.sub(r"\s+", "", normalize(s))
    return {s[i:i + n] for i in range(max(len(s) - n + 1, 1))}


def similarity(a: str, b: str) -> float:
    """문자 3-gram 자카드 유사도 (0~1)"""
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def most_similar(text: str, others: list[str], ignore: list[str] | None = None) -> tuple[float, str | None]:
    """브랜드명/광고 표시처럼 매번 들어가는 문구는 빼고 비교."""
    def strip(s: str) -> str:
        for w in ignore or []:
            if w:
                s = s.replace(w, "")
        return s
    base = strip(text)
    best, best_text = 0.0, None
    for o in others:
        s = similarity(base, strip(o))
        if s > best:
            best, best_text = s, o
    return best, best_text
