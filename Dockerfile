# ── 네이티브 가속 빌더: native/(Rust, PyO3) → hc_native abi3 휠 ───────────────
# tracker 텍스트 파이프라인(가시텍스트 추출+삭제/차단/봇 패턴 매칭)의 CPU 가속.
# 이 스테이지가 없어도(휠 미설치) 앱은 services/nativetext.py 의 파이썬 폴백으로
# 동일하게 동작한다 — 빌드 실패 시 이 스테이지만 걷어내면 됨.
FROM python:3.12-slim AS native-builder
RUN apt-get update && apt-get install -y --no-install-recommends curl build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"
RUN pip install --no-cache-dir maturin
COPY native/ native/
RUN cd native && maturin build --release --out /wheels

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# hc_native(abi3-py310 휠): 파이썬 버전과 무관하게 설치 가능
COPY --from=native-builder /wheels /tmp/wheels
RUN pip install --no-cache-dir /tmp/wheels/*.whl && rm -rf /tmp/wheels

COPY routers/   routers/
COPY services/  services/
COPY app/       app/
COPY scripts/   scripts/
COPY static/    static/
COPY main.py .

EXPOSE 8000
# --workers 1 고정: 백그라운드 루프(hunter/tracker/sweeper)와 인메모리 캐시·레이트리밋이
# 단일 프로세스를 전제로 한다. 워커를 늘리면 루프가 N배 기동되어 LLM 비용·rate limit
# 폭증, 캐시 불일치가 발생한다. 수평 확장이 필요하면 루프를 별도 컨테이너로 분리할 것.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
