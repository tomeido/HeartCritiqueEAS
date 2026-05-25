FROM python:3.12-slim

WORKDIR /app

COPY api/ api/
COPY agent.py .

EXPOSE 9999

CMD ["python", "agent.py"]
