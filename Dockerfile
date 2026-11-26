FROM python:3.11-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    libx11-6 \
    libxxf86vm1 \
    libxfixes3 \
    libxi6 \
    libxrender1 \
    libxkbcommon0 \
    libsm6 \
    libice6 \
    libgl1 \
    libegl1 \
    libgomp1 \
    libdbus-1-3 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=180 \
    PIP_RETRIES=20 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn mirrors.aliyun.com"

RUN pip install --no-cache-dir \
    "numpy>=1.26,<2.0" \
    cython \
    requests \
    zstandard

COPY vendor-wheels/bpy-4.5.12-cp311-cp311-manylinux_2_28_x86_64.whl \
    /wheels/bpy-4.5.12-cp311-cp311-manylinux_2_28_x86_64.whl
RUN pip install --no-cache-dir --no-deps \
    /wheels/bpy-4.5.12-cp311-cp311-manylinux_2_28_x86_64.whl \
    && rm -rf /wheels

COPY . /app
RUN rm -rf /app/vendor-wheels

ENV CUSTOMDIY_BLENDER_HOST=0.0.0.0 \
    CUSTOMDIY_BLENDER_PORT=8765 \
    PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["python", "server.py"]
