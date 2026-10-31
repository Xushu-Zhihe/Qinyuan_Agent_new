# Sandbox seccomp profile

`analysis.json` is the Docker default seccomp profile vendored from
[moby/profiles `seccomp/default.json`](https://github.com/moby/profiles/blob/main/seccomp/default.json)
(fetched 2026-10-31), with two rules appended for
`personality(ADDR_NO_RANDOMIZE)`:

- `262144` (`0x40000`)
- `262152` (`0x40008`, the `PER_LINUX32` variant)

The stock profile allows `personality` only for `0`, `8`, `131072`, `131080`
and `4294967295`, so gdb's ASLR disable fails with `EPERM` and every run gets
randomized addresses (see [moby#43011](https://github.com/moby/moby/issues/43011)).
Every other rule is unchanged: `defaultAction` stays `SCMP_ACT_ERRNO`,
`ptrace` is allowed for kernels >= 4.8, socket families stay restricted, and
capability-gated syscalls remain gated because the sandbox drops all caps.

The profile is applied only by the T6 command sandbox. It is passed as
`seccomp=<json>` in `HostConfig.SecurityOpt`; the Docker CLI reads a profile
file and sends its content, so the Python runtime must do the same instead of
sending a host path.
