# Labeling lab: the scorer's base image + the pinned sqlglot checkout + the labeling environment.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock coverage
RUN python - <<'P'
import urllib.request, tarfile, io, os
sha = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
data = urllib.request.urlopen(f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz").read()
tarfile.open(fileobj=io.BytesIO(data)).extractall("/opt")
os.rename(f"/opt/sqlglot-{sha}", "/opt/sqlglot")
P
RUN echo "import coverage; coverage.process_startup()" > /usr/local/lib/python3.13/site-packages/zz_cov.pth
ENV PYTHONPATH=/opt/sqlglot SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/sqlglot
