//! Heart & Critique — tracker 텍스트 파이프라인 네이티브 가속.
//!
//! 포팅 대상은 services/tracker.py 의 두 CPU 핫패스뿐이다:
//!   1. `visible_text(html)`  — script/style/주석/태그 제거 + 공백 접기
//!   2. `Scanner.scan(text)`  — 삭제/차단/봇챌린지 패턴 3종 검색(스니펫 반환)
//!
//! 의미론 계약 (tests/test_nativetext.py 패리티 테스트가 고정):
//!   · 탐지 패턴(삭제/차단/봇)은 **파이썬 쪽 re.compile 원본 문자열을 그대로 받아**
//!     컴파일한다(이중 정의 드리프트 방지). rust regex 가 못 받는 문법(역참조·룩어라운드)이
//!     들어오면 컴파일 에러 → 파이썬 쪽 래퍼가 순수 파이썬 폴백으로 전환한다.
//!   · visible_text 의 구조 정규식은 알고리즘의 일부라 여기 고정 재현한다.
//!     원본 `<(script|style|noscript|template)\b.*?(?:</\1\s*>|\Z)` 의 역참조는 rust regex 미지원
//!     → 태그별 4개 대안으로 전개(의미 동일: 같은 태그로 닫힐 때만 매치).
//!   · 공백 클래스는 파이썬 re 의 \s(유니코드 + U+001C..1F 포함)와 맞추기 위해
//!     `[\s\x1C-\x1F]` 를 쓴다(러스트 \s 는 White_Space 프로퍼티라 001C-1F 미포함).
//!   · rust regex 는 파이썬과 같은 leftmost-first(앞 대안 우선) 매칭이라
//!     search/find 결과 스니펫이 일치한다.
//!
//! 무거운 작업은 전부 `allow_threads` 로 GIL 을 풀고 수행 — 이벤트 루프 스톨 제거.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use regex::{Regex, RegexBuilder};
use std::sync::OnceLock;

fn re_strip_block() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(
            r"(?is)<!--.*?(?:-->|\z)|<script\b.*?(?:</script\s*>|\z)|<style\b.*?(?:</style\s*>|\z)|<noscript\b.*?(?:</noscript\s*>|\z)|<template\b.*?(?:</template\s*>|\z)",
        )
        .expect("strip_block regex")
    })
}

fn re_any_tag() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r"<[^>]+>").expect("any_tag regex"))
}

fn re_ws() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    // 파이썬 \s 패리티: 유니코드 White_Space + U+001C..001F(파이썬은 공백 취급)
    R.get_or_init(|| Regex::new(r"[\s\x1C-\x1F]+").expect("ws regex"))
}

fn extract_visible(html: &str) -> String {
    let s = re_strip_block().replace_all(html, " ");
    let s = re_any_tag().replace_all(&s, " ");
    let s = re_ws().replace_all(&s, " ");
    s.trim().to_string()
}

/// HTML → 가시 텍스트 (tracker._visible_text 와 동일 의미론).
#[pyfunction]
fn visible_text(py: Python<'_>, html: String) -> String {
    py.allow_threads(move || extract_visible(&html))
}

/// 최상위(괄호/문자클래스 밖) `|` 기준으로 대안을 분리한다.
///
/// 왜: 큰 알ternation 전체에 case_insensitive 를 걸면 regex crate 의 리터럴
/// 프리필터가 포기되어 lazy DFA 풀스캔(80KB 텍스트에 ~14ms)으로 떨어진다.
/// 대안을 개별 컴파일하면 각자 강한 리터럴 프리필터를 유지해 총합 ~0.2-0.3ms.
fn split_top_level(pattern: &str) -> Vec<String> {
    let mut parts = Vec::new();
    let mut depth = 0i32;
    let mut in_class = false;
    let mut esc = false;
    let mut cur = String::new();
    for ch in pattern.chars() {
        if esc {
            cur.push(ch);
            esc = false;
            continue;
        }
        match ch {
            '\\' => {
                cur.push(ch);
                esc = true;
            }
            '[' if !in_class => {
                cur.push(ch);
                in_class = true;
            }
            ']' if in_class => {
                cur.push(ch);
                in_class = false;
            }
            '(' if !in_class => {
                cur.push(ch);
                depth += 1;
            }
            ')' if !in_class => {
                cur.push(ch);
                depth -= 1;
            }
            '|' if !in_class && depth == 0 => {
                parts.push(std::mem::take(&mut cur));
            }
            _ => cur.push(ch),
        }
    }
    parts.push(cur);
    parts
}

/// 대안 분리 컴파일 + leftmost-first 재현 멀티 매처.
///
/// 파이썬 re 의 알터네이션 의미론: 가장 왼쪽 시작 위치가 우선, 같은 위치면
/// 앞선 대안이 우선. (시작위치, 대안 인덱스) 최소값 선택이 이를 정확히 재현한다.
struct MultiRegex {
    alts: Vec<Regex>,
}

impl MultiRegex {
    fn build(pattern: &str, name: &str) -> PyResult<Self> {
        let alts = split_top_level(pattern)
            .iter()
            .map(|a| {
                RegexBuilder::new(a)
                    .case_insensitive(true) // 파이썬 쪽 re.IGNORECASE 와 동일
                    .size_limit(16 * 1024 * 1024)
                    .build()
                    .map_err(|e| {
                        PyValueError::new_err(format!("{name} 패턴 컴파일 실패: {e}"))
                    })
            })
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Self { alts })
    }

    fn find_first(&self, hay: &str) -> Option<String> {
        let mut best: Option<(usize, usize, &str)> = None; // (start, alt_idx, text)
        for (i, re) in self.alts.iter().enumerate() {
            if let Some(m) = re.find(hay) {
                let key = (m.start(), i);
                if best.map_or(true, |(s, j, _)| key < (s, j)) {
                    best = Some((m.start(), i, m.as_str()));
                }
            }
        }
        best.map(|(_, _, t)| t.to_string())
    }

    fn is_match(&self, hay: &str) -> bool {
        self.alts.iter().any(|r| r.is_match(hay))
    }
}

/// 삭제/차단/봇챌린지 패턴 스캐너. 패턴 문자열은 파이썬 re.compile 원본을 그대로 받는다.
#[pyclass(frozen)]
struct Scanner {
    del: MultiRegex,
    blk: MultiRegex,
    bot: MultiRegex,
}

#[pymethods]
impl Scanner {
    #[new]
    fn new(del_pattern: &str, blk_pattern: &str, bot_pattern: &str) -> PyResult<Self> {
        Ok(Self {
            del: MultiRegex::build(del_pattern, "deletion")?,
            blk: MultiRegex::build(blk_pattern, "blocked")?,
            bot: MultiRegex::build(bot_pattern, "bot_challenge")?,
        })
    }

    /// (삭제 매치 스니펫|None, 차단 매치 스니펫|None, 봇챌린지 여부)
    fn scan(&self, py: Python<'_>, text: String) -> (Option<String>, Option<String>, bool) {
        py.allow_threads(move || {
            (
                self.del.find_first(&text),
                self.blk.find_first(&text),
                self.bot.is_match(&text),
            )
        })
    }

    /// visible_text + scan 을 한 번의 GIL 해제 구간에서 수행.
    /// 반환: (가시 텍스트, 삭제 스니펫|None, 차단 스니펫|None, 봇챌린지 여부)
    fn extract_and_scan(
        &self,
        py: Python<'_>,
        html: String,
    ) -> (String, Option<String>, Option<String>, bool) {
        py.allow_threads(move || {
            let text = extract_visible(&html);
            let d = self.del.find_first(&text);
            let b = self.blk.find_first(&text);
            let bot = self.bot.is_match(&text);
            (text, d, b, bot)
        })
    }
}

#[pymodule]
fn hc_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(visible_text, m)?)?;
    m.add_class::<Scanner>()?;
    m.add("__engine__", "rust-regex")?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
