# heatwise-hsi-lst-prep: HSI/LST preprocessing EOAP processor.
#
# Plain python:slim (not a GDAL-based image) is enough here: rasterio's pip
# wheels already bundle GDAL, and this repo's other dependencies (numpy,
# scipy, scikit-learn, affine, pyyaml) don't need geopandas/fiona's system
# GDAL linkage (unlike heatwise-patch-extraction).
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libexpat1 \
    libgdal-dev \
    gdal-bin \
    && rm -rf /var/lib/apt/lists/*

# cwltool (and some other CWL runners) run the container as an arbitrary
# numeric UID (--user=1000:1000 by default) with no matching /etc/passwd
# entry. Some libraries call getpass.getuser()/pwd.getpwuid() and crash with
# `KeyError: getpwuid(): uid not found` if that lookup fails -- confirmed by
# an actual cwltool run against heatwise-lcz-classification's identical setup
# (triggered deep inside torch's dynamo cache-dir resolution). Pre-creating a
# UID-1000 user here means the lookup succeeds regardless of which repo/deps
# end up calling it.
RUN useradd --create-home --uid 1000 --shell /bin/bash appuser

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "processor.py"]