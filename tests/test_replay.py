"""Raw replay, ограничение диска, PID, права и неизменность удалённых исходов."""
from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import ssh_relay
import ssh_relay_core as core
import ssh_relay_replay as replay
import ssh_relay_replay_cli as cli
import ssh_relay_replay_platform as platform


def lock_contender(state, connection, before_unlink=None, resume=None):
    """Spawn-safe worker: pause after stale identity checks, immediately before unlink."""
    store = replay.Store(Path(state))
    unlink = Path.unlink
    paused = False

    def controlled_unlink(path, *args, **kwargs):
        nonlocal paused
        if before_unlink is not None and path == store.root / 'gc.lock' and not paused:
            paused = True
            before_unlink.set()
            if not resume.wait(20):
                raise RuntimeError('resume timeout')
        return unlink(path, *args, **kwargs)

    try:
        with patch.object(Path, 'unlink', controlled_unlink), store.locked():
            connection.send('acquired')
            if not connection.poll(20):
                raise RuntimeError('release timeout')
            connection.recv()
    except replay.ReplayError:
        connection.send('busy')
    except BaseException as exc:
        connection.send(type(exc).__name__ + ': ' + str(exc))
        raise
    finally:
        connection.close()


class ReplayTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get('LOCALAPPDATA') if os.name == 'nt' else None
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.store = replay.Store(self.state)

    def writer(self, *, session='test', rid=None):
        return self.store.begin(rid=rid or str(uuid.uuid4()), session=session, action='exec',
                                command='secret-command TOKEN', risky=False, owners=platform.owner_chain())

    def finish(self, writer, *, unknown=False, code=0):
        return writer.finish(operation_status='unknown' if unknown else ('succeeded' if not code else 'command_failed'),
                             command_status='unknown' if unknown else ('succeeded' if not code else 'failed'),
                             command_exit_code=None if unknown else code, receipt_status='not_requested')

    def read(self, writer, encoding='utf-8'):
        return self.store.replay(rid=writer.data['request_id'], session='test', encoding=encoding, owners=[])

    def test_raw_bytes_separate_strict_decode_and_safe_metadata(self):
        writer = self.writer()
        raw = 'Привет'.encode('cp866') + b'\x00'
        writer.capture('stdout', raw)
        writer.capture('stderr', b'error\n')
        self.assertEqual(self.finish(writer)['replay_status'], 'available')
        self.assertEqual((writer.directory/'stdout.bin').read_bytes(), raw)
        self.assertEqual(self.read(writer, 'cp866')['stdout'], 'Привет\x00')
        self.assertEqual(self.read(writer, 'cp866')['stderr'], 'error\n')
        with self.assertRaises(replay.ReplayError):
            self.read(writer)
        metadata = (writer.directory/'metadata.json').read_text()
        self.assertNotIn('secret-command', metadata)
        self.assertNotIn('TOKEN', metadata)
        self.assertEqual(set(p.name for p in writer.directory.iterdir()), {'stdout.bin', 'stderr.bin', 'metadata.json'})

    def test_rolling_tail_incremental_hash_and_partial_status(self):
        writer = self.writer()
        chunk = b'0123456789abcdef' * 4096
        digest = hashlib.sha256()
        for _ in range(70):
            writer.capture('stdout', chunk)
            digest.update(chunk)
            self.assertLessEqual((writer.directory/'stdout.bin').stat().st_size, replay.STREAM_LIMIT)
        status = self.finish(writer)
        self.assertEqual(status, {'replay_status': 'partial', 'replay_truncated': True})
        self.assertEqual(writer.data['stdout']['sha256_full'], digest.hexdigest())
        self.assertEqual(writer.data['stdout']['dropped_prefix_bytes'], 6 * len(chunk))
        result = self.read(writer)
        self.assertFalse(result['replay_complete'])
        self.assertEqual(len(result['stdout']), replay.STREAM_LIMIT)

    def test_ttl_read_does_not_move_terminal_timestamp(self):
        writer = self.writer()
        self.finish(writer, code=7)
        before = writer.data['finished_at_utc']
        self.assertEqual(writer.data['retention_class'], 'clean')
        self.read(writer, 'cp866')
        metadata = self.store.metadata(writer.directory)
        self.assertEqual(metadata['finished_at_utc'], before)
        self.assertEqual(metadata['retention_class'], 'suspect')
        with patch.object(replay.time, 'time', return_value=replay.timestamp(before) + replay.SUSPECT_TTL + 1):
            with self.assertRaises(replay.ReplayError):
                self.read(writer)
        self.assertFalse(writer.directory.exists())

    def test_clean_expires_in_five_minutes(self):
        writer = self.writer()
        self.finish(writer)
        with patch.object(replay.time, 'time', return_value=replay.timestamp(writer.data['finished_at_utc']) + 301):
            self.store.gc()
        self.assertFalse(writer.directory.exists())

    def test_expired_unremovable_is_unreadable_and_still_charged(self):
        writer = self.writer()
        writer.capture('stdout', b'expired')
        self.finish(writer)
        writer.data['finished_at_utc'] = replay.utc(time.time() - replay.CLEAN_TTL - 10)
        replay.write_json(writer.directory, writer.data)
        with patch.object(self.store, 'remove', return_value=False) as remove:
            for rid in (writer.data['request_id'], None):
                with self.subTest(rid=rid), self.assertRaises(replay.ReplayError):
                    self.store.replay(rid=rid, session='test', encoding='utf-8', owners=[])
            records = self.store.collect(self.store.records())
            self.assertEqual([(p, size) for p, _, size in records], [(writer.directory, 7)])
            with patch.object(replay, 'SESSION_LIMIT', replay.REQUEST_LIMIT):
                with self.assertRaisesRegex(replay.ReplayError, 'бюджета'):
                    self.writer()
            self.assertTrue(remove.called)
        self.assertEqual((writer.directory / 'stdout.bin').read_bytes(), b'expired')
        self.store.gc()
        self.assertFalse(writer.directory.exists())

    def test_last_ignores_expired_unremovable_candidate(self):
        old = self.writer()
        self.finish(old)
        live = self.writer()
        live.capture('stdout', b'live')
        self.finish(live)
        old.data['finished_at_utc'] = replay.utc(time.time() - replay.CLEAN_TTL - 10)
        replay.write_json(old.directory, old.data)
        with patch.object(self.store, 'remove', return_value=False):
            result = self.store.replay(rid=None, session='test', encoding='utf-8', owners=[])
        self.assertEqual(result['request_id'], live.directory.name)
        self.assertEqual(result['stdout'], 'live')

    def test_partial_cleanup_keeps_expiry_metadata(self):
        writer = self.writer()
        writer.capture('stderr', b'expired')
        self.finish(writer)
        writer.data['finished_at_utc'] = replay.utc(time.time() - replay.CLEAN_TTL - 10)
        replay.write_json(writer.directory, writer.data)
        unlink, iterdir = Path.unlink, Path.iterdir

        def refuse_raw(path, *args, **kwargs):
            if path == writer.directory / 'stderr.bin':
                raise PermissionError('sharing violation')
            return unlink(path, *args, **kwargs)

        def metadata_first(path):
            return iter(sorted(iterdir(path), key=lambda p: p.name != 'metadata.json'))

        with patch.object(Path, 'unlink', refuse_raw), patch.object(Path, 'iterdir', metadata_first):
            for _ in range(2):
                with self.assertRaisesRegex(replay.ReplayError, 'истёк'):
                    self.read(writer)
                self.assertTrue((writer.directory / 'metadata.json').is_file())
                self.assertEqual(self.store.records()[0][2], 7)
        self.store.gc()
        self.assertFalse(writer.directory.exists())

    def test_unknown_and_abandoned_remain_partial(self):
        writer = self.writer()
        writer.capture('stdout', b'partial')
        self.finish(writer, unknown=True)
        self.assertFalse(self.read(writer)['replay_complete'])
        active = self.writer()
        active.capture('stderr', b'crash')
        with patch.object(replay, 'alive', return_value=False):
            recovered = self.read(active)
        self.assertFalse(recovered['replay_complete'])
        self.assertEqual(recovered['stderr'], 'crash')
        self.assertEqual(self.store.metadata(active.directory)['state'], 'abandoned')

    def test_active_is_never_read_or_evicted(self):
        active = self.writer()
        with self.assertRaises(replay.ReplayError):
            self.read(active)
        self.writer()
        with self.assertRaises(replay.ReplayError):
            self.writer()
        self.store.gc()
        self.assertTrue(active.directory.exists())

    def test_global_reservations_across_sessions(self):
        writers = [self.writer(session=str(n)) for n in range(8)]
        with self.assertRaises(replay.ReplayError):
            self.writer(session='overflow')
        self.assertEqual(len(list(self.store.requests.iterdir())), 8)
        self.finish(writers[0])
        self.writer(session='next')

    def test_count_limit_and_clean_eviction_priority(self):
        suspect = self.writer()
        suspect.capture('stdout', b'\xff')
        self.finish(suspect)
        clean = self.writer()
        self.finish(clean)
        for _ in range(replay.SESSION_COUNT):
            writer = self.writer()
            self.finish(writer)
        self.assertTrue(suspect.directory.exists())
        self.assertFalse(clean.directory.exists())
        self.assertLessEqual(len(list(self.store.requests.iterdir())), replay.SESSION_COUNT)

    def test_last_ambiguous_and_session_filter(self):
        writer = self.writer()
        self.finish(writer)
        other = self.writer(session='other')
        self.finish(other)
        args = dict(rid=None, session='test', encoding='utf-8', owners=[])
        self.assertEqual(self.store.replay(**args)['request_id'], writer.data['request_id'])
        second = self.writer()
        self.finish(second)
        with self.assertRaisesRegex(replay.ReplayError, 'ambiguous_last'):
            self.store.replay(**args)

    def test_duplicate_uuid_and_invalid_identity(self):
        writer = self.writer()
        with self.assertRaises(replay.ReplayError):
            self.writer(rid=writer.data['request_id'])
        for bad in ('../x', str(uuid.uuid1()), 'foo', None):
            with self.assertRaises(replay.ReplayError):
                replay.request_id(bad)

    def test_corrupt_metadata_explicit_only_and_hash_mismatch(self):
        writer = self.writer()
        writer.capture('stdout', b'data')
        self.finish(writer)
        (writer.directory/'stdout.bin').write_bytes(b'bad')
        with self.assertRaises(replay.ReplayError):
            self.read(writer)
        (writer.directory/'metadata.json').write_bytes(b'{')
        result = self.read(writer)
        self.assertFalse(result['replay_complete'])
        self.assertEqual(result['source_operation_status'], 'unknown')
        with self.assertRaises(replay.ReplayError):
            self.store.replay(rid=None, session='test', encoding='utf-8', owners=[])

    def test_unknown_entry_and_cleanup_never_follow_links(self):
        writer = self.writer()
        self.finish(writer)
        extra = writer.directory/'do-not-delete'
        extra.write_text('protected')
        self.assertFalse(self.store.remove(writer.directory))
        self.store.gc()
        self.assertEqual(extra.read_text(), 'protected')
        with self.assertRaises(replay.ReplayError):
            self.writer()

    def test_explicit_read_isolated_from_other_problem_directory(self):
        good = self.writer()
        good.capture('stdout', b'good')
        self.finish(good)
        bad = self.writer()
        self.finish(bad)
        (bad.directory / 'unknown').write_bytes(b'leave alone')
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in bad.directory.iterdir()}
        self.assertEqual(self.read(good)['stdout'], 'good')
        with self.assertRaises(replay.ReplayError):
            self.read(bad)
        with self.assertRaises(replay.ReplayError):
            self.store.replay(rid=None, session='test', encoding='utf-8', owners=[])
        with self.assertRaises(replay.ReplayError):
            self.writer()
        self.store.gc()
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in bad.directory.iterdir()})

    @unittest.skipIf(os.name == 'nt', 'POSIX modes и symlink')
    def test_private_modes_symlink_and_hardlink(self):
        writer = self.writer()
        self.finish(writer)
        self.assertEqual(writer.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((writer.directory/'stdout.bin').stat().st_mode & 0o777, 0o600)
        outside = self.state/'outside'
        outside.write_text('protected')
        path = writer.directory/'stdout.bin'
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(replay.ReplayError):
            self.read(writer)
        self.assertFalse(self.store.remove(writer.directory))
        path.unlink()
        os.link(outside, path)
        with self.assertRaises(replay.ReplayError):
            self.read(writer)
        self.assertEqual(outside.read_text(), 'protected')

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL')
    def test_windows_private_acl(self):
        writer = self.writer()
        platform.windows_private(writer.directory)
        platform.windows_private(writer.directory/'stdout.bin')
        self.finish(writer)
        self.assertTrue(self.read(writer)['replay_complete'])

    def test_reparse_attribute_rejected(self):
        writer = self.writer()
        real = Path.lstat
        def changed(path):
            info = real(path)
            if path == writer.directory:
                class Reparse:
                    st_mode = info.st_mode
                    st_file_attributes = 0x400
                return Reparse()
            return info
        with patch.object(Path, 'lstat', changed):
            with self.assertRaises(replay.ReplayError):
                platform.check_path(writer.directory, directory=True)

    def test_lock_weak_owner_never_removed(self):
        self.store.prepare()
        path = self.store.root/'gc.lock'
        with replay.open_file(path, create=True, write=True) as stream:
            stream.write(json.dumps({'owner': {'pid': 9876, 'platform': 'weak', 'start': None}}).encode())
        with self.assertRaises(replay.ReplayError):
            with self.store.locked():
                pass
        self.assertTrue(path.exists())

    def test_lock_live_owner_refuses_competing_process(self):
        self.store.prepare()
        script = 'from pathlib import Path; from ssh_relay_replay import Store; import sys\ns=Store(Path(sys.argv[1]))\nwith s.locked(): print("acquired")'
        with self.store.locked():
            result = subprocess.run([sys.executable, '-c', script, str(self.state)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('acquired', result.stdout)

    def start_lock_contender(self, context, *pause):
        receiver, sender = context.Pipe()
        worker = context.Process(target=lock_contender, args=(str(self.state), sender, *pause))
        worker.start()
        sender.close()

        def cleanup():
            if not worker._closed:
                try:
                    receiver.send('release')
                except (BrokenPipeError, OSError):
                    pass
                worker.join(5)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(5)
                worker.close()
            receiver.close()
        self.addCleanup(cleanup)
        return worker, receiver

    def lock_result(self, receiver):
        self.assertTrue(receiver.poll(20), 'lock contender did not report')
        return receiver.recv()

    def test_stale_lock_recovery_serializes_processes_before_unlink(self):
        self.store.prepare()
        # A real exited process supplies a strong identity for the stale lock.
        script = 'import os,json; from ssh_relay_replay_platform import process_identity; print(json.dumps(process_identity(os.getpid())))'
        dead = json.loads(subprocess.check_output([sys.executable, '-c', script], text=True))
        self.assertIs(platform.alive(dead), False)
        with replay.open_file(self.store.root / 'gc.lock', create=True, write=True) as stream:
            replay.write_all(stream, json.dumps({'owner': dead}).encode())
        context = multiprocessing.get_context('spawn')
        paused, resume = context.Event(), context.Event()
        b, b_result = self.start_lock_contender(context, paused, resume)
        self.addCleanup(resume.set)
        self.assertTrue(paused.wait(20), 'B did not reach stale unlink')
        a, a_result = self.start_lock_contender(context)
        a_status = self.lock_result(a_result)
        resume.set()
        b_status = self.lock_result(b_result)
        self.assertEqual((a_status, b_status), ('busy', 'acquired'),
                         'stale recovery allowed simultaneous lock owners')
        b_result.send('release')
        for worker in (a, b):
            worker.join(20)
            self.assertEqual(worker.exitcode, 0)
        with self.store.locked():
            pass

    def test_kernel_guard_released_after_process_death(self):
        self.store.prepare()
        context = multiprocessing.get_context('spawn')
        worker, result = self.start_lock_contender(context)
        self.assertEqual(self.lock_result(result), 'acquired')
        identity = replay.read_json(self.store.root / 'gc.lock')['owner']
        worker.terminate()
        worker.join(20)
        self.assertFalse(worker.is_alive())
        # Keep the Windows process handle open: PID/start still exist after exit.
        self.assertIs(platform.alive(identity), False)
        with self.store.locked():
            pass

    def test_guard_is_persistent_and_rejects_same_process_reentry(self):
        self.store.prepare()
        guard = self.store.root / 'gc.guard'
        with self.store.locked():
            first = guard.stat()
            with self.assertRaises(replay.ReplayError):
                with self.store.locked():
                    self.fail('second descriptor acquired guard')
            # Closing the rejected descriptor must not release the first lock.
            context = multiprocessing.get_context('spawn')
            worker, result = self.start_lock_contender(context)
            self.assertEqual(self.lock_result(result), 'busy')
            worker.join(20)
            self.assertEqual(worker.exitcode, 0)
        with self.store.locked():
            second = guard.stat()
            self.assertEqual((first.st_dev, first.st_ino), (second.st_dev, second.st_ino))
        self.assertTrue(guard.is_file())

    def test_unsafe_guard_fails_closed(self):
        self.store.prepare()
        guard = self.store.root / 'gc.guard'
        with replay.open_file(guard, create=True, write=True):
            pass
        os.link(guard, self.store.root / 'guard-alias')
        with self.assertRaises(replay.ReplayError):
            with self.store.locked():
                self.fail('hardlinked guard was accepted')
        self.assertFalse((self.store.root / 'gc.lock').exists())

    def test_pid_reuse_and_weak_identity(self):
        identity = {'pid': 1234, 'platform': 'linux', 'start': 'abc:1'}
        with patch.object(platform, 'process_identity', return_value={**identity, 'start': 'abc:2'}):
            self.assertFalse(platform.alive(identity))
        self.assertIsNone(platform.alive({'pid': 1234, 'platform': 'weak', 'start': None}))
        current = platform.process_identity(os.getpid())
        self.assertTrue(platform.alive(current))

    def test_cli_is_local_and_json_source_outcome_is_separate(self):
        writer = self.writer()
        writer.capture('stdout', b'ok')
        self.finish(writer, code=7)
        args = ssh_relay.build_parser().parse_args(['replay', '--name', 'test', '--request-id', writer.data['request_id'], '--encoding', 'utf-8', '--json'])
        output = io.StringIO()
        with patch.object(core, 'state_directory', return_value=self.state), patch.object(core, 'request_daemon', side_effect=AssertionError('SSH запрещён')), redirect_stdout(output):
            self.assertEqual(args.handler(args), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['source_command_exit_code'], 7)
        self.assertTrue(result['replay_complete'])

    def test_cli_decode_failure_produces_no_partial_output(self):
        writer = self.writer()
        writer.capture('stdout', b'valid')
        writer.capture('stderr', b'\xff')
        self.finish(writer)
        args = ssh_relay.build_parser().parse_args(['replay', '--name', 'test', '--request-id', writer.data['request_id'], '--encoding', 'utf-8'])
        output, error = io.StringIO(), io.StringIO()
        with patch.object(core, 'state_directory', return_value=self.state), redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(args.handler(args), 1)
        self.assertEqual(output.getvalue(), '')

    def test_capture_only_inside_explicit_scope(self):
        writer = self.writer()
        cli.capture_chunk('stdout', b'job')
        with cli.capturing(writer):
            cli.capture_chunk('stdout', b'user')
        cli.capture_chunk('stdout', b'receipt')
        self.finish(writer)
        self.assertEqual(self.read(writer)['stdout'], 'user')

    def test_storage_write_failure_releases_active_reservation(self):
        writer = self.writer()
        writer.capture('stdout', b'before')
        with patch.object(replay, 'open_file', side_effect=OSError('disk full')):
            writer.capture('stdout', b'after')
        result = self.finish(writer)
        self.assertEqual(result['replay_status'], 'unavailable')
        self.assertEqual(self.store.metadata(writer.directory)['state'], 'abandoned')
        restored = self.read(writer)
        self.assertEqual(restored['stdout'], 'before')
        self.assertFalse(restored['replay_complete'])

    def test_stale_strong_lock_recovered_and_live_lock_not_recovered(self):
        self.store.prepare()
        path = self.store.root/'gc.lock'
        with replay.open_file(path, create=True, write=True) as stream:
            stream.write(json.dumps({'owner': platform.process_identity(os.getpid())}).encode())
        with self.assertRaises(replay.ReplayError):
            with self.store.locked():
                pass
        with patch.object(replay, 'alive', return_value=False):
            with self.store.locked():
                self.assertTrue(path.exists())
        self.assertFalse(path.exists())

    def test_bad_metadata_types_and_missing_metadata_are_partial(self):
        writer = self.writer()
        writer.capture('stdout', b'raw')
        self.finish(writer)
        path = writer.directory/'metadata.json'
        data = json.loads(path.read_text())
        for key, value in [('stdout', None), ('writer_identity', None), ('command_exit_code', []), ('finished_at_utc', None)]:
            path.write_text(json.dumps({**data, key: value}))
            self.assertFalse(self.read(writer)['replay_complete'])
        path.unlink()
        self.assertFalse(self.read(writer)['replay_complete'])

    def test_codec_errors_and_missing_record_are_controlled(self):
        writer = self.writer()
        self.finish(writer)
        for codec in ('not-a-codec', 'hex'):
            with self.assertRaises(replay.ReplayError):
                self.read(writer, codec)
        with self.assertRaises(replay.ReplayError):
            self.store.replay(rid=str(uuid.uuid4()), session='test', encoding='utf-8', owners=[])

    def test_partial_multibyte_utf8_tail_is_suspect(self):
        writer = self.writer()
        writer.capture('stdout', b'\xe2\x82')
        self.finish(writer)
        self.assertEqual(writer.data['retention_class'], 'suspect')
        with self.assertRaises(replay.ReplayError):
            self.read(writer)

    def test_large_single_chunk_bounded_before_write(self):
        writer = self.writer()
        raw = b'a' * replay.STREAM_LIMIT + b'b'
        writer.capture('stderr', raw)
        self.finish(writer)
        self.assertEqual((writer.directory/'stderr.bin').read_bytes(), raw[-replay.STREAM_LIMIT:])

    @unittest.skipIf(os.name == 'nt', 'POSIX private modes')
    def test_public_mode_refused_without_permission_repair(self):
        writer = self.writer()
        path = writer.directory/'stdout.bin'
        path.chmod(0o644)
        with self.assertRaises(replay.ReplayError):
            replay.open_file(path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_unavailable_storage_never_blocks_command_helper(self):
        request = dict(request_id=str(uuid.uuid4()), command='not stored', action='exec', risky=False)
        with patch.object(replay.Store, 'begin', side_effect=replay.ReplayError('denied')):
            self.assertIsNone(cli.begin(core, request, {'name': 'test'}))
        with self.assertRaises(replay.ReplayError):
            cli.validate_request({'no_replay': 'false'})

    def test_owner_chain_does_not_retain_extra_process_data(self):
        chain = platform.sanitize_chain([dict(pid=123, ppid=1, platform='linux', start='abcd:1', argv='SECRET')])
        self.assertNotIn('SECRET', json.dumps(chain))
        self.assertEqual(platform.sanitize_chain([dict(pid='bad')]), [])
        self.assertEqual(platform.sanitize_chain(['bad']), [])
        self.assertEqual(platform.sanitize_chain([dict(pid=2, platform='linux', start='bad value')]), [])
        self.assertIsNone(platform.alive(None))

    def test_short_writes_are_completed_or_rejected(self):
        class ShortWriter:
            def __init__(self):
                self.data = bytearray()
            def write(self, data):
                self.data.extend(data[:2])
                return min(2, len(data))
        stream = ShortWriter()
        replay.write_all(stream, b'abcdef')
        self.assertEqual(stream.data, b'abcdef')
        with patch.object(stream, 'write', return_value=0):
            with self.assertRaises(OSError):
                replay.write_all(stream, b'x')

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL')
    def test_windows_public_acl_is_refused_without_repair(self):
        writer = self.writer()
        result = subprocess.run(['icacls', str(writer.directory), '/grant', '*S-1-1-0:(OI)(CI)R'], capture_output=True)
        self.assertEqual(result.returncode, 0)
        with self.assertRaises(replay.ReplayError):
            platform.windows_private(writer.directory)

    def test_sharing_violation_leaves_record_for_later_gc(self):
        writer = self.writer()
        self.finish(writer)
        with patch.object(Path, 'unlink', side_effect=PermissionError('sharing violation')):
            self.assertFalse(self.store.remove(writer.directory))
        self.assertTrue(writer.directory.exists())

    def test_disabled_and_unavailable_do_not_capture(self):
        request = {'request_id': str(uuid.uuid4()), 'no_replay': True}
        self.assertIsNone(cli.begin(core, request, {}))
        self.assertEqual(cli.finish(None, request, {'ok': True})['replay_status'], 'disabled')
        request['no_replay'] = False
        self.assertEqual(cli.finish(None, request, {'ok': True})['replay_status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
