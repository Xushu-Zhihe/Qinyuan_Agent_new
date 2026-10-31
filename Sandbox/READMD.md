构建：

docker build `
  --progress=plain `
  --provenance=false `
  --sbom=false `
  --file Sandbox/dockerfile `
  --tag qinyuan/pentest-sandbox:0.3.5 `
  .

构建后把 `docker image inspect qinyuan/pentest-sandbox:0.3.5 --format '{{.Id}}'`
的结果同步到 `agent_adapters/docker_profile.py` 的 `IMAGE_ID`，并把 `profile`
版本递增；D9 会按固定镜像 ID 和完整容器 spec 校验运行配置。

0.3.5 相比 0.3.4 的调试链变化：gdbserver/tmux/patchelf/strace/ltrace/
socat/nc/gcc/qemu-user-static/gdb-multiarch/one_gadget、GEF（/etc/gdb/gdbinit）、
pwntools-terminal（无 TTY 时用 script(1) 分配 pty）、可写 $HOME 与
`/opt/qinyuan/python/bin` PATH、以及只放宽 `personality(ADDR_NO_RANDOMIZE)`
的 seccomp 窄档（`Sandbox/seccomp/analysis.json`）。
