# HomeEye · 公开 Docker 镜像
#
# 自建 NVR：RTSP / ONVIF / 小米摄像头 -> 浏览器直播（MPEG-TS）+ 分段 MP4 录像。
#
# 基础镜像必须是 glibc 系（Debian/Ubuntu）。
# 原因只有小米那一路需要：它的 SDK 内含预编译原生库 libmiot_camera_lite.so
# （glibc 编译，依赖 __*_chk fortify 符号与 ucontext），在 alpine(musl) 上装
# gcompat 也只补 loader、补不上 fortify 符号。RTSP/ONVIF 用户其实用得上 slim 就够。
ARG PYTHON_BASE=python:3.12-slim
FROM ${PYTHON_BASE}

# 不写死构建期依赖版本，装不到东西时优先怀疑源而不是代码。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HE_HOST=0.0.0.0 \
    HE_PORT=8088 \
    HE_DATA_DIR=/data \
    HE_MILOCO_SDK=/app/sdk/miloco-sdk-main \
    TZ=Asia/Shanghai

# ffmpeg 负责取流/转封装/切片录像；tzdata 让录像按本地时间命名；
# libgomp1 是小米原生库的运行期依赖；curl 给容器健康检查用。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg tzdata ca-certificates curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 境内构建走默认 PyPI 可能很慢或超时。用 --build-arg PIP_INDEX_URL 换源，
# 不要改本文件：换源是环境问题，不是代码问题。
ARG PIP_INDEX_URL=https://pypi.org/simple

# 小米 Miloco SDK 不随本仓库分发（许可见 NOTICE.md），所以默认**不装**它的
# Python 依赖。需要小米摄像头的人两步走：
#   sh tools/fetch-miloco-sdk.sh
#   docker build --build-arg HE_WITH_MILOCO_SDK=1 -f docker/Dockerfile .
ARG HE_WITH_MILOCO_SDK=0
COPY docker/requirements-xiaomi.txt /tmp/requirements-xiaomi.txt
RUN if [ "$HE_WITH_MILOCO_SDK" = "1" ]; then \
        pip install --no-cache-dir --retries 5 --timeout 30 \
            --index-url "$PIP_INDEX_URL" -r /tmp/requirements-xiaomi.txt \
        && rm -rf /root/.cache; \
    else \
        echo "[he] 未装小米 SDK 依赖：RTSP / ONVIF / 其他流照常工作"; \
    fi

# 应用代码。HomeEye 后端只用 Python 标准库，没有自带第三方依赖。
COPY app/server /app/server
COPY app/www    /app/www
# 云台侧车 heptz（Go 静态二进制）：小米云台/对讲走 miss 私有命令，官方 SDK 不开放。
# 源码 = go2rtc 上游 + third_party/go2rtc-ptz/homeeye-ptz.patch（MIT）。
COPY app/bin    /app/bin
# 小米 SDK 目录：默认只有一个 README 占位，用户自取后这里才有真东西。
COPY app/sdk    /app/sdk

# 打包/同步过程可能丢读权限，非 root 会 dlopen 失败，这里统一放开。
# SDK 目录可能没有 .so（用户还没取），那一步允许失败。
RUN chmod -R a+rX /app/sdk /app/server /app/www /app/bin \
    && chmod a+rx /app/bin/* \
    && (find /app/sdk -name "*.so*" -exec chmod a+rx {} + || true) \
    && python -c "import json,os,sys; sys.path.insert(0,'/app/server'); import nw_config, nw_api; print('[he] image ok, version', nw_api.VERSION)"

LABEL org.opencontainers.image.title="HomeEye" \
      org.opencontainers.image.description="Self-hosted NVR: browser live view + segmented MP4 recording, backed by ffmpeg" \
      org.opencontainers.image.licenses="MIT"

# 配置 + 录像 + 登录态 + 米家授权缓存全在 /data 这一个卷里。
VOLUME ["/data"]

EXPOSE 8088 8443

HEALTHCHECK --interval=30s --timeout=8s --start-period=60s --retries=3 \
  CMD curl -sf --max-time 4 http://127.0.0.1:8088/api/health || exit 1

CMD ["python", "/app/server/main.py"]
