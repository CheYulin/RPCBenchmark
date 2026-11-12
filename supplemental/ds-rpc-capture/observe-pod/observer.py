"""Bounded own-child observer library. No SDK/cohort runner or signal API."""
import ctypes as ct
import hashlib
import json
import os
from pathlib import Path
import struct
import time

from contracts import EVENT_BYTES, counters_check, decode_payload

COUNTERS = ('allowed_seen', 'read_failed', 'submit_succeeded', 'submit_failed')
MAX_RECORDS = 250000
MAX_RAW_BYTES = 128 * 1024 * 1024
MAX_PERF_FDS = 128
PAGES = 16


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def task(pid, tid=None):
    tid = pid if tid is None else tid
    path = Path('/proc') / str(pid) / 'task' / str(tid)
    fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    status = dict(x.split(':', 1) for x in (path / 'status').read_text().splitlines() if ':' in x)
    if int(status['Tgid']) != pid or int(status['Pid']) != tid:
        raise RuntimeError('task is not in the expected TGID')
    return dict(pid=pid, tid=tid, startticks=int(fields[19]), ppid=int(status['PPid']))


def owned_descendant(pid, owner):
    chain = []
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        current = task(pid)
        chain.append(current)
        if pid == owner['pid']:
            if current != owner:
                raise RuntimeError('own ancestor identity changed')
            return chain
        pid = current['ppid']
    raise RuntimeError('process does not descend from the explicit controller')


def perf_fds():
    result = {}
    for path in Path('/proc/self/fd').iterdir():
        try:
            if 'perf_event' in os.readlink(path):
                result[int(path.name)] = (Path('/proc/self/fdinfo') / path.name).read_text()
        except FileNotFoundError:
            pass
    return result


def online_cpus():
    text = Path('/sys/devices/system/cpu/online').read_text().strip()
    result = []
    for part in text.split(','):
        edges = list(map(int, part.split('-')))
        result.extend(range(edges[0], edges[-1] + 1))
    if not result or len(set(result)) != len(result):
        raise RuntimeError('invalid online CPU set')
    return text, result


def elf_entry(path, value, size, offset, prologue_sha):
    with Path(path).open('rb') as stream:
        header = struct.unpack('<16sHHIQQQIHHHHHH', stream.read(64))
        if header[0][:6] != b'\x7fELF\x02\x01' or header[1:3] != (3, 62) or header[9] != 56:
            raise RuntimeError('not frozen ELF64 AMD64 DYN')
        stream.seek(header[5])
        segments = [struct.unpack('<IIQQQQQQ', stream.read(56)) for _ in range(header[10])]
        matches = [p for p in segments if p[0] == 1 and p[1] & 1 and p[3] <= value and value + size <= p[3] + p[5]]
        if len(matches) != 1 or matches[0][2] + value - matches[0][3] != offset:
            raise RuntimeError('normal entry not in unique executable PT_LOAD')
        stream.seek(offset)
        if hashlib.sha256(stream.read(64)).hexdigest() != prologue_sha:
            raise RuntimeError('normal entry bytes differ from frozen proof')


class PodObserver:
    def __init__(self, output, image, callback_bytes, controller_pid, controller_startticks):
        if callback_bytes not in (EVENT_BYTES, EVENT_BYTES + 4):
            raise ValueError('native callback size requires an exact fixture proof')
        self.output = Path(output)
        self.output.mkdir(exist_ok=False)
        self.image = dict(image)
        self.callback_bytes = callback_bytes
        self.bpf = None
        self.event_name = None
        self.target = None
        self.callers = {}
        self.closed = False
        self.failure = None
        self.received = self.decoded = self.raw_bytes = self.lost = self.records = 0
        self.sizes = {}
        self.lost_by_cpu = {}
        self.observer_owner = task(os.getpid())
        if controller_pid <= 1:
            raise RuntimeError('explicit non-init controller required')
        self.controller = task(controller_pid)
        if self.controller['startticks'] != controller_startticks:
            raise RuntimeError('controller startticks mismatch')
        owned_descendant(os.getpid(), self.controller)
        self.initial_fds = perf_fds()
        self.raw = (self.output / 'raw-events.bin').open('wb')
        self.record = dict(
            status='CREATED_NOT_ATTACHED', source='BPF_OBSERVE_ENTRY_ALL_RPC',
            SDK_accepted=False, full_coverage=False, performance_or_RPC30=False,
            native_callback_bytes_expected=callback_bytes,
            native_framing_fixture_runtime_NOT_PROVEN=True,
            maximum_records=MAX_RECORDS, maximum_raw_bytes=MAX_RAW_BYTES,
            controller=self.controller, reader=self.observer_owner,
            signals_sent=False, no_strings_pointers_payload=True, errors=[],
        )

    def fail(self, reason):
        if reason not in self.record['errors']:
            self.record['errors'].append(reason)
        self.failure = self.failure or reason

    def persist(self):
        self.record.update(received_callbacks=self.received, decoded_records=self.decoded,
                           stored_records=self.records, raw_event_bytes=self.raw_bytes,
                           lost_callbacks=self.lost, lost_callbacks_by_cpu=self.lost_by_cpu, callback_sizes=self.sizes)
        (self.output / 'observer.json').write_text(json.dumps(self.record, indent=2) + '\n')

    def own(self, require_controller=True):
        current = task(os.getpid())
        if any(current[key] != self.observer_owner[key] for key in ('pid', 'tid', 'startticks')):
            raise RuntimeError('observer used by a different process/fork')
        if require_controller:
            owned_descendant(os.getpid(), self.controller)

    def target_identity(self):
        if task(self.target['pid']) != self.target:
            raise RuntimeError('SDK PID identity changed; never rebound')
        chain = owned_descendant(self.target['pid'], self.controller)
        for tid, original in self.callers.items():
            if task(self.target['pid'], tid) != original:
                raise RuntimeError('SDK caller identity changed')
        return chain

    def maps_identity(self):
        path = Path(self.image['path']).resolve()
        stat = path.stat()
        rows = []
        zeros = 0
        raw = Path('/proc', str(self.target['pid']), 'maps').read_text()
        for line in raw.splitlines():
            parts = line.split(None, 5)
            if len(parts) != 6 or not parts[5].startswith('/'):
                continue
            removed = parts[5].endswith(' (deleted)')
            other = Path(parts[5].removesuffix(' (deleted)'))
            sdk = other.name == 'libdatasystem.so' or other.name.startswith('libdatasystem.so.')
            same = other.resolve() == path
            if sdk and not same:
                raise RuntimeError('another SDK image mapped')
            if same:
                device = tuple(int(x, 16) for x in parts[3].split(':'))
                if removed or int(parts[4]) != stat.st_ino or device != (os.major(stat.st_dev), os.minor(stat.st_dev)):
                    raise RuntimeError('SDK mapped identity differs')
                rows.append(line)
                zeros += int(parts[2], 16) == 0
        if not rows or zeros != 1:
            raise RuntimeError('exact single SDK image not observed')
        return dict(path=str(path), dev=stat.st_dev, inode=stat.st_ino, maps=rows,
                    sha256=sha(path), all_maps=raw)

    def prepare(self, bpf_factory=None):
        self.own()
        if self.bpf is not None or self.closed:
            raise RuntimeError('observer already prepared/closed')
        try:
            if sha(self.image['path']) != self.image['sha256']:
                raise RuntimeError('frozen image hash differs')
            elf_entry(self.image['path'], self.image['value'], self.image['size'],
                      self.image['offset'], self.image['prologue64_sha256'])
            online, cpus = online_cpus()
            if len(cpus) + 1 > MAX_PERF_FDS:
                raise RuntimeError('online CPUs exceed hard observer FD bound')
            if bpf_factory is None:
                import bcc
                if bcc.__version__ != '0.35.0':
                    raise RuntimeError('requires pinned BCC0.35.0')
                bpf_factory = bcc.BPF
            self.bpf = bpf_factory(text=Path(__file__).with_name('observe.bpf.c').read_text())
            self.bpf['events'].open_perf_buffer(self._event, page_cnt=PAGES, lost_cb=self._lost)
            added = {n: v for n, v in perf_fds().items() if n not in self.initial_fds}
            if len(added) != len(cpus) or len(added) > MAX_PERF_FDS:
                raise RuntimeError('perf-output FDs not explained by online CPUs')
            self.record.update(status='PRECOMPILED_TARGET_DISABLED', online=online,
                               online_count=len(cpus), page_size=os.sysconf('SC_PAGE_SIZE'),
                               perf_output_pages_per_CPU=PAGES,
                               buffer_bytes_including_metadata=(PAGES + 1) * os.sysconf('SC_PAGE_SIZE') * len(cpus),
                               perf_FDs_pre_attach=added)
            self.persist()
        except BaseException as error:
            self.fail(repr(error))
            self.finish(partial=True)
            raise

    def bind(self, pid, startticks, callers, warm_epoch_ns):
        self.own()
        try:
            if self.bpf is None or self.target is not None or pid <= 0 or len(callers) != 32 or len(set(callers)) != 32:
                raise RuntimeError('requires prepared observer and owned32 SDK callers')
            actual = task(pid)
            if actual['startticks'] != startticks:
                raise RuntimeError('SDK startticks mismatch')
            self.target = actual
            self.callers = {tid: task(pid, tid) for tid in callers}
            self.record['target_ancestry'] = self.target_identity()
            image = self.maps_identity()
            if image['sha256'] != self.image['sha256']:
                raise RuntimeError('SDK file changed')
            (self.output / 'target-maps-before.txt').write_text(image.pop('all_maps'))
            self.record['target_image_before'] = image
            previous = set(self.bpf.uprobe_fds)
            if time.monotonic_ns() >= warm_epoch_ns:
                raise RuntimeError('no epoch extension for late bind')
            for tid in self.callers:
                self.bpf['callers'][ct.c_uint(tid)] = ct.c_ubyte(1)
            self.bpf.attach_uprobe(name=self.image['path'], addr=self.image['value'], fn_name='observe_entry', pid=pid)
            registered = set(self.bpf.uprobe_fds) - previous
            if len(registered) != 1:
                raise RuntimeError('one positive-PID uprobe registration required')
            self.event_name = registered.pop()
            self.target_identity()
            after = self.maps_identity()
            if (after['sha256'], after['dev'], after['inode']) != (image['sha256'], image['dev'], image['inode']):
                raise RuntimeError('SDK image changed while attaching')
            (self.output / 'target-maps-after.txt').write_text(after.pop('all_maps'))
            self.record['target_image_after'] = after
            added = {n: v for n, v in perf_fds().items() if n not in self.initial_fds}
            if len(added) != self.record['online_count'] + 1 or len(added) > MAX_PERF_FDS:
                raise RuntimeError('unexpected perf FDs including target attach')
            self.record['perf_FDs_with_attach'] = added
            if time.monotonic_ns() >= warm_epoch_ns:
                raise RuntimeError('attach setup missed original warm epoch')
            self.bpf['target'][ct.c_int(0)] = ct.c_uint(pid)
            self.target_identity()
            ready = time.monotonic_ns()
            if ready >= warm_epoch_ns:
                raise RuntimeError('READY not strictly before warm epoch')
            self.record.update(status='READY', ready_ns=ready, target=self.target,
                               callers=list(self.callers.values()), positive_attach_pid=pid,
                               uprobe_event_name=self.event_name.decode(), warm_epoch_ns=warm_epoch_ns)
            self.persist()
            return ready
        except BaseException as error:
            self.fail(repr(error))
            self.finish(partial=True)
            raise

    def _lost(self, cpu, count):
        self.lost += count
        self.lost_by_cpu[cpu] = self.lost_by_cpu.get(cpu, 0) + count
        if count:
            self.fail('lost perf-output callbacks')

    def _event(self, cpu, data, size):
        self.received += 1
        self.sizes[size] = self.sizes.get(size, 0) + 1
        if size != self.callback_bytes:
            self.fail('unexpected native callback size')
            return
        if self.records >= MAX_RECORDS or self.raw_bytes + EVENT_BYTES > MAX_RAW_BYTES:
            self.fail('raw observation hard bound reached')
            return
        try:
            raw = ct.string_at(data, EVENT_BYTES)
            self.raw.write(raw)
            self.raw_bytes += len(raw)
            self.records += 1
            event = decode_payload(raw)
            if self.target is None or event['tgid'] != self.target['pid'] or event['tid'] not in self.callers or event['cpu'] != cpu:
                raise ValueError('unexpected emitted owner/TID/CPU')
            self.decoded += 1
        except BaseException as error:
            self.fail(repr(error))

    def poll(self, timeout_ms=50):
        self.own()
        if self.closed or self.bpf is None:
            raise RuntimeError('observer closed/unprepared')
        if self.failure:
            raise RuntimeError(self.failure)
        self.bpf.perf_buffer_poll(timeout=timeout_ms)
        for index in (1, 3):
            if self.bpf['counts'].sum(ct.c_int(index)).value:
                self.fail(COUNTERS[index] + ' during capture')
        if self.failure:
            raise RuntimeError(self.failure)

    def finish(self, partial=False):
        self.own(require_controller=False)
        if self.closed:
            return self.record
        self.record['stop_requested_ns'] = time.monotonic_ns()
        detached = self.event_name is None
        try:
            if self.bpf is not None:
                if self.event_name is not None:
                    self.bpf.detach_uprobe_event(self.event_name)
                    self.event_name = None
                    detached = True
                    self.record['detached_ns'] = time.monotonic_ns()
                # Detach first; keep output/maps alive through bounded drain and counters.
                deadline = time.monotonic() + 2
                before = -1
                while time.monotonic() < deadline:
                    current = self.received
                    self.bpf.perf_buffer_poll(timeout=10)
                    if self.received == current == before:
                        break
                    before = self.received
                else:
                    self.fail('drain bound exceeded')
                counters = {name: int(self.bpf['counts'].sum(ct.c_int(i)).value) for i, name in enumerate(COUNTERS)}
                self.record.update(counters=counters,
                                   transport_check=counters_check(counters, self.decoded, self.lost),
                                   drain_finished_ns=time.monotonic_ns())
        except BaseException as error:
            self.fail(repr(error))
        finally:
            if self.bpf is not None:
                try:
                    self.bpf.cleanup()
                    self.bpf = None
                except BaseException as error:
                    self.fail('BPF cleanup: ' + repr(error))
            try:
                remaining = {n: v for n, v in perf_fds().items() if n not in self.initial_fds}
                self.record['remaining_perf_FDs'] = remaining
                if remaining:
                    self.fail('observer perf FDs remain')
            except BaseException as error:
                self.fail('FD audit: ' + repr(error))
            try:
                self.raw.flush()
            except BaseException as error:
                self.fail('raw flush: ' + repr(error))
            try:
                self.raw.close()
            except BaseException as error:
                self.fail('raw close: ' + repr(error))
            self.closed = True
            self.record.update(status='CLOSED_PARTIAL' if partial or self.failure else 'CLOSED_REQUIRES_OFFLINE_CROSSCHECK',
                               partial_target_exit=partial, error=self.failure,
                               explicit_detach_before_drain=detached,
                               original_assess_and_prefix_crosscheck_NOT_RUN=True)
            self.persist()
        return self.record
