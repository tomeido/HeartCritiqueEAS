FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY routers/   routers/
COPY services/  services/
COPY static/    static/
COPY main.py .

EXPOSE 8000
# --workers 1 고정: 백그라운드 루프(hunter/tracker/sweeper)와 인메모리 캐시·레이트리밋이
# 단일 프로세스를 전제로 한다. 워커를 늘리면 루프가 N배 기동되어 LLM 비용·rate limit
# 폭증, 캐시 불일치가 발생한다. 수평 확장이 필요하면 루프를 별도 컨테이너로 분리할 것.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
