"""Fake BCC/proc contracts: no BCC import, Linux attach, SDK, or signals."""
import copy
import ctypes as ct
import os
from pathlib import Path
import re
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

import observer as module
from contracts import decode_payload
from test_contracts import event, payload


class Table:
    def __init__(self, values=None, actions=None):
        self.values = values or {}
        self.actions = actions

    def __setitem__(self, key, value):
        self.values[key.value] = value.value

    def sum(self, key):
        self.actions.append('counter')
        return ct.c_ulonglong(self.values.get(key.value, 0))


class FakeBpf:
    def __init__(self, **kwargs):
        self.fds = {}
        self.actions = []
        self.uprobe_fds = {}
        self.pending = []
        self.tables = {'target': Table(), 'callers': Table(), 'counts': Table(actions=self.actions), 'events': self}
        self.cleanup_error = self.detach_error = False
        self.extra_fd = False

    def __getitem__(self, name):
        return self.tables[name]

    def open_perf_buffer(self, callback, page_cnt, lost_cb):
        self.callback, self.lost_cb = callback, lost_cb
        self.pages = page_cnt
        self.fds.update({10: 'cpu0 output', 11: 'cpu1 output'})
        if self.extra_fd:
            self.fds[12] = 'unexplained'

    def attach_uprobe(self, **kwargs):
        self.attach = kwargs
        self.uprobe_fds[b'owned_event'] = 20
        self.fds[20] = 'positive-pid attach'

    def detach_uprobe_event(self, name):
        self.actions.append('detach')
        if self.detach_error:
            raise RuntimeError('fake detach error')
        self.fds.pop(self.uprobe_fds.pop(name))

    def perf_buffer_poll(self, timeout):
        self.actions.append('poll')
        batch, self.pending = self.pending, []
        for cpu, data, size in batch:
            self.callback(cpu, ct.create_string_buffer(data), size)

    def cleanup(self):
        self.actions.append('cleanup')
        if self.cleanup_error:
            raise RuntimeError('fake cleanup error')
        self.fds.clear()
        self.uprobe_fds.clear()


class Observer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bpf = FakeBpf()
        self.procs = {
            100: dict(pid=100, tid=100, startticks=1000, ppid=50),
            300: dict(pid=300, tid=300, startticks=3000, ppid=100),
            200: dict(pid=200, tid=200, startticks=2000, ppid=100),
        }
        self.callers = list(range(400, 432))
        self.dead_tasks = set()
        self.image = dict(path='/frozen/libdatasystem.so', sha256='image', value=1, size=1201, offset=1, prologue64_sha256='prologue')
        for name, replacement in [
            ('task', self.task), ('perf_fds', lambda: dict(self.bpf.fds)),
            ('online_cpus', lambda: ('0-1', [0, 1])), ('sha', lambda path: 'image'),
            ('elf_entry', lambda *args: None),
        ]:
            p = patch.object(module, name, replacement)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(module.os, 'getpid', lambda: 300)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.instance = None
        self.addCleanup(self.close)

    def task(self, pid, tid=None):
        if pid in self.dead_tasks or pid not in self.procs:
            raise FileNotFoundError(pid)
        result = dict(self.procs[pid])
        if tid is not None and tid != pid:
            if pid != 200 or tid not in self.callers:
                raise FileNotFoundError(tid)
            result.update(tid=tid, startticks=tid * 10)
        return result

    def make(self, callback_bytes=72):
        self.instance = module.PodObserver(Path(self.tmp.name) / 'out', self.image, callback_bytes, 100, 1000)
        self.instance.maps_identity = lambda: dict(path=self.image['path'], dev=1, inode=2, maps=['exact'], sha256='image', all_maps='full synthetic maps')
        return self.instance

    def ready(self, callback_bytes=72):
        item = self.make(callback_bytes)
        item.prepare(lambda **kwargs: self.bpf)
        item.bind(200, 2000, self.callers, time.monotonic_ns() + 10**9)
        return item

    def close(self):
        if self.instance and not self.instance.closed:
            self.instance.finish(partial=True)

    def emit(self, item, data=None, size=72):
        data = data or payload(dict(event(tid=400), tgid=200, cpu=0))
        item._event(0, ct.create_string_buffer(data), size)

    def test_sibling_owned_children_and_positive_pid32(self):
        item = self.ready()
        self.assertEqual(self.bpf.attach['pid'], 200)
        self.assertEqual(len(self.bpf['callers'].values), 32)
        self.assertEqual(self.bpf['target'].values[0], 200)
        self.assertEqual(item.record['reader']['ppid'], item.record['target']['ppid'])
        self.assertEqual(item.record['status'], 'READY')
        self.assertFalse(item.record['full_coverage'])

    def test_foreign_controller_and_pid_reuse_rejected(self):
        item = self.make()
        item.prepare(lambda **kwargs: self.bpf)
        self.procs[200]['ppid'] = 1
        with self.assertRaises(RuntimeError):
            item.bind(200, 2000, self.callers, time.monotonic_ns() + 10**9)
        self.assertFalse(hasattr(self.bpf, 'attach'))
        self.assertTrue(item.closed)
        self.procs[200]['ppid'] = 100
        self.procs[100]['startticks'] += 1
        with self.assertRaises(RuntimeError):
            module.owned_descendant(200, dict(pid=100, tid=100, startticks=1000, ppid=50))

    def test_init_controller_and_wrong_reader_rejected(self):
        with self.assertRaises(RuntimeError):
            module.PodObserver(Path(self.tmp.name) / 'init', self.image, 72, 1, 1)
        item = self.ready()
        self.procs[300]['startticks'] += 1
        with self.assertRaises(RuntimeError):
            item.poll()
        self.procs[300]['startticks'] -= 1

    def test_final_ready_rechecks32caller_identity(self):
        item = self.make()
        item.prepare(lambda **kwargs: self.bpf)
        original = self.bpf.attach_uprobe
        def attach(**kwargs):
            original(**kwargs)
            self.callers.remove(431)
        self.bpf.attach_uprobe = attach
        with self.assertRaises(FileNotFoundError):
            item.bind(200, 2000, list(range(400, 432)), time.monotonic_ns() + 10**9)
        self.assertTrue(item.closed)
        self.assertIn('detach', self.bpf.actions)

    def test_late_epoch_and_extra_fds_fail_closed(self):
        item = self.make()
        self.bpf.extra_fd = True
        with self.assertRaises(RuntimeError):
            item.prepare(lambda **kwargs: self.bpf)
        self.assertTrue(item.closed)
        self.assertFalse(self.bpf.fds)
        self.assertEqual(item.record['status'], 'CLOSED_PARTIAL')

    def test_late_bind_does_not_extend_epoch(self):
        item = self.make()
        item.prepare(lambda **kwargs: self.bpf)
        with self.assertRaises(RuntimeError):
            item.bind(200, 2000, self.callers, time.monotonic_ns() - 1)
        self.assertFalse(hasattr(self.bpf, 'attach'))

    def test_target_and_controller_gone_still_detach_before_drain(self):
        item = self.ready()
        self.dead_tasks.update({200, 100})
        result = item.finish(partial=True)
        self.assertEqual(self.bpf.actions[0], 'detach')
        self.assertLess(self.bpf.actions.index('poll'), self.bpf.actions.index('counter'))
        self.assertEqual(self.bpf.actions[-1], 'cleanup')
        self.assertTrue(result['explicit_detach_before_drain'])
        self.assertFalse(result['remaining_perf_FDs'])
        self.assertTrue(item.raw.closed)
        actions = list(self.bpf.actions)
        self.assertIs(item.finish(), result)
        self.assertEqual(actions, self.bpf.actions)

    def test_detach_error_still_cleanup_and_preserves_failure(self):
        item = self.ready()
        self.bpf.detach_error = True
        result = item.finish()
        self.assertIn('detach error', result['error'])
        self.assertFalse(result['explicit_detach_before_drain'])
        self.assertFalse(result['remaining_perf_FDs'])
        self.assertTrue(item.raw.closed)

    def test_cleanup_error_never_claims_no_remaining_fds(self):
        item = self.ready()
        self.bpf.cleanup_error = True
        result = item.finish()
        self.assertTrue(result['remaining_perf_FDs'])
        self.assertIn('cleanup', result['error'])
        self.assertTrue(item.raw.closed)
        self.bpf.cleanup_error = False
        self.bpf.cleanup()

    def test_reparented_reader_cleans_same_owned_fds(self):
        item = self.ready()
        self.dead_tasks.update({200, 100})
        self.procs[300]['ppid'] = 1
        error = None
        try:
            item.finish(partial=True)
        except RuntimeError as caught:
            error = str(caught)
        finally:
            self.procs[300]['ppid'] = 100
        self.assertIsNone(error, 'unchanged reader PID/start must retain cleanup authority after reparent')
        self.assertTrue(item.closed)
        self.assertIn('detach', self.bpf.actions)
        self.assertFalse(item.record['remaining_perf_FDs'])

    def test_reparented_reader_cannot_start_new_control(self):
        item = self.ready()
        self.procs[300]['ppid'] = 1
        try:
            with self.assertRaises(RuntimeError):
                item.poll()
        finally:
            self.procs[300]['ppid'] = 100

    def test_same_pid_new_start_cannot_cleanup_saved_registration(self):
        item = self.ready()
        self.procs[300]['startticks'] += 1
        try:
            with self.assertRaises(RuntimeError):
                item.finish(partial=True)
            self.assertFalse(self.bpf.actions)
            self.assertFalse(item.closed)
        finally:
            self.procs[300]['startticks'] -= 1

    def test_native_framing_trailer_not_emitted(self):
        item = self.ready(76)
        data = payload(dict(event(tid=400), tgid=200, cpu=0))
        self.emit(item, data + b'\xa5' * 4, 76)
        self.assertEqual(item.decoded, 1)
        self.assertEqual(item.raw_bytes, 72)
        item.raw.flush()
        self.assertEqual((item.output / 'raw-events.bin').read_bytes(), data)
        self.assertTrue(item.record['native_framing_fixture_runtime_NOT_PROVEN'])

    def test_bad_record_kept_and_later_records_not_hidden(self):
        item = self.ready()
        data = bytearray(payload(dict(event(tid=400), tgid=200, cpu=0)))
        data[65] = 0xa5
        self.emit(item, bytes(data))
        self.emit(item)
        self.assertEqual(item.records, 2)
        self.assertEqual(item.decoded, 1)
        self.assertIsNotNone(item.failure)
        self.bpf['counts'].values.update({0: 2, 2: 2})
        result = item.finish()
        self.assertFalse(result['transport_check']['eligible'])

    def test_raw_bound_and_wrong_native_size_do_not_read_past_buffer(self):
        item = self.ready()
        with patch.object(module, 'MAX_RECORDS', 1):
            self.emit(item)
            self.emit(item)
        self.assertEqual(item.received, 2)
        self.assertEqual(item.records, 1)
        self.assertIn('hard bound', item.failure)
        item._event(0, ct.c_void_p(1), 1)
        self.assertEqual(item.records, 1)
        self.assertEqual(item.sizes[1], 1)

    def test_lost_is_separate_and_read_failure_conservation_not_eligibility(self):
        item = self.ready()
        self.emit(item)
        item._lost(0, 2)
        self.bpf['counts'].values.update({0: 3, 1: 1, 2: 1, 3: 1})
        result = item.finish()
        self.assertTrue(result['transport_check']['conserved'])
        self.assertFalse(result['transport_check']['eligible'])
        self.assertEqual(result['lost_callbacks'], 2)

    def test_lost_callback_stops_next_poll(self):
        item = self.ready()
        item._lost(0, 1)
        with self.assertRaises(RuntimeError):
            item.poll()

    def test_read_failed_stops_next_poll(self):
        item = self.ready()
        self.bpf['counts'].values.update({0: 1, 1: 1})
        with self.assertRaises(RuntimeError):
            item.poll()

    def test_submit_failed_stops_next_poll(self):
        item = self.ready()
        self.bpf['counts'].values.update({0: 1, 3: 1})
        with self.assertRaises(RuntimeError):
            item.poll()

    def test_active_submitted_not_yet_decoded_is_not_a_loss(self):
        item = self.ready()
        self.bpf['counts'].values.update({0: 2, 2: 1})
        item.poll()
        self.assertIsNone(item.failure)
        self.assertEqual(item.decoded, 0)

    def test_source_typed45bytes_padding_poison_model_not_bpf_execution(self):
        text = Path(module.__file__).with_name('observe.bpf.c').read_text()
        fields = re.findall(r'READ_MEMBER\((\w+), (\d+)\);', text)
        layout = [('send_ns', 0, 'Q'), ('recv_ns', 8, 'Q'), ('method', 16, 'H'), ('failed', 18, 'B'),
                  ('controller_error', 20, 'i'), ('ds_error', 24, 'i'), ('server_processing_ns', 32, 'Q'),
                  ('server_execution_ns', 40, 'Q'), ('processing_state', 48, 'B'), ('execution_state', 49, 'B')]
        self.assertEqual(fields, [(name, str(offset)) for name, offset, fmt in layout])
        self.assertEqual(sum(struct.calcsize('<' + fmt) for name, offset, fmt in layout), 45)
        self.assertIn('struct Observation event = {};', text)
        self.assertIn('bpf_probe_read_user(&event.field, sizeof(event.field), source + offset)', text)
        source = bytearray(b'\xa5' * 56)
        expected = dict(event(), controller_error=-123, ds_error=-456)
        for name, offset, fmt in layout:
            struct.pack_into('<' + fmt, source, offset, expected[name])
        decoded = dict(expected)
        for name, offset, fmt in layout:
            decoded[name] = struct.unpack_from('<' + fmt, source, offset)[0]
        self.assertEqual(decode_payload(payload(decoded)), expected)
        self.assertEqual(payload(decoded)[65:], b'\0' * 7)
        self.assertEqual(source[19], 0xa5)
        self.assertEqual(source[28:32], b'\xa5' * 4)
        self.assertEqual(source[50:], b'\xa5' * 6)


if __name__ == '__main__':
    unittest.main()
