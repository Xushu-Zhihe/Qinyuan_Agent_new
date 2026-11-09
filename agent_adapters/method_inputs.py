"""On-demand input capture inside the original admitted tool operation."""
import asyncio
import base64
import hashlib

from agent_core.turn_modeling import digest
from Sandbox.image_readback import validate_image_path


async def capture_inputs(service, evidence, identity, claim, call):
    from agent_adapters.mcp_plan_executor import ToolArgumentError
    from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
    if service is None or not hasattr(service, 'read_image'):
        raise ToolArgumentError('method.capture_unavailable: current sandbox has no bounded file reader')
    files, size = [], 0
    expected = dict(call.expected_files)
    for path in call.pin_files:
        try:
            validate_image_path(path)
        except ValueError:
            raise ToolArgumentError('method.path_rejected: use an absolute sandbox input or work file') from None
        # The fixed worker reads bytes through a separate bounded channel, never tool stdout.
        data = await asyncio.to_thread(service.read_image, claim['task_run_id'], path)
        size += len(data)
        if size > 256 * 1024:
            raise ToolArgumentError('method.capture_capacity: declared inputs exceed 256 KiB')
        sha = hashlib.sha256(data).hexdigest()
        if path in expected and sha != expected[path]:
            raise ToolArgumentError('method.input_version_changed: a declared input no longer matches the method')
        files.append({'path': path, 'sha256': sha, 'base64': base64.b64encode(data).decode('ascii')})
    arguments = dict(call.arguments)
    script_path = arguments.get('script_path')
    if script_path:
        source = next((f for f in files if f['path'] == script_path), None)
        if source is None:
            raise ToolArgumentError('method.script_not_pinned: include script_path in pin_files')
        python_tools = {'pwntools.run_pwntools', 'pycryptodome.run_crypto', 'z3.solve_constraints'}
        if call.tool_name not in python_tools:
            raise ToolArgumentError('method.file_runner_unsupported: this runner cannot execute a pinned script')
        import zlib
        body = base64.b64decode(source['base64'], validate=True)
        if not body.strip() or b'\x00' in body or len(body) > 65536:
            raise ToolArgumentError('method.script_invalid: script must be nonempty, NUL-free and at most 64 KiB')
        encoded = base64.b64encode(zlib.compress(body)).decode('ascii')
        arguments.pop('script_path')
        arguments['script'] = ("import sys,base64,zlib\n__file__=" + repr(script_path)
            + "\nsys.argv=[__file__]\nsys.path.insert(0," + repr(script_path.rsplit('/', 1)[0])
            + ")\n_qy_source=zlib.decompress(base64.b64decode(" + repr(encoded)
            + "))\nexec(compile(_qy_source,__file__,'exec'),globals())\n")
    # Input files remain version prerequisites; script bytes themselves are embedded in the entry.
    inputs = [f for f in files if f['path'] != script_path]
    resolved = {'tool_name': call.tool_name, 'arguments': arguments}
    if inputs:
        resolved.update(pin_files=[f['path'] for f in inputs],
                        expected_files=[[f['path'], f['sha256']] for f in inputs])
    record = {'request_sha256': digest(claim), 'files': files, 'resolved_call': resolved}
    previous = evidence.read('mcp-method-inputs', identity)
    if previous is not None and previous != record:
        raise ExecutionRecoveryRequired('method.capture_binding_changed')
    evidence.save('mcp-method-inputs', identity, record)
    return arguments
