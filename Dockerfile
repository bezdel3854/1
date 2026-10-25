FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MODE=paper DB_PATH=/data/paper.sqlite PORT=8080
WORKDIR /app
RUN groupadd --gid 10001 bot && useradd --uid 10001 --gid bot --create-home bot && mkdir /data && chown bot:bot /data
COPY --chown=bot:bot bingx_bot /app/bingx_bot
USER bot
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/health',timeout=4)"
CMD ["python", "-m", "bingx_bot.cli", "paper"]
