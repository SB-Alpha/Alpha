# 빌드 컨텍스트는 infra/db_migration (커넥터 템플릿을 함께 넣는다)
#   docker build -f agent/Dockerfile -t alpha-cdc-agent .
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get purge -y gnupg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agent/pyproject.toml ./
COPY agent/cdc_agent ./cdc_agent
RUN pip install --no-cache-dir .
COPY connectors /app/connectors

# Connect 컨테이너와 같은 uid: 노드의 secrets 폴더(700)를 함께 읽는다.
RUN useradd --uid 1001 --no-create-home agent
USER agent

EXPOSE 8080
CMD ["uvicorn", "cdc_agent.main:app", "--host", "0.0.0.0", "--port", "8080"]
