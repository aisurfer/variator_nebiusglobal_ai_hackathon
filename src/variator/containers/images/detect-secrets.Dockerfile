# detect-secrets (Apache-2.0): secret patterns and high-entropy strings. The report goes to stdout,
# so the recipe runs it through sh and writes /out/detect-secrets.json.
FROM python:3.12.7-slim
RUN pip install --no-cache-dir detect-secrets==1.5.0
ENTRYPOINT ["sh", "-c"]
