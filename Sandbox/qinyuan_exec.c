/*
 * qinyuan-exec: run a program with PR_SET_PTRACER_ANY so gdb can attach to it.
 *
 * Background: the command sandbox runs as uid 10001 with all capabilities
 * dropped and kernel.yama.ptrace_scope=1. That combination allows a debugger
 * to trace its own children but denies `gdb -p` on a process started by a
 * previous tool call. pwntools already does this prctl for process() tubes;
 * this launcher gives the shell/agent the same ability without root or caps.
 *
 * The permission is scoped to the caller's uid inside its own PID namespace:
 * it does not widen the container boundary and grants no capability.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/prctl.h>
#include <unistd.h>

#ifndef PR_SET_PTRACER
#define PR_SET_PTRACER 0x59616d61
#endif

#define PR_SET_PTRACER_ANY ((unsigned long)-1)

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: qinyuan-exec PROGRAM [ARG...]\n");
        return 2;
    }
    if (prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0) != 0) {
        fprintf(stderr, "qinyuan-exec: prctl(PR_SET_PTRACER) failed: %s\n",
                strerror(errno));
        return 1;
    }
    execvp(argv[1], &argv[1]);
    fprintf(stderr, "qinyuan-exec: exec %s failed: %s\n", argv[1],
            strerror(errno));
    return 127;
}
