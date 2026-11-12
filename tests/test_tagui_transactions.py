"""regression tests for bounded, cancellable tagui instruction transactions

the real tagui executable is replaced by a scripted fake subprocess, so the
transaction logic (instruction-id matching, bounded waits, temp state
cleanup and recovery) can be exercised deterministically without a browser.

run with:  python3 -I tests/test_tagui_transactions.py
"""

import os
import sys
import time
import glob
import shutil
import tempfile
import threading
import unittest

# make the repo root importable when the test file is run directly
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(TESTS_DIR)
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import tagui


LIVE_MODE_LINE = 'LIVE MODE - type done to quit'
LISTENING_LINE = '[RPA][%d] - listening for inputs'


class FakeStdin(object):
    """records writes like subprocess pipes, hands whole lines to the fake"""

    def __init__(self, fake):
        self._fake = fake
        self._buffer = ''
        self._lock = threading.Lock()

    def write(self, data):
        if isinstance(data, bytes):
            data = data.decode('utf-8')
        with self._lock:
            self._buffer += data
            while '\n' in self._buffer:
                line, self._buffer = self._buffer.split('\n', 1)
                self._fake.handle_stdin_line(line)

    def flush(self):
        pass


class FakeStdout(object):
    """line-based stand-in for the tagui process stdout pipe"""

    def __init__(self, fake):
        self._fake = fake

    def readline(self):
        return self._fake._out_queue.get()


class FakePopen(object):
    """scripted tagui subprocess replacement for subprocess.Popen"""

    def __init__(self, behaviors=None):
        # behaviors is a dict keyed by instruction marker id (first real
        # instruction is id 1), values:
        # 'normal' (default), 'late-marker', 'duplicate-marker',
        # 'silent' (no listening marker), 'exit' (process dies)
        self._behaviors = behaviors or {}
        # marker ids whose dump instruction must NOT write a result file,
        # so the python-side result wait can exercise its bounded timeout
        self._no_output_ids = set()
        self._out_queue = tagui.queue.Queue()
        self.stdin = FakeStdin(self)
        self.stdout = FakeStdout(self)
        self.stderr = FakeStdout(self)
        self.returncode = None
        self._closed = False
        self._lock = threading.Lock()
        # tagui banner that init() waits for
        self._out_queue.put(LIVE_MODE_LINE.encode('utf-8'))

    # subprocess.Popen-compatible surface used by tagui.py
    def poll(self):
        return self.returncode

    def terminate(self):
        with self._lock:
            if self.returncode is None:
                self.returncode = -15
                self._closed = True
                self._out_queue.put(b'')

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        if self.returncode is None and timeout is not None:
            deadline = time.time() + timeout
            while self.returncode is None and time.time() < deadline:
                time.sleep(0.01)
        return self.returncode

    def _emit(self, text):
        with self._lock:
            if not self._closed:
                self._out_queue.put(text.encode('utf-8'))

    def _emit_listening(self, marker_id):
        self._emit(LISTENING_LINE % marker_id)

    def _exit(self, code = 0):
        with self._lock:
            self.returncode = code
            self._closed = True
            self._out_queue.put(b'')

    @staticmethod
    def _echo_marker_id(echoed):
        """extract n from an echoed line like [RPA][n] - ..."""
        prefix = '[RPA]['
        if not echoed.startswith(prefix):
            return None
        # the id sits in the second bracket group, after '[RPA]['
        end = echoed.find(']', len(prefix))
        if end == -1:
            return None
        token = echoed[len(prefix):end]
        try:
            return int(token)
        except ValueError:
            return None

    def handle_stdin_line(self, line):
        line = line.strip()
        if not line:
            return

        if line == 'done':
            self._exit(0)
            return

        if not line.startswith('echo "'):
            # real instruction lines (click, read, dump ...) need no stdout
            # reply; dump output files are produced from the echo line below
            return

        echoed = line[len('echo "'):-1]

        if echoed == '[RPA][STARTED]':
            return

        marker_id = self._echo_marker_id(echoed)

        # listening marker: this is what closes one instruction transaction
        if tagui._listening_marker_id(echoed) is not None:
            behavior = self._behaviors.get(marker_id, 'normal')
            if behavior == 'late-marker':
                # reply with the previous instruction's id, then the right one
                self._emit_listening(marker_id - 1)
                self._emit_listening(marker_id)
            elif behavior == 'duplicate-marker':
                # same marker emitted twice, only one call may be released
                self._emit_listening(marker_id)
                self._emit_listening(marker_id)
            elif behavior == 'silent':
                pass
            elif behavior == 'exit':
                self._exit(1)
            else:
                self._emit_listening(marker_id)
            return

        # echo line of a real instruction; if it is a dump to a transaction
        # result file, the fake tagui writes that file after executing.
        # strip the '[RPA][n] - ' prefix first so 'dump ... to ...' matches
        # whether it starts the instruction or appears after another keyword
        instruction_text = echoed.split(' - ', 1)[1] if ' - ' in echoed else echoed
        if instruction_text.startswith('dump ') and ' to ' in instruction_text:
            dumped = instruction_text[len('dump '):]
            expression, filename = dumped.split(' to ', 1)
            if marker_id not in self._no_output_ids:
                # real tagui runs in the directory captured at init(); write
                # relative result files there, exactly where _tagui_output()
                # looks via the init-directory fallback
                if not os.path.isabs(filename):
                    base_dir = tagui._tagui_init_directory or os.getcwd()
                    filename = os.path.join(base_dir, filename)
                with open(filename, 'w') as output_file:
                    output_file.write('fake-result')


def start_fake_session(behaviors=None):
    """drive tagui.init() against a scripted fake subprocess in cwd"""
    fake = FakePopen(behaviors)
    tagui._process = fake
    tagui._tagui_id = 0
    tagui._tagui_started = False
    tagui._tagui_visual = False
    tagui._tagui_chrome = False
    tagui._tagui_init_directory = os.getcwd()
    tagui._tagui_download_directory = os.getcwd()
    tagui._start_reader()

    # emulate the tail of init(): banner -> start markers -> listening id 0
    # read through the same queue as the reader thread (like real init())
    banner = tagui._read_output_line(tagui._transaction_deadline())
    assert LIVE_MODE_LINE in banner
    tagui._tagui_write('echo "[RPA][STARTED]"\n')
    tagui._tagui_write('echo "[RPA][0] - listening for inputs"\n')
    tagui._tagui_started = True
    assert tagui._wait_for_listening(0, tagui._transaction_deadline())
    tagui._tagui_id = 1
    return fake


class TransactionTests(unittest.TestCase):

    def setUp(self):
        self._workdir = tempfile.mkdtemp(prefix = 'rpa_tx_test_')
        self._previous_dir = os.getcwd()
        os.chdir(self._workdir)
        # keep the tests fast and deterministic
        tagui._tagui_delay = 0.005
        tagui._tagui_transaction_timeout = 0.2
        tagui._tagui_error = False
        tagui._tagui_debug = False

    def tearDown(self):
        tagui._stop_reader()
        tagui._process = None
        tagui._tagui_started = False
        tagui._tagui_transaction_timeout = 60.0
        os.chdir(self._previous_dir)
        shutil.rmtree(self._workdir, ignore_errors = True)

    def test_stale_reader_cannot_pollute_a_new_session_queue(self):
        # regression: a drainer left over from a failed/aborted process must
        # be bound to its own queue, so its end-of-stream signal cannot kill
        # a later session that reuses the module globals
        old_fake = FakePopen()
        tagui._process = old_fake
        tagui._start_reader()
        old_queue = tagui._output_queue
        # a new session starts and installs a fresh queue while the old
        # drainer is still blocked on the old process's stdout
        new_fake = FakePopen()
        tagui._process = new_fake
        tagui._start_reader()
        new_queue = tagui._output_queue
        self.assertIsNot(old_queue, new_queue)
        # ending the old process wakes its stale drainer
        old_fake._exit(0)
        # drain everything the stale thread emits (banner first, then EOF)
        saw_end_of_stream = False
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                if old_queue.get_nowait() == b'':
                    saw_end_of_stream = True
                    break
            except tagui.queue.Empty:
                time.sleep(0.01)
        # the stale EOF signal landed only in the old queue, never the new one
        self.assertTrue(saw_end_of_stream)
        # the new queue holds only its own banner; crucially it has no EOF
        # marker that a stale drainer could have injected
        new_contents = []
        while not new_queue.empty():
            new_contents.append(new_queue.get_nowait())
        self.assertIn(LIVE_MODE_LINE.encode('utf-8'), new_contents)
        self.assertNotIn(b'', new_contents)
        # the live session still produces readable output
        new_queue.put((LISTENING_LINE % 0).encode('utf-8'))
        self.assertEqual(
            tagui._read_output_line(time.time() + 1.0),
            LISTENING_LINE % 0)
        new_fake._exit(0)

    def test_marker_id_parsing(self):
        parse = tagui._listening_marker_id
        self.assertEqual(parse('[RPA][7] - listening for inputs'), 7)
        self.assertEqual(parse('  [RPA][0] - listening for inputs\r\n'), 0)
        # markers for other ids or malformed lines must not match
        self.assertIsNone(parse('[RPA][] - listening for inputs'))
        self.assertIsNone(parse('[RPA][x] - listening for inputs'))
        self.assertIsNone(parse('[RPA][7] - something else'))
        self.assertIsNone(parse('unrelated output'))

    def test_happy_path_send_and_consecutive_calls(self):
        fake = start_fake_session()
        self.assertTrue(tagui.send('click btn'))
        self.assertEqual(tagui._tagui_id, 2)
        self.assertTrue(tagui.send('type box as hello'))
        self.assertEqual(tagui._tagui_id, 3)
        # every instruction used its own id on the wire
        self.assertTrue(tagui._started())
        self.assertIsNone(fake.poll())

    def test_out_of_order_late_marker_does_not_release_call_early(self):
        # instruction id 1 sends back id 0 first (late marker), then id 1
        fake = start_fake_session({1: 'late-marker'})
        started_at = time.time()
        self.assertTrue(tagui.send('click btn'))
        # the id-0 marker must have been consumed and ignored, not returned
        # instantly as a false positive; the call completes on id 1 instead
        self.assertEqual(tagui._tagui_id, 2)
        # nothing left queued that could leak into the next transaction
        self.assertTrue(fake._out_queue.empty())
        self.assertLess(time.time() - started_at, tagui._tagui_transaction_timeout)
        # next call still works with a fresh id
        self.assertTrue(tagui.send('click other'))
        self.assertEqual(tagui._tagui_id, 3)

    def test_duplicate_marker_releases_only_one_call(self):
        fake = start_fake_session({1: 'duplicate-marker'})
        self.assertTrue(tagui.send('click btn'))
        self.assertEqual(tagui._tagui_id, 2)
        # the second copy of the same marker must be drained on transaction
        # cleanup, so instruction 2 starts with an empty output queue
        self.assertTrue(fake._out_queue.empty())
        self.assertTrue(tagui.send('click other'))
        self.assertEqual(tagui._tagui_id, 3)

    def test_send_timeout_is_bounded_and_session_recovers(self):
        start_fake_session({1: 'silent'})
        started_at = time.time()
        self.assertFalse(tagui.send('click btn'))
        elapsed = time.time() - started_at
        # bounded: fails near the transaction timeout instead of hanging
        self.assertGreaterEqual(elapsed, tagui._tagui_transaction_timeout - 0.05)
        self.assertLess(elapsed, tagui._tagui_transaction_timeout + 1.0)
        # session stays usable and the timed-out id was retired
        self.assertTrue(tagui._started())
        self.assertEqual(tagui._tagui_id, 2)
        self.assertTrue(tagui.send('click recovery'))
        self.assertEqual(tagui._tagui_id, 3)

    def test_send_fails_when_tagui_exits_and_close_recovers(self):
        fake = start_fake_session({1: 'exit'})
        self.assertFalse(tagui.send('click btn'))
        # process death must flip session state, not hang or raise
        self.assertFalse(tagui._started())
        self.assertIsNotNone(fake.poll())
        # after death, init() can start a brand new session
        fake2 = start_fake_session()
        self.assertTrue(tagui._started())
        self.assertTrue(tagui.send('click after-restart'))
        # and close() terminates cleanly with a bounded wait
        self.assertTrue(tagui.close())
        self.assertIsNotNone(fake2.poll())
        self.assertFalse(tagui._started())

    def test_result_wait_timeout_on_missing_output_file(self):
        # read('page') issues two transactions: the read instruction (id 1)
        # completes normally, its dump (id 2) writes no file so the result
        # wait hits the bounded timeout instead of hanging forever
        fake = start_fake_session()
        fake._no_output_ids.add(2)
        started_at = time.time()
        result = tagui.read('page')
        self.assertEqual(result, '')
        self.assertGreaterEqual(
            time.time() - started_at, tagui._tagui_transaction_timeout - 0.05)
        # the aborted transaction's result file must not linger
        self.assertEqual(glob.glob('rpa_python*.txt'), [])
        # session still usable afterwards
        self.assertTrue(tagui._started())
        self.assertIs(fake, tagui._process)

    def test_result_file_is_scoped_per_transaction(self):
        start_fake_session()
        # capture the id owned by this transaction before send() advances it
        instruction_id = tagui._tagui_id
        result_file = tagui._result_file_for(instruction_id)
        self.assertTrue(tagui.send('dump dom_result to ' + result_file))
        content = tagui._tagui_output(instruction_id)
        self.assertEqual(content, 'fake-result')
        # file is consumed by the successful transaction
        self.assertFalse(os.path.isfile(result_file))
        self.assertEqual(glob.glob('rpa_python*.txt'), [])

    def test_late_result_file_cannot_be_read_by_next_call(self):
        start_fake_session()
        first_id = tagui._tagui_id
        first_file = tagui._result_file_for(first_id)
        # simulate the previous call's dump landing only after it timed out:
        # its own file exists, but the next call waits on a different file
        tagui._abort_transaction(first_id)
        with open(first_file, 'w') as stale_file:
            stale_file.write('stale')
        next_id = tagui._tagui_id
        self.assertNotEqual(next_id, first_id)
        next_file = tagui._result_file_for(next_id)
        self.assertNotEqual(next_file, first_file)
        # abort cleanup of the new transaction must not touch the old file
        # semantics: the new call only ever watches its own file
        self.assertTrue(os.path.isfile(first_file))
        os.remove(first_file)

    def test_abort_cleans_only_its_own_result_file(self):
        start_fake_session()
        other_id = tagui._tagui_id
        other_file = tagui._result_file_for(other_id)
        with open(other_file, 'w') as kept_file:
            kept_file.write('keep')
        # a later transaction aborts; glob cleanup is id-scoped
        later_id = other_id + 5
        later_file = tagui._result_file_for(later_id)
        with open(later_file, 'w') as dead_file:
            dead_file.write('drop')
        tagui._abort_transaction(later_id)
        self.assertTrue(os.path.isfile(other_file))
        self.assertFalse(os.path.isfile(later_file))
        os.remove(other_file)

    def test_close_is_bounded_when_tagui_ignores_done(self):
        fake = start_fake_session()
        # make the fake ignore 'done' by neutering its exit handler
        original_handler = fake.handle_stdin_line
        def hanging_handler(line):
            if line.strip() == 'done':
                return
            original_handler(line)
        fake.handle_stdin_line = hanging_handler
        started_at = time.time()
        self.assertTrue(tagui.close())
        elapsed = time.time() - started_at
        # terminate() path: bounded instead of an infinite poll loop
        self.assertGreaterEqual(elapsed, tagui._tagui_transaction_timeout - 0.05)
        self.assertLess(elapsed, tagui._tagui_transaction_timeout + 2.0)
        self.assertIsNotNone(fake.poll())
        self.assertFalse(tagui._started())

    def test_close_removes_all_leftover_result_files(self):
        start_fake_session()
        for leftover_id in (1, 2, 3):
            with open(tagui._result_file_for(leftover_id), 'w') as leftover:
                leftover.write('x')
        with open('rpa_python.txt', 'w') as legacy:
            legacy.write('x')
        self.assertTrue(tagui.close())
        self.assertEqual(glob.glob('rpa_python*.txt'), [])

    def test_transaction_timeout_public_api(self):
        self.assertEqual(tagui.transaction_timeout(), 0.2)
        self.assertTrue(tagui.transaction_timeout(2.5))
        self.assertEqual(tagui.transaction_timeout(), 2.5)
        # 0 restores unbounded waiting
        self.assertTrue(tagui.transaction_timeout(0))
        self.assertIsNone(tagui._transaction_deadline())
        tagui.transaction_timeout(0.2)
        # error() mode is preserved: timeout failures raise instead of print
        tagui.error(True)
        start_fake_session({1: 'silent'})
        with self.assertRaises(Exception):
            tagui.send('click btn')
        self.assertTrue(tagui._started())

    def test_read_public_function_happy_path_and_id_binding(self):
        start_fake_session()
        first = tagui._tagui_id
        # read('page') skips exist(): one read transaction + one dump
        # transaction, and the dump must wait on the id it actually wrote to
        self.assertEqual(tagui.read('page'), 'fake-result')
        self.assertEqual(tagui._tagui_id, first + 2)
        # a second read must not be confused by the first call's result file
        self.assertEqual(tagui.read('page'), 'fake-result')
        self.assertEqual(tagui._tagui_id, first + 4)
        self.assertEqual(glob.glob('rpa_python*.txt'), [])

    def test_consecutive_result_waits_use_distinct_files_and_succeed(self):
        start_fake_session()
        files_used = []
        for _ in range(3):
            instruction_id = tagui._tagui_id
            result_file = tagui._result_file_for(instruction_id)
            files_used.append(result_file)
            self.assertTrue(tagui.send('dump dom_result to ' + result_file))
            self.assertEqual(tagui._tagui_output(instruction_id), 'fake-result')
            # each successful transaction consumes and deletes its own file
            self.assertFalse(os.path.isfile(result_file))
        # every consecutive call had a unique result file
        self.assertEqual(len(set(files_used)), len(files_used))
        self.assertEqual(glob.glob('rpa_python*.txt'), [])


if __name__ == '__main__':
    unittest.main(verbosity = 2)
