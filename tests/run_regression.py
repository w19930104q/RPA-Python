#!/usr/bin/env python3
"""Regression suite for the enhanced tagui.run().

Run from anywhere:

    python3 -I run_regression.py

Covers the behaviors required for the nightly RPA conversion job:
  1. argv call against a script whose path contains spaces
  2. space-bearing arguments without shell quoting
  3. legacy string call still returns a merged output string
  4. non-zero exit code is reported, not raised
  5. stdout / stderr captured separately
  6. timeout kills the whole process tree (no orphan grandchildren)
  7. timeout returns a stable timed_out status with signal info
  8. very large output is capped and marked truncated
  9. repeated consecutive calls remain isolated and correct
 10. cwd and env parameters take effect
 11. invalid cwd / timeout fail predictably instead of raising
 12. timeout termination never harms unrelated host processes
 13. RunResult dict view / truth value / repr stay stable
 14. registry of run children is empty after every call (close() safety)

The suite only depends on the Python standard library and a POSIX shell.
Windows-only branches (creationflags / taskkill) are guarded and reported
as skipped on non-Windows hosts.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import tagui  # noqa: E402

PASS = 'PASS'
FAIL = 'FAIL'
SKIP = 'SKIP'
IS_WINDOWS = sys.platform.startswith('win')

results = []


def check(name, condition, detail=''):
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    print('[%s] %s%s' % (status, name, (' - ' + detail) if detail else ''))


def section(title):
    print('\n=== %s ===' % title)


def main():
    workdir = tempfile.mkdtemp(prefix='rpa run regression ')
    try:
        _run_suite(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print('\n----------------------------------------')
    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    skipped = sum(1 for s, _, _ in results if s == SKIP)
    print('total=%d pass=%d fail=%d skip=%d'
          % (len(results), passed, failed, skipped))
    print('work dir cleaned up: %s' % workdir)
    return 1 if failed else 0


def _run_suite(workdir):
    # ---- fixture: helper script installed under a path with spaces -------
    fixture_src = os.path.join(REPO_ROOT, 'tests', 'run fixtures',
                               'convert_helper.sh')
    space_dir = os.path.join(workdir, 'nightly jobs', 'conversion step')
    os.makedirs(space_dir)
    helper = os.path.join(space_dir, 'convert helper.sh')
    shutil.copyfile(fixture_src, helper)
    os.chmod(helper, 0o755)

    input_file = os.path.join(space_dir, 'input data.txt')
    with open(input_file, 'w') as handle:
        handle.write('row one\nrow two\n')
    output_file = os.path.join(space_dir, 'output data.txt')

    section('1-2. argv with space-bearing path and arguments')
    r = tagui.run([helper, input_file, output_file])
    check('argv run succeeds', r.success, 'returncode=%r' % r.returncode)
    check('space path output file written', os.path.isfile(output_file))
    if os.path.isfile(output_file):
        with open(output_file) as handle:
            body = handle.read()
        check('output content converted',
              body == 'converted: row one\nconverted: row two\n',
              repr(body))
    check('stdout and stderr captured separately',
          'helper starting' in r.stdout and 'audit:' in r.stderr,
          'stdout=%r stderr=%r' % (r.stdout[:40], r.stderr[:40]))
    check('no truncation on normal output',
          not r.stdout_truncated and not r.stderr_truncated)
    check('registry empty after call', tagui._run_processes == set())

    r_arg = tagui.run(['printf', '%s', 'argument with spaces'])
    check('space-bearing argv element preserved',
          r_arg.stdout == 'argument with spaces', repr(r_arg.stdout))

    section('3. legacy string call contract')
    legacy = tagui.run('echo legacy output')
    check('legacy returns plain str',
          isinstance(legacy, str) and legacy == 'legacy output\n',
          repr(legacy))
    legacy_fail = tagui.run('echo to stdout; echo to stderr 1>&2; exit 3')
    check('legacy never raises on non-zero (exit 0 appended)',
          isinstance(legacy_fail, str) and 'to stdout' in legacy_fail
          and 'to stderr' in legacy_fail, repr(legacy_fail))
    missing = tagui.run('')
    check('empty command returns empty string', missing == '')

    section('4-5. non-zero exit and separate streams')
    r_fail = tagui.run([helper, input_file,
                        os.path.join(space_dir, 'out2.txt'), 'fail'])
    check('non-zero exit reported',
          (not r_fail.success) and r_fail.returncode == 7,
          'returncode=%r' % r_fail.returncode)
    check('signal is None for normal exit', r_fail.signal is None)
    check('stderr holds failure message',
          'helper failing on demand' in r_fail.stderr, repr(r_fail.stderr))
    check('timed_out False on plain failure', r_fail.timed_out is False)

    section('6-7. timeout terminates the whole process tree')
    # spawn a shell that forks a grandchild sleep; both must be reaped
    spawner = (
        'sleep 30 & child=$!; '
        'echo "grandchild=$child"; '
        'wait $child'
    )
    start = time.time()
    r_to = tagui.run(['sh', '-c', spawner], timeout=1.5)
    elapsed = time.time() - start
    check('timeout returns within budget', elapsed < 8.0,
          'elapsed=%.2fs' % elapsed)
    check('timed_out flag set', r_to.timed_out is True)
    check('success False after timeout', r_to.success is False)
    check('killed by signal (SIGTERM/SIGKILL)',
          r_to.signal in (signal.SIGTERM, signal.SIGKILL),
          'signal=%r returncode=%r' % (r_to.signal, r_to.returncode))

    # parse the grandchild pid printed before the wait and confirm it is gone
    grandchild_pid = None
    for token in r_to.stdout.split():
        if token.startswith('grandchild='):
            try:
                grandchild_pid = int(token.split('=', 1)[1])
            except ValueError:
                pass
    check('grandchild pid observed', grandchild_pid is not None,
          repr(r_to.stdout))
    if grandchild_pid is not None:
        orphan = _pid_running(grandchild_pid)
        check('no orphan grandchild left behind', orphan is False,
              'pid=%d still running' % grandchild_pid if orphan
              else 'pid=%d reaped' % grandchild_pid)
    check('registry empty after timeout', tagui._run_processes == set())

    # harder variant: grandchild ignores SIGTERM, proving the SIGKILL group
    # escalation reaches descendants that survive the first signal
    stubborn = (
        'trap "" TERM; '
        'sleep 30 & child=$!; echo "stubborn=$child"; wait $child'
    )
    r_kill = tagui.run(['sh', '-c', stubborn], timeout=1.0)
    check('stubborn tree timed out', r_kill.timed_out is True)
    stubborn_pid = None
    for token in r_kill.stdout.split():
        if token.startswith('stubborn='):
            try:
                stubborn_pid = int(token.split('=', 1)[1])
            except ValueError:
                pass
    if stubborn_pid is not None:
        # small grace for the kernel to finish reaping after SIGKILL
        deadline = time.time() + 3.0
        while time.time() < deadline and _pid_running(stubborn_pid):
            time.sleep(0.1)
        still_running = _pid_running(stubborn_pid)
        check('SIGKILL escalates to whole group (TERM-ignoring child)',
              still_running is False,
              'pid=%d still running' % stubborn_pid
              if still_running else 'pid=%d reaped via SIGKILL' % stubborn_pid)

    section('8. large output is capped and flagged truncated')
    big = tagui.run(
        ['sh', '-c', 'i=0; while [ $i -lt 200000 ]; do echo line$i; i=$((i+1)); done'],
        output_limit=4096)
    check('stdout capped at limit', len(big.stdout.encode('utf-8')) <= 4096,
          'len=%d' % len(big.stdout))
    check('stdout truncation flagged', big.stdout_truncated is True)
    big_err = tagui.run(
        ['sh', '-c', 'echo out; i=0; while [ $i -lt 100000 ]; do echo err$i 1>&2; i=$((i+1)); done'],
        output_limit=2048)
    check('stderr capped independently', len(big_err.stderr) <= 2048)
    check('stderr truncation flagged', big_err.stderr_truncated is True)
    check('small stdout not flagged', big_err.stdout_truncated is False)

    section('9. consecutive calls stay isolated')
    for index in range(5):
        rr = tagui.run(['printf', 'seq-%d' % index])
        if not (rr.success and rr.stdout == 'seq-%d' % index):
            check('consecutive call %d' % index, False, repr(rr.stdout))
            break
    else:
        check('5 consecutive argv calls return correct output', True)
    for index in range(3):
        ls = tagui.run('echo legacy-%d' % index)
        if ls != 'legacy-%d\n' % index:
            check('consecutive legacy call %d' % index, False, repr(ls))
            break
    else:
        check('3 consecutive legacy calls return correct output', True)

    section('10. cwd and env')
    cwd_dir = os.path.join(workdir, 'cwd target')
    os.makedirs(cwd_dir)
    r_cwd = tagui.run(['pwd'], cwd=cwd_dir)
    check('cwd applied', os.path.realpath(r_cwd.stdout.strip())
          == os.path.realpath(cwd_dir), repr(r_cwd.stdout))
    custom_env = dict(os.environ)
    custom_env['RPA_RUN_TEST_VAR'] = 'nightly value'
    r_env = tagui.run(['sh', '-c', 'echo "$RPA_RUN_TEST_VAR"'], env=custom_env)
    check('custom env applied', r_env.stdout == 'nightly value\n',
          repr(r_env.stdout))
    r_missing_cwd = tagui.run(['pwd'],
                              cwd=os.path.join(workdir, 'does not exist'))
    check('invalid cwd handled without raise',
          r_missing_cwd.returncode is None and r_missing_cwd.cwd is not None)
    r_bad_timeout = tagui.run(['echo', 'x'], timeout='soon')
    check('invalid timeout handled without raise',
          r_bad_timeout.returncode is None)

    section('11. details=True gives structured result for string calls')
    r_details = tagui.run('echo detailed; echo err-line 1>&2', details=True)
    check('details returns RunResult', hasattr(r_details, 'as_dict'))
    check('details captures both streams',
          'detailed' in r_details.stdout and 'err-line' in r_details.stderr)

    section('12. timeout does not harm unrelated processes')
    sentinel = subprocess.Popen(['sleep', '20'])
    try:
        tagui.run(['sh', '-c', 'sleep 30'], timeout=1.0)
        check('unrelated sibling process survives',
              _pid_running(sentinel.pid), 'sentinel pid=%d' % sentinel.pid)
    finally:
        sentinel.terminate()
        sentinel.wait()

    section('12b. close() teardown reaps only run() children')
    # close() delegates teardown to _terminate_remaining_run_processes();
    # exercise that exact helper with an in-flight run child plus an
    # unrelated sentinel that must survive
    teardown_sentinel = subprocess.Popen(['sleep', '20'])
    in_flight = subprocess.Popen(
        ['sh', '-c', 'sleep 30 & wait'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)
    tagui._run_processes.add(in_flight)
    try:
        tagui._terminate_remaining_run_processes()
        check('in-flight run child reaped on teardown',
              not _pid_running(in_flight.pid),
              'pid=%d' % in_flight.pid)
        check('unrelated process survives teardown',
              _pid_running(teardown_sentinel.pid),
              'sentinel pid=%d' % teardown_sentinel.pid)
        check('registry cleared on teardown', tagui._run_processes == set())
    finally:
        if in_flight.poll() is None:
            in_flight.kill()
        teardown_sentinel.terminate()
        teardown_sentinel.wait()
        tagui._run_processes.discard(in_flight)

    section('13. stable RunResult surface')
    r_surface = tagui.run(['sh', '-c', 'echo ok; exit 0'])
    view = r_surface.as_dict()
    expected_keys = {'command', 'returncode', 'signal', 'timed_out',
                     'success', 'stdout', 'stderr', 'stdout_truncated',
                     'stderr_truncated', 'cwd'}
    check('as_dict key set stable', set(view.keys()) == expected_keys,
          str(sorted(view.keys())))
    check('dict(result) works', dict(r_surface)['returncode'] == 0)
    check('bool True on success', bool(r_surface) is True)
    check('bool False on failure', bool(tagui.run(['sh', '-c', 'exit 1'])) is False)
    check('repr is informative', 'RunResult(' in repr(r_surface), repr(r_surface))

    section('14. Windows-only branch availability')
    if IS_WINDOWS:
        check('creationflags constant present',
              hasattr(subprocess, 'CREATE_NEW_PROCESS_GROUP'))
    else:
        results.append((SKIP, 'Windows creationflags/taskkill branch',
                        'not a Windows host; code path guarded by platform check'))


def _pid_running(pid):
    """True only if pid exists as a non-zombie process (POSIX).

    A zombie (state Z) is a process that has already exited and is waiting
    for its parent to reap it, not a live orphan; when the parent is gone
    the kernel re-anchors it to init and reaps it promptly. Windows hosts
    use the guarded tasklist branch instead."""
    try:
        os.kill(pid, 0)
    except OSError as exc:
        import errno
        # EPERM means the process exists but belongs to another user: alive
        return exc.errno == errno.EPERM
    proc_stat = '/proc/%d/stat' % pid
    if os.path.isfile(proc_stat):
        try:
            with open(proc_stat) as handle:
                fields = handle.read().split()
            return fields[2] != 'Z'
        except (IOError, IndexError):
            return True
    # signal 0 succeeded but /proc is unavailable: treat as running
    return True


if __name__ == '__main__':
    sys.exit(main())
