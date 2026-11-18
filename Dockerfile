FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Asia/Kolkata
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY dhan_autotrader ./dhan_autotrader

# logs/ and state/ are mounted as volumes so trades, logs and crash-recovery state survive restarts
RUN useradd --create-home trader && mkdir -p logs state && chown trader:trader logs state
USER trader

STOPSIGNAL SIGINT
ENTRYPOINT ["python", "-m", "dhan_autotrader"]
# Paper by default. For live: TRADING_MODE=live in .env AND add --confirm-live here / in compose.
CMD ["--daemon"]
