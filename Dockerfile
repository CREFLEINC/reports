# syntax=docker/dockerfile:1.7
# CREFLE Reports — 런타임 이미지. 소스 코드만 포함하고 리포트 데이터는 런타임에 bind mount.
FROM python:3.12-slim

# UTF-8 고정 (proposals/ 의 한글 파일명). slim 이 이미 C.UTF-8 이지만 베이스 변경에 무관하게 명시.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=28080

WORKDIR /app

# 의존성 레이어 캐싱. pydantic 2.x / uvicorn 은 amd64 manylinux 휠 제공 → 컴파일러 불필요.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 소스만 복사. proposals/ 는 .dockerignore 로 제외하고 런타임 마운트로 주입.
COPY server.py uploads_handler.py shares.py ./

# 마운트 지점 선생성(미마운트 시에도 BASE_DIR=/app, DOCS_DIR=/app/proposals 가 깨끗히 해석되도록).
# 비루트 사용자로 실행.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/proposals \
    && chown -R appuser:appuser /app
USER appuser

# 컨테이너 노출 포트(요구사항). server.py 는 PORT 환경변수를 읽어 바인딩.
EXPOSE 28080

# server.py 의 __main__ 가 uvicorn.run(app, host=HOST, port=PORT) 실행.
CMD ["python", "server.py"]
