"""A networkless Python process in the existing container, with HTTP over stdio."""
import json
import math
import time

from Sandbox.task_run_sandbox import TaskRunSandboxError

MAX_SCRIPT_BYTES = 64 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024

# The filter is installed before compiling or running any model code. No library
# installation, privileges, new container, network change or model key is needed.
WORKER = r'''
import base64,ctypes,errno,json,os,platform,sys
wire_in,wire_out=sys.stdin,sys.stdout
def emit(value):
    wire_out.write(json.dumps(value,ensure_ascii=True,allow_nan=False)+'\n');wire_out.flush()
def isolate():
    arch=platform.machine().lower()
    if arch in ('x86_64','amd64'):
        audit=0xc000003e
        denied=(41,42,43,44,45,49,50,53,56,57,58,59,62,101,234,272,308,310,311,321,322,424,425,426,427,434,435,438)
    elif arch in ('aarch64','arm64'):
        audit=0xc00000b7
        denied=(97,117,129,130,131,198,199,200,201,202,203,206,211,220,221,242,270,271,280,281,424,425,426,427,434,435,438)
    else:raise RuntimeError('unsupported_architecture')
    class Ins(ctypes.Structure):
        _fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint)]
    class Prog(ctypes.Structure):
        _fields_=[('len',ctypes.c_ushort),('filter',ctypes.POINTER(Ins))]
    # Audit architecture first; reject alternate ABI/x32 syscall numbers.
    code=[(0x20,0,0,4),(0x15,1,0,audit),(0x06,0,0,0x80000000),
          (0x20,0,0,0),(0x35,0,1,0x40000000),(0x06,0,0,0x00050000|errno.EPERM)]
    for nr in denied:code.extend(((0x15,0,1,nr),(0x06,0,0,0x00050000|errno.EPERM)))
    code.append((0x06,0,0,0x7fff0000))
    ins=(Ins*len(code))(*(Ins(*i) for i in code));prog=Prog(len(code),ins)
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(38,1,0,0,0)!=0 or libc.prctl(22,2,ctypes.byref(prog),0,0)!=0:
        raise RuntimeError('seccomp_unavailable')
class Output:
    def __init__(self):self.parts=[];self.size=0
    def write(self,value):
        value=str(value);self.size+=len(value.encode('utf-8'))
        if self.size>65536:raise RuntimeError('output_limit')
        self.parts.append(value);return len(value)
    def flush(self):pass
def request(method,path_query,headers=None,body=''):
    emit(dict(kind='request',method=method,path_query=path_query,headers=headers or {},body=body))
    line=wire_in.readline(4*1024*1024+1)
    if not line or len(line)>4*1024*1024:raise RuntimeError('bridge_closed')
    result=json.loads(line)
    if 'error' in result:raise RuntimeError(result['error'])
    result['body']=base64.b64decode(result.pop('body_b64'),validate=True)
    result['text']=result['body'].decode('utf-8',errors='replace')
    return result
try:
    line=wire_in.readline(4*1024*1024+1)
    if not line.endswith('\n') or len(line)>4*1024*1024:raise ValueError('frame_invalid')
    value=json.loads(line)
    if set(value)!={'script'} or not isinstance(value['script'],str) or len(value['script'].encode('utf-8'))>65536:
        raise ValueError('script_invalid')
    isolate()
except BaseException:
    emit(dict(kind='unavailable',code='d22_experiment_isolation_unavailable'));raise SystemExit(2)
emit(dict(kind='ready',pid=os.getpid()))
output=Output();sys.stdout=output;sys.stderr=output
try:
    exec(compile(value['script'],'<http-experiment>','exec'),{'request':request,'__name__':'__experiment__'})
except BaseException as error:
    line=error.lineno if isinstance(error,SyntaxError) else None
    trace=error.__traceback__
    while trace:
        if trace.tb_frame.f_code.co_filename=='<http-experiment>':line=trace.tb_lineno
        trace=trace.tb_next
    emit(dict(kind='finished',status='failed',code='d22_experiment_script_error',error_type=type(error).__name__,error_line=line,output=''.join(output.parts)))
else:emit(dict(kind='finished',status='completed',code=None,output=''.join(output.parts)))
'''


class HttpExperimentProcess:
    """No execution lock is held while a script waits for its trusted HTTP RPC.

    The application remains the only tool dispatcher. Network and process-create
    syscalls are denied in the script; independent trusted HTTP execs retain the
    existing TaskRun admission lock and target channel.
    """
    def __init__(self, sandbox, task_id, sandbox_id, script, timeout):
        if not isinstance(script, str) or not script.strip() or '\x00' in script or len(script.encode('utf-8')) > MAX_SCRIPT_BYTES:
            raise ValueError('d22_experiment_script_invalid')
        self.sandbox, self.task_id, self.sandbox_id = sandbox, task_id, sandbox_id
        self.api = sandbox._runtime.client.api
        self.binding = sandbox._binding(sandbox_id)
        self.pid = None
        self.buffer = bytearray()
        self.expires = time.monotonic() + timeout
        sandbox._verify_active(task_id, sandbox_id)
        with sandbox._admit(self.binding):
            result = self.api.exec_create(container=sandbox_id,
                cmd=['timeout', '--signal=TERM', '--kill-after=2s', f'{max(1, math.ceil(timeout))}s',
                     'python3', '-I', '-c', WORKER], stdin=True, stdout=True, stderr=True,
                tty=False, privileged=False, user='10001:10001', workdir='/workspace/work',
                environment={'PATH':'/usr/bin:/bin'})
            self.exec_id = result['Id']
            try:
                self.socket = self.api.exec_start(self.exec_id, socket=True, tty=False)
            except Exception as error:
                raise TaskRunSandboxError('experiment_start','outcome_unknown',task_id,(sandbox_id,),original_error=error) from error
        self.raw = getattr(self.socket, '_sock', self.socket)
        if hasattr(self.raw, 'settimeout'):
            self.raw.settimeout(timeout)
        try:
            self.send({'script':script})
        except Exception as error:
            self.socket.close()
            raise TaskRunSandboxError('experiment_start','outcome_unknown',task_id,(sandbox_id,),original_error=error) from error

    def send(self, value):
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False).encode('ascii') + b'\n'
        if len(raw) > MAX_FRAME_BYTES:
            raise ValueError('d22_experiment_frame_too_large')
        self.raw.sendall(raw)

    def receive(self):
        from docker.utils.socket import next_frame_header, read_exactly
        while b'\n' not in self.buffer:
            if time.monotonic() >= self.expires:
                raise TimeoutError('d22_experiment_timeout')
            stream, length = next_frame_header(self.raw)
            if stream != 1 or length < 0 or len(self.buffer) + length > MAX_FRAME_BYTES:
                raise RuntimeError('d22_experiment_bridge_invalid')
            self.buffer.extend(read_exactly(self.raw, length))
        line, _, remaining = self.buffer.partition(b'\n')
        self.buffer = bytearray(remaining)
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError('d22_experiment_bridge_invalid')
        if value.get('kind') == 'ready':
            if type(value.get('pid')) is not int or value['pid'] <= 1 or self.pid is not None:
                raise RuntimeError('d22_experiment_bridge_invalid')
            self.pid = value['pid']
        return value

    def close(self):
        self.stop_diagnostic = dict(phase='inspect', process_stopped=False)
        try:
            status = self.api.exec_inspect(self.exec_id)
            if status.get('Running') is True and self.pid is not None:
                # Use the existing interpreter: external kill is not an image contract.
                # Only this session's confirmed PID, never a shell/global kill.
                self.stop_diagnostic['phase'] = 'signal'
                self.sandbox._verify_active(self.task_id, self.sandbox_id)
                with self.sandbox._admit(self.binding):
                    kill = self.api.exec_create(container=self.sandbox_id,
                        cmd=['python3','-I','-c',
                             'import os,signal,sys\ntry: os.kill(int(sys.argv[1]),signal.SIGKILL)\nexcept ProcessLookupError: pass',
                             str(self.pid)], stdout=False, stderr=False,
                        tty=False, privileged=False, user='10001:10001')
                    self.api.exec_start(kill['Id'])
                signal_status = self.api.exec_inspect(kill['Id'])
                if type(signal_status.get('ExitCode')) is int:
                    self.stop_diagnostic['signal_exit_code'] = signal_status['ExitCode']
                status = self.api.exec_inspect(self.exec_id)
            self.stop_diagnostic['phase'] = 'wait_exit'
            until = time.monotonic() + 3
            while status.get('Running') is True and time.monotonic() < until:
                time.sleep(0.025)
                status = self.api.exec_inspect(self.exec_id)
            if status.get('Running') is not False:
                raise TaskRunSandboxError('experiment_stop','outcome_unknown',self.task_id,(self.sandbox_id,))
            self.stop_diagnostic['process_stopped'] = True
            if type(status.get('ExitCode')) is int:
                self.stop_diagnostic['process_exit_code'] = status['ExitCode']
        except TaskRunSandboxError:
            raise
        except Exception as error:
            raise TaskRunSandboxError('experiment_stop','outcome_unknown',self.task_id,
                (self.sandbox_id,),original_error=error) from error
        finally:
            # A stream-close error cannot undo a confirmed process exit or mask
            # the primary failure. The caller retains this bounded receipt.
            try:
                self.socket.close()
                self.stop_diagnostic['stream_closed'] = True
            except Exception:
                self.stop_diagnostic['stream_closed'] = False
