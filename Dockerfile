# Use a stable Python version supported by the QQ SDK rather than the host's
# development Python runtime.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py rizline.py rizline_b40.py rizline_bindings.py rizline_cos.py rizline_qq_login.py rizline_token_vault.py ./
COPY tools ./tools

CMD ["python", "bot.py"]
