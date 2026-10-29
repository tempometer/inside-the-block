FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir solders==0.29.0

# toolchain — self-contained copy, no external files needed
COPY itb_sender.py verify.py analyze.py leader_watch.py crosscheck_versions.py itb_campaign.py ./
COPY p0-*.json ./
COPY REPORT.md ./
COPY data/ ./data/

# DRY by default — live sending needs an explicit flag AND a funded key in /app/keys
CMD ["python", "itb_campaign.py", "--dry"]
