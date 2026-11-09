"""Bounded binary read through a fixed non-root worker, outside tool stdout."""
import threading

MAX_IMAGE_SOURCE_BYTES = 8 * 1024 * 1024


def validate_image_path(path):
    if (not isinstance(path, str) or len(path.encode('utf-8')) > 4096
            or not path.startswith(('/workspace/input/', '/workspace/work/'))
            or any(p in ('', '.', '..') for p in path.split('/')[1:])
            or '\\' in path or any(ord(c) < 32 for c in path)):
        raise ValueError('image.path_rejected')
    return path


READ_IMAGE = r'''import os,stat,sys
fds=[]
try:
    current=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(current)
    parts=sys.argv[1].split('/')[1:]
    for part in parts[:-1]:
        current=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current);fds.append(current)
    fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=current);fds.append(fd)
    before=os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size>8388608: raise ValueError()
    data=os.read(fd,8388609)
    after=os.fstat(fd)
    if len(data)!=before.st_size or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns): raise ValueError()
    sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
except Exception:
    raise SystemExit(1)
finally:
    for fd in reversed(fds):os.close(fd)
'''


def read_image(sandbox, sandbox_id, path, timeout=30):
    from docker.utils.socket import next_frame_header, read_exactly
    validate_image_path(path)
    binding = sandbox._binding(sandbox_id)
    if not binding.execution_lock.acquire(timeout=timeout):
        raise TimeoutError('image.read_timeout')
    sock, done = None, threading.Event()
    try:
        sandbox._verify_active(binding.task_run_id, sandbox_id)
        api = sandbox._runtime.client.api
        with sandbox._admit(binding):
            execution = api.exec_create(sandbox_id, ['python3', '-I', '-c', READ_IMAGE, path],
                stdout=True, stderr=True, tty=False, user='10001:10001')
        with sandbox._admit(binding):
            sock = api.exec_start(execution['Id'], socket=True, tty=False)
        raw = getattr(sock, '_sock', sock)
        def expire():
            if not done.wait(timeout):
                raw.close()
        threading.Thread(target=expire, daemon=True).start()
        output, errors = bytearray(), bytearray()
        while True:
            stream, length = next_frame_header(raw)
            if stream < 0:
                break
            if stream not in (1, 2) or length < 0 or len(output) + len(errors) + length > MAX_IMAGE_SOURCE_BYTES:
                raise ValueError('image.read_limit')
            (output if stream == 1 else errors).extend(read_exactly(raw, length))
        state = api.exec_inspect(execution['Id'])
        if state.get('Running') is not False or state.get('ExitCode') != 0 or errors:
            raise ValueError('image.read_rejected')
        return bytes(output)
    finally:
        done.set()
        if sock is not None:
            sock.close()
        binding.execution_lock.release()
