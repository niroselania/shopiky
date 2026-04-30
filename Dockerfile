FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py shopify_pdf_parser.py ./

EXPOSE 5055

# PDF parsing puede tardar subidas grandes
CMD ["gunicorn", "--bind", "0.0.0.0:5055", "--workers", "2", "--threads", "4", "--timeout", "300", "--limit-request-line", "8192", "--limit-request-field_size", "16384", "app:app"]
