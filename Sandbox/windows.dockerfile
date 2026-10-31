# Dedicated Windows user-mode profile. This image is NOT used by the default
# Linux TaskRun: it is selected only through the explicit windows sandbox
# profile, because Wine needs a larger writable prefix and more memory than the
# 512 MiB default sandbox.
FROM qinyuan/pentest-sandbox:0.3.7

USER root

# `wine` is the Debian auto-dispatch wrapper. It depends on `wine64 | wine32`,
# so installing `wine` together with `wine32` can satisfy the dependency with
# the 32-bit loader only; both loaders must be named explicitly. With both
# present, WINEARCH=win64 uses wine64 and WINEARCH=win32 uses wine32.
# gcc-mingw-w64-i686 is the 32-bit cross compiler used to build test harnesses
# and exploit stubs for PE32 targets.
RUN dpkg --add-architecture i386 \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get install --no-install-recommends -y \
        wine \
        wine64 \
        wine32 \
        gcc-mingw-w64-i686 \
    && rm -rf /var/lib/apt/lists/* \
    && wine --version \
    && i686-w64-mingw32-gcc --version | head -n 1

ENV WINEPREFIX=/workspace/work/.wine64 \
    WINEPREFIX32=/workspace/work/.wine32 \
    WINEARCH=win64 \
    WINEDEBUG=-all

# Debian's /usr/bin/wine always prefers wine32 when it exists. Pin the loader
# and prefix explicitly so 64-bit and 32-bit PE are both deterministic.
COPY Sandbox/qy_wine.sh /usr/local/bin/qy-wine
RUN chmod 0755 /usr/local/bin/qy-wine \
    && ln -sf qy-wine /usr/local/bin/qy-wine64 \
    && ln -sf qy-wine /usr/local/bin/qy-wine32 \
    && ln -sf qy-wine /usr/local/bin/qy-wine-auto \
    && qy-wine64 --version \
    && qy-wine32 --version

USER 10001:10001
WORKDIR /workspace
