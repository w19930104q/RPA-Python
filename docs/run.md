# `run()` — operating-system command execution

`run()` executes an OS command from Python and captures its output. It has
two calling styles:

- **string (legacy, shell)** — `run('echo hello')` returns a single merged
  `stdout`+`stderr` string, exactly as in earlier releases.
- **argv list (new, no shell)** — `run(['/path with space/convert.sh', arg])`
  returns a structured `RunResult`. No shell is involved, so paths and
  arguments containing spaces never need quoting and cannot be interpreted
  as shell metacharacters.

## Why the enhancement

The original implementation used `subprocess.check_output(..., shell=True,
stderr=STDOUT)` with no working directory, environment or timeout control:

- paths containing spaces required fragile shell quoting;
- `stdout` and `stderr` were merged, making failures hard to audit;
- a command that hung blocked the nightly job forever;
- killing a shell on timeout could leave orphaned grandchild processes;
- non-zero exits raised rather than returning a stable status.

The enhanced version keeps the old string-call contract while adding
argv execution, separate stream capture, `cwd`/`env`, a hard timeout that
terminates the **whole process tree**, bounded output with truncation
flags, and a stable status object for both success and failure.

## Signature

```python
run(command_to_run=None, cwd=None, env=None, timeout=None,
    output_limit=RUN_OUTPUT_LIMIT, details=False, shell=None)
```

| parameter | meaning |
|-----------|---------|
| `command_to_run` | shell string (legacy) or argv `list`/`tuple` (new) |
| `cwd` | working directory the command starts in |
| `env` | full environment mapping for the child; `None` inherits the current environment |
| `timeout` | seconds (`int`/`float`); on expiry the entire process tree is terminated |
| `output_limit` | maximum bytes retained **per stream** (default 1 MiB, `RUN_OUTPUT_LIMIT`) |
| `details` | when `True`, even a string call returns a `RunResult` instead of a merged string |
| `shell` | force shell mode; defaults to `True` for strings, `False` for argv |

Invalid arguments (missing command, non-existent `cwd`, non-positive or
non-numeric `timeout`, bad `env`) are reported through `show_error()` and
return an empty string (legacy mode) or a `RunResult` with `returncode=None`
(structured mode) instead of raising.

## Return value

### String call (default)

```python
out = run('echo hello')        # 'hello\n', a plain str
```

`stdout` and `stderr` are merged, preserving the historical
`stderr=subprocess.STDOUT` behavior. A trailing `; exit 0` (POSIX) or
`& exit 0` (Windows) is appended so a non-zero command never raises — the
legacy contract.

### argv call, or any call with `details=True`

A `RunResult` object is returned:

| attribute | meaning |
|-----------|---------|
| `command` | the argv list or shell string that was executed |
| `returncode` | exit code; a negative value means death by signal (`-SIGNAL`, the `subprocess` convention) |
| `signal` | signal number when terminated by a signal, else `None` |
| `timed_out` | `True` if the timeout fired and the tree was killed |
| `stdout` / `stderr` | separately captured text, each capped at `output_limit` |
| `stdout_truncated` / `stderr_truncated` | `True` if that stream exceeded the cap |
| `success` | `True` when not timed out and `returncode == 0` |
| `cwd` | working directory used |

`RunResult` also supports:

- `result.as_dict()` / `dict(result)` — a stable, JSON-serializable view for
  audit logs;
- `bool(result)` — `True` on success;
- `repr(result)` — a compact one-line summary.

## Examples

### Space-bearing path (the nightly conversion job)

```python
result = run([
    '/opt/nightly jobs/convert.sh',
    '/data/in/input file.csv',
    '/data/out/output file.csv',
])
if not result.success:
    log.error('conversion failed rc=%s stderr=%s',
              result.returncode, result.stderr)
```

### Non-zero exit without an exception

```python
r = run(['sh', '-c', 'exit 7'])
r.returncode      # 7
r.signal          # None
r.success         # False
r.timed_out       # False
```

### Timeout that kills the entire tree (no orphans)

```python
r = run(['sh', '-c', 'sleep 30 & wait'], timeout=2)
r.timed_out       # True
r.signal          # 15 (SIGTERM), or 9 (SIGKILL) if the tree ignored TERM
```

The child starts in its own process group (POSIX `start_new_session=True`)
or a new Windows process group / job (`CREATE_NEW_PROCESS_GROUP` +
`CREATE_BREAKAWAY_FROM_JOB`). On timeout the group is signaled with
`SIGTERM`, then escalated to `SIGKILL` after `RUN_KILL_GRACE` seconds; on
Windows `taskkill /T /F` walks the tree. Grandchildren are therefore
reaped and no unrelated process is touched.

### Working directory and environment

```python
run(['pwd'], cwd='/tmp/work area')
run(['sh', '-c', 'echo "$JOB_ID"'], env={**os.environ, 'JOB_ID': '42'})
```

### Large output is bounded

```python
r = run(['sh', '-c', 'yes data'], timeout=3, output_limit=4096)
len(r.stdout) <= 4096      # True
r.stdout_truncated         # True
```

Each stream is drained in a background thread and capped while reading, so
memory stays bounded even when a command emits unbounded output.

## Cross-platform and cancellation behavior

- **POSIX (Linux/macOS):** new session → new process group; `os.killpg()`
  reaches every descendant; `SIGTERM` then `SIGKILL` escalation.
- **Windows:** isolated process group via creation flags; `taskkill /PID
  <pid> /T /F` terminates the whole tree, with `process.kill()` as a
  fallback.
- **`close()` safety:** every `run()` child is registered in
  `_run_processes` and removed on completion. `close()` calls
  `_terminate_remaining_run_processes()`, which signals only those tracked
  children through their own groups — it never broad-kills host processes
  or the caller's unrelated subprocesses.

## Regression suite

A local, standard-library-only suite lives under `tests/`:

```bash
./tests/run_regression.sh        # or: python3 -I tests/run_regression.py
```

It covers space-bearing paths and arguments, legacy string semantics,
non-zero exits, separate `stdout`/`stderr`, timeout tree-kill (including a
`SIGTERM`-ignoring child that requires `SIGKILL`), orphan detection,
output truncation, repeated consecutive calls, `cwd`/`env`, invalid
arguments, `close()` teardown isolation, and the stable `RunResult`
surface. The Windows-specific branch is reported as skipped on non-Windows
hosts.
