# Dedicated Windows user-mode profile. This image is NOT used by the default
# Linux TaskRun: it is selected only through the explicit windows sandbox
# profile, because Wine needs a larger writable prefix and more memory than the
# 512 MiB default sandbox.
FROM qinyuan/pentest-sandbox:0.3.6

USER root

# `wine` is the Debian wrapper that dispatches to wine64; the wine64 package
# alone only installs /usr/lib/wine/wine64 and no /usr/bin entry point. A
# missing wine32 is harmless for win64 prefixes (WINEARCH=win64).
RUN apt-get -o Acquire::Retries=5 update \
    && apt-get install --no-install-recommends -y \
        wine \
    && rm -rf /var/lib/apt/lists/* \
    && wine --version

ENV WINEPREFIX=/workspace/work/.wine \
    WINEARCH=win64 \
    WINEDEBUG=-all

USER 10001:10001
WORKDIR /workspace
