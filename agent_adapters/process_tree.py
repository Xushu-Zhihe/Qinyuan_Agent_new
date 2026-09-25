"""Own only the CLI worker's descendants; no process-name based termination."""

from __future__ import annotations

import os
import signal


class ProcessTree:
    def __init__(self, pid):
        self.pid, self.handle = pid, None
        if os.name != "nt":
            return
        import ctypes as c
        from ctypes import wintypes as w

        class Limits(c.Structure):
            _fields_ = [
                ("process_time", c.c_int64),
                ("job_time", c.c_int64),
                ("flags", w.DWORD),
                ("min_working", c.c_size_t),
                ("max_working", c.c_size_t),
                ("active_processes", w.DWORD),
                ("affinity", c.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class IO(c.Structure):
            _fields_ = [
                (name, c.c_uint64)
                for name in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class Extended(c.Structure):
            _fields_ = [
                ("limits", Limits),
                ("io", IO),
                ("process_memory", c.c_size_t),
                ("job_memory", c.c_size_t),
                ("peak_process_memory", c.c_size_t),
                ("peak_job_memory", c.c_size_t),
            ]

        k = c.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes, k.CreateJobObjectW.restype = (
            [c.c_void_p, w.LPCWSTR],
            w.HANDLE,
        )
        k.SetInformationJobObject.argtypes, k.SetInformationJobObject.restype = (
            [w.HANDLE, c.c_int, c.c_void_p, w.DWORD],
            w.BOOL,
        )
        k.OpenProcess.argtypes, k.OpenProcess.restype = (
            [w.DWORD, w.BOOL, w.DWORD],
            w.HANDLE,
        )
        k.AssignProcessToJobObject.argtypes, k.AssignProcessToJobObject.restype = (
            [w.HANDLE, w.HANDLE],
            w.BOOL,
        )
        k.CloseHandle.argtypes, k.CloseHandle.restype = [w.HANDLE], w.BOOL
        self.kernel = k
        job = k.CreateJobObjectW(None, None)
        if not job:
            raise OSError("worker_job_creation_failed")
        try:
            settings = Extended()
            settings.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(
                job, 9, c.byref(settings), c.sizeof(settings)
            ):
                raise OSError("worker_job_limits_failed")
            handle = k.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            if not handle:
                raise OSError("worker_process_open_failed")
            try:
                if not k.AssignProcessToJobObject(job, handle):
                    raise OSError("worker_job_assignment_failed")
            finally:
                k.CloseHandle(handle)
            self.handle = job
        except BaseException:
            k.CloseHandle(job)
            raise

    def close(self):
        if os.name == "nt":
            if self.handle is not None:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
        else:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
