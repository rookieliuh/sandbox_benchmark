"""Opt-in real E2B smoke test through native bRPC; touches only its newly created sandbox."""
import argparse
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import time
import uuid


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', type=Path, default=Path('build/agent-proxy'))
    parser.add_argument('--client', type=Path, default=Path('build/agent-proxy-client'))
    parser.add_argument('--backends', type=Path, required=True)
    parser.add_argument('--backend', required=True)
    parser.add_argument('--template', required=True)
    parser.add_argument('--cpu', type=int, required=True)
    parser.add_argument('--memory-mib', type=int, required=True)
    parser.add_argument('--ttl', type=int, default=300)
    parser.add_argument('--basic-lifecycle', action='store_true',
                        help='Six normal RPCs only: bash printf, no file-tool tests or injected delays')
    parser.add_argument('--spdiag-shm-name', default='', help='Existing private /agent_proxy_* SHM')
    parser.add_argument('--report', type=Path, help='Write sanitized RPC timing and cleanup results')
    parser.add_argument('--allow-create', action='store_true', help='Authorize one test sandbox and lifecycle operations')
    args = parser.parse_args()
    if not args.allow_create:
        parser.error('--allow-create is required: this test creates, pauses, resumes and deletes one sandbox')
    if min(args.cpu, args.memory_mib, args.ttl) <= 0:
        parser.error('CPU, memory and TTL must be positive')
    if args.spdiag_shm_name and (not args.spdiag_shm_name.startswith('/agent_proxy_') or
                               '/' in args.spdiag_shm_name[1:]):
        parser.error('SpDiag requires an existing private /agent_proxy_* name')
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
    # Validate paths before starting a service. Do not change any existing deployment.
    for path in (args.proxy, args.client, args.backends):
        if not path.is_file():
            parser.error('proxy/client/config file missing')
    run = 'smoke-' + uuid.uuid4().hex
    create_id = run + '-create'
    port = free_port()
    output = Path('validation-logs')
    output.mkdir(exist_ok=True)
    log_path = output / (run + '-proxy.log')
    process = None
    sequence = 0
    attempted = False
    success = False
    cleaned = False
    sid = ''
    records = []
    file_checks = {} if args.basic_lifecycle else {
        'write_read': False, 'edit_read': False, 'after_resume': False}
    proxy_exit = {'returncode': None, 'forced_kill': False, 'normal_exit': False}
    env = os.environ.copy()
    for name in ('NO_PROXY', 'no_proxy'):
        env[name] = '127.0.0.1,localhost' + (',' + env[name] if env.get(name) else '')

    def rpc(method, body, request_id=None, require_success=True):
        nonlocal sequence
        sequence += 1
        body = dict(body)
        phases = {'CreateSandbox': 'PHASE_CREATE', 'DeleteSandbox': 'PHASE_CLEANUP',
                  'GetStatus': 'PHASE_RECONCILE'}
        body['meta'] = {'request_id': request_id or f'{run}-{sequence}', 'run_id': run,
                        'task_instance_id': run, 'phase': phases.get(method, 'PHASE_VALIDATE')}
        if method != 'GetStatus':
            body.setdefault('timeout_ms', 60000)
        if method == 'RunStep':
            body.setdefault('step_id', sequence)
        started = time.monotonic()
        result = subprocess.run([str(args.client.resolve()), f'--server=127.0.0.1:{port}',
                                 f'--method={method}', '--request=-'], input=json.dumps(body),
                                text=True, encoding='utf-8', capture_output=True, timeout=75, env=env)
        if result.returncode not in (0, 2):
            raise RuntimeError(f'{method}: client transport/local failure, exit={result.returncode}')
        response = json.loads(result.stdout)
        code = response.get('error', {}).get('code')
        elapsed = int(response['meta']['proxy_elapsed']['value_ms'])
        records.append({'method': method, 'status': code, 'proxy_elapsed_ms': elapsed,
                        'rpc_wall_ms': (time.monotonic() - started) * 1000,
                        'request_id': body['meta']['request_id']})
        if method == 'RunStep':
            records[-1]['tool'] = body['tool']
        label = f'RunStep[{body["tool"]}]' if method == 'RunStep' else method
        print(f'{label}: {code}; proxy_elapsed_ms={elapsed}', flush=True)
        if code != 'ERROR_OK':
            detail = response.get('error', {})
            # These are sanitized Proxy errors, never the raw backend response or config.
            print('Proxy error:', json.dumps({k: detail.get(k, '') for k in
                                             ('message', 'backend_code')}, ensure_ascii=False), flush=True)
        if require_success and (not response.get('success') or code != 'ERROR_OK'):
            raise RuntimeError(f'{label} failed: {code}')
        return response

    def step(tool, arguments):
        result = rpc('RunStep', {'sandbox_id': sid, 'tool': tool,
                                'arguments_json': json.dumps(arguments), 'timeout_ms': 10000})
        return json.loads(result['result_json'])

    try:
        with log_path.open('w', encoding='utf-8') as log:
            process = subprocess.Popen([str(args.proxy.resolve()), f'--listen=127.0.0.1:{port}',
                                        f'--backends={args.backends.resolve()}',
                                        '--control_threads=2', '--step_threads=2',
                                        '--graceful_quit_on_sigterm=true',
                                        f'--spdiag_shm_name={args.spdiag_shm_name}'],
                                       stdout=log, stderr=subprocess.STDOUT, env=env)
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise RuntimeError('Proxy failed to start; inspect local proxy log')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Proxy startup timeout')
                    time.sleep(0.05)
            attempted = True
            created = rpc('CreateSandbox', {'backend': args.backend, 'template': args.template,
                          'spec': {'cpu_count': args.cpu, 'memory_mib': args.memory_mib},
                          'ttl_seconds': args.ttl}, request_id=create_id)
            sid = created['sandbox']['sandbox_id']
            print(f'Own test sandbox: {sid}', flush=True)
            observed = rpc('GetStatus', {'sandbox_id': sid})
            if observed['sandbox']['state'] != 'SANDBOX_READY':
                raise RuntimeError('Sandbox is not READY')
            result = step('bash', {'command': 'printf hello'})
            if result['stdout'] != 'hello' or result['exit_code'] != 0:
                raise RuntimeError('Command output mismatch')
            if not args.basic_lifecycle:
                path = '/tmp/' + run + '.txt'
                print('File check [write-read]: starting', flush=True)
                step('write', {'path': path, 'content': 'agent proxy smoke'})
                if step('read', {'path': path})['content'] != 'agent proxy smoke':
                    raise RuntimeError('Initial file write/read mismatch')
                file_checks['write_read'] = True
                print('File check [write-read]: PASS', flush=True)
                print('File check [edit-read]: starting', flush=True)
                step('edit', {'path': path, 'oldString': 'proxy', 'newString': 'benchmark'})
                if step('read', {'path': path})['content'] != 'agent benchmark smoke':
                    raise RuntimeError('File roundtrip mismatch')
                file_checks['edit_read'] = True
                print('File check [edit-read]: PASS', flush=True)
            paused = rpc('PauseSandbox', {'sandbox_id': sid})
            if paused['sandbox']['state'] != 'SANDBOX_PAUSED':
                raise RuntimeError('Pause was not verified')
            resumed = rpc('ResumeSandbox', {'sandbox_id': sid})
            if resumed['sandbox']['state'] != 'SANDBOX_READY' or resumed['sandbox']['sandbox_id'] != sid:
                raise RuntimeError('Resume or identity mismatch')
            if not args.basic_lifecycle:
                print('File check [after-resume]: starting', flush=True)
                if step('read', {'path': path})['content'] != 'agent benchmark smoke':
                    raise RuntimeError('File was not preserved across pause/resume')
                file_checks['after_resume'] = True
                print('File check [after-resume]: PASS', flush=True)
                record = rpc('GetStatus', {'operation_request_id': create_id})
                if record['operation']['info']['state'] != 'OPERATION_SUCCEEDED':
                    raise RuntimeError('Create operation record missing')
            success = True
    except (Exception, KeyboardInterrupt) as error:
        print(f'FAIL: {error}', file=sys.stderr, flush=True)
    finally:
        if attempted and process is not None and process.poll() is None:
            try:
                # A timed-out RPC may still be finishing. Never submit a second create.
                if not (args.basic_lifecycle and success):
                    deadline = time.monotonic() + 75
                    while True:
                        status = rpc('GetStatus', {'operation_request_id': create_id}, require_success=False)
                        state = status.get('operation', {}).get('info', {}).get('state')
                        if state not in ('OPERATION_ACCEPTED', 'OPERATION_RUNNING') or time.monotonic() >= deadline:
                            break
                        time.sleep(0.5)
                result = rpc('DeleteSandbox', {'create_request_id': create_id}, require_success=False)
                cleaned = bool(result.get('success') and result.get('cleanup_confirmed'))
                print(f'Cleanup confirmed: {str(cleaned).lower()}', flush=True)
            except (Exception, KeyboardInterrupt):
                cleaned = False
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proxy_exit['forced_kill'] = True
                process.kill()
                process.wait()
            proxy_exit['returncode'] = process.returncode
            proxy_exit['normal_exit'] = process.returncode == 0 and not proxy_exit['forced_kill']
            print(f'Proxy shutdown: {proxy_exit}', flush=True)
        print(f'Run ID: {run}\nCreate request ID: {create_id}\nProxy log: {log_path}', flush=True)
        if attempted and not cleaned:
            print('CLEANUP UNCONFIRMED: retain this create request ID; ask deployment owner to locate '
                  'the sandbox by metadata.agent_proxy_request. Do not delete unrelated sandboxes.', flush=True)
    passed = success and cleaned and proxy_exit['normal_exit']
    if args.report:
        args.report.write_text(json.dumps({'passed': passed, 'run_id': run, 'create_request_id': create_id,
                               'basic_lifecycle': args.basic_lifecycle, 'perf_cases': records,
                               'file_checks': file_checks,
                               'sandbox_cleanup_confirmed': cleaned, 'proxy_log': str(log_path),
                               'process_exits': {'proxy': proxy_exit}}, indent=2), encoding='utf-8')
    if passed:
        scope = 'basic lifecycle and bash' if args.basic_lifecycle else 'lifecycle and tools'
        print(f'PASS: real E2B {scope} verified; cleanup confirmed by control API.')
    elif cleaned:
        print('FAIL: smoke test failed; cleanup confirmed by control API.')
    else:
        print('FAIL: smoke test or cleanup incomplete.')
    return 0 if passed else 1


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
