FROM qinyuan/pentest-sandbox:0.3.4

LABEL qinyuan.target.private_ipv4="rfc1918"

USER 0:0
COPY Sandbox/target_address.py /opt/qinyuan/target_address.py
COPY Sandbox/target_forwarder.py /opt/qinyuan/target_forwarder.py
RUN chmod 0444 /opt/qinyuan/target_address.py /opt/qinyuan/target_forwarder.py

USER 10001:10001
ENTRYPOINT ["python3", "-I", "/opt/qinyuan/target_forwarder.py"]
