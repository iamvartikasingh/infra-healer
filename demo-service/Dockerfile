FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
RUN useradd -u 10001 app && mkdir /state && chown app /state
USER 10001
EXPOSE 8080
CMD ["python", "app.py"]
