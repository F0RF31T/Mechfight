# Build from this folder: docker build -t my-bot .
# Pin the base image further (e.g. python:3.13.x-slim-bookworm) once you've confirmed a tag you like.
FROM python:3.13-slim
WORKDIR /bot
COPY mechfight.py bot.py ./
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
