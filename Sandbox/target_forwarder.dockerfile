# TaskRun-scoped TCP/HTTP target forwarder. It only needs the Python standard
# library and the two fixed scripts, so keep it small instead of inheriting the
# full analysis image.
FROM debian:bookworm-slim

LABEL qinyuan.target.private_ipv4="rfc1918"

RUN apt-get -o Acquire::Retries=5 update \
    && apt-get install --no-install-recommends -y python3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --shell /usr/sbin/nologin sandbox \
    && mkdir -p /opt/qinyuan

COPY Sandbox/target_address.py /opt/qinyuan/target_address.py
COPY Sandbox/target_forwarder.py /opt/qinyuan/target_forwarder.py
RUN chmod 0444 /opt/qinyuan/target_address.py /opt/qinyuan/target_forwarder.py

USER 10001:10001
ENTRYPOINT ["python3", "-I", "/opt/qinyuan/target_forwarder.py"]
