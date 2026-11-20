"""Owned SDK8 full sched/Get trace. Plan mode never imports BCC or loads BPF."""
import argparse
import ctypes as ct
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import resource
import select
import signal
import struct
import subprocess
import sys
import time

TASKS, MAP_LIMIT, RSS_LIMIT = 8, 20 * 1024 * 1024, 256 * 1024 * 1024
CPU_SLOTS, OWNER_CAPACITY = 256, 65536
EVENT_FORMAT = "<QQIIHHI"
EVENT_BYTES = struct.calcsize(EVENT_FORMAT)
DRIVER = '6b13a98d4e721c5546e6dea6e3177484ee7f0be5aa64922900b636584ac7d069'
SOURCE = '50efed0f5386508cf5bcf4c3147905fb8472bbe0a4293f7d97b240927954a197'
_stop_requested = False
MAPS = ('allowed','births','gates','requested_mode','control','deadline','seal_bounds','bad','seen','next_record','get_next','get_pending','records')
GET_OBJECT_SHA = '56d6a4dceb98a62128900147e0d57ade5fa3105551a603117340ce6b1cc31791'
GET_SYMBOL = b'_ZN10datasystem8KVClient3GetERKNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEERS6_'
TARGET_KIND = 'SDK8_FULL_SCHED_GET_DIAGNOSTIC'
PROFILE = {'protocol': 'fixed-owner8-bounded-admission-v1', 'owner_period_ns': 16000000, 'hard_dispatch_lateness_ns_max_inclusive': 15999999, 'soft_late_threshold_ns_exclusive': 1000000, 'soft_late_budget_numerator': 1, 'soft_late_budget_denominator': 1000, 'pacing_policy': 'early_wake_then_spin_v1', 'early_wake_ns': 1000000, 'no_debt_policy': 'observer_end_strictly_before_scheduled_plus_owner_period_including_last'}


def need(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def validate_inputs(receipt, allowlist):
    need(receipt.get('schema') == 'OWNED_SDK8_COLD_READY_FULL_SCHED_GET_V1', 'receipt schema')
    need(receipt.get('root_authorized') is True and receipt.get('cold_GO_held') is True, 'owned cold GO authority')
    need(receipt.get('purpose') == 'FULL_CALLER_SCHED_AND_GET_ATTRIBUTION_ONLY' and receipt.get('driver_source_sha256') == SOURCE, 'source/purpose')
    need(receipt.get('target_kind') == TARGET_KIND, 'target kind')
    need(receipt.get('object_sha256') == GET_OBJECT_SHA and receipt.get('symbol') == GET_SYMBOL.decode(), 'exact Get object/symbol')
    need(type(receipt.get('clock_alignment_verified')) is bool, 'explicit clock verification state')
    owner = receipt['sdk']
    need(owner.get('exe_sha256') == DRIVER and all(type(owner.get(k)) is int and owner[k] > 0
         for k in ('pid', 'pgrp', 'session', 'startticks')), 'owned SDK identity')
    need(isinstance(allowlist, list) and len(allowlist) == TASKS, 'exactly8 caller entries')
    tids = set()
    for row in allowlist:
        need(isinstance(row, dict) and set(row) == {'tid', 'tgid', 'startticks'}, 'allowlist fields')
        need(all(type(row[k]) is int and row[k] > 0 for k in row), 'allowlist integer identity')
        need(row['tgid'] == owner['pid'] and row['tid'] not in tids, 'allowlist owner/duplicate')
        tids.add(row['tid'])
    need(receipt.get('kernel_release') == '5.14.0-514.el9.x86_64', 'unqualified kernel release')
    need(re.fullmatch('[0-9a-f]{64}', receipt.get('kernel_btf_sha256', '')) is not None, 'BTF source binding')
    return owner


def proc_stat(path):
    values = Path(path).read_text().rsplit(')', 1)[1].split()
    return dict(state=values[0], pgrp=int(values[2]), session=int(values[3]), startticks=int(values[19]))


def check_live(receipt, allowlist):
    owner = receipt['sdk']; root = Path('/proc') / str(owner['pid'])
    actual = proc_stat(root / 'stat')
    need(actual['state'] not in ('Z', 'X') and all(actual[k] == owner[k] for k in ('pgrp', 'session', 'startticks')), 'SDK exited/changed')
    for row in allowlist:
        value = proc_stat(root / 'task' / str(row['tid']) / 'stat')
        need(value['state'] not in ('Z', 'X') and value['startticks'] == row['startticks'], 'caller exited/reused')
        need(os.sched_getaffinity(row['tid']) == {1,3,5,7,9,11,13,15} and os.getpriority(os.PRIO_PROCESS, row['tid']) == 0, 'caller role changed')
        need(os.sched_getscheduler(row['tid']) == os.SCHED_OTHER and os.sched_getparam(row['tid']).sched_priority == 0, 'caller policy changed')
    for key, kind in (('netns', 'net'), ('mntns', 'mnt')):
        need(os.readlink(root / 'ns' / kind) == owner[key], 'SDK namespace changed')
    need(digest(root / 'cgroup') == owner['cgroup_sha256'], 'SDK cgroup changed')


def btf_offsets(text):
    records = {}
    matches = list(re.finditer(r'^\[(\d+)\] (.*)$', text, re.M))
    for i, match in enumerate(matches):
        records[int(match[1])] = (match[2], text[match.end():matches[i+1].start() if i+1 < len(matches) else len(text)])
    def named(kind, name):
        found = [i for i, (h, _) in records.items() if h.startswith(kind + " '" + name + "'")]
        need(len(found) == 1, 'unknown/ambiguous BTF ' + name)
        return found[0]
    def base(i):
        for _ in range(12):
            head, body = records[i]
            if head.startswith(('TYPEDEF ', 'CONST ', 'VOLATILE ', 'RESTRICT ', 'TYPE_TAG ')):
                i = int(re.search(r'type_id=(\d+)', head)[1])
            else:
                return i, head, body
        raise ValueError('BTF type cycle')
    task = named('STRUCT', 'task_struct')
    fields = {}
    for name, kind, width in [('pid','INT',4),('tgid','INT',4),('__state','INT',4),('start_boottime','INT',8),('group_leader','PTR',8)]:
        values = re.findall(r"'" + name + r"' type_id=(\d+) bits_offset=(\d+)\s*$", records[task][1], re.M)
        need(len(values) == 1, 'missing BTF task field ' + name)
        tid, offset = map(int, values[0]); _, head, _ = base(tid)
        need(offset % 8 == 0 and head.startswith(kind + ' '), 'BTF task field kind/offset ' + name)
        if kind == 'INT':
            need(re.search(r'size=' + str(width) + r'\b', head) is not None, 'BTF task field width ' + name)
        else:
            need(int(re.search(r'type_id=(\d+)', head)[1]) == task, 'BTF leader pointer')
        fields[name] = offset // 8
    def pointer(i, target):
        _, h, _ = base(i)
        return h.startswith('PTR ') and int(re.search(r'type_id=(\d+)', h)[1]) == target
    for name, count in [('sched_wakeup', 2), ('sched_wakeup_new', 2), ('sched_switch', 4)]:
        _, head, _ = base(named('TYPEDEF', 'btf_trace_' + name))
        need(head.startswith('PTR '), 'BTF trace prototype pointer')
        _, proto, body = base(int(re.search(r'type_id=(\d+)', head)[1]))
        need(proto.startswith('FUNC_PROTO '), 'BTF trace function prototype')
        args = [int(v) for v in re.findall(r"^\s*'[^']*' type_id=(\d+)", body, re.M)]
        need(len(args) in ((4, 5) if name == 'sched_switch' else (count,)), 'unknown raw trace arity')
        need(pointer(args[0], 0) and pointer(args[-1] if name != 'sched_switch' else args[2], task), 'BTF trace task pointer')
        if name == 'sched_switch':
            need(pointer(args[3], task), 'BTF switch next pointer')
            _, boolean, _ = base(args[1]); need(boolean.startswith('INT ') and 'size=1' in boolean, 'BTF preempt bool')
            if len(args)==5:
                _, state, _=base(args[4]); need(state.startswith('INT ') and 'size=4' in state,'unknown switch prev_state type')
    return fields


def render_program(source, offsets):
    for token, name in {'PID':'pid','TGID':'tgid','STATE':'__state','START':'start_boottime','LEADER':'group_leader'}.items():
        need(type(offsets[name]) is int and 0 <= offsets[name] < 65536, 'invalid BTF offset')
        source = source.replace('@' + token + '@', str(offsets[name]))
    need('@' not in source and 'RAW_TRACEPOINT_PROBE' not in source, 'source/autoload macro rejected')
    return source


def perf_attr():
    data = bytearray(128)
    struct.pack_into('<IIQQ', data, 0, 1, 128, 3, 1)
    struct.pack_into('<Q', data, 40, 1)  # disabled until native SET_BPF/ENABLE; inherit/exclude_kernel/freq stay zero.
    return bytes(data)


def rss():
    for line in Path('/proc/self/status').read_text().splitlines():
        if line.startswith('VmRSS:'):
            return int(line.split()[1]) * 1024
    raise ValueError('observer RSS unavailable')


def close_owned(perf_fds, raw_fds, close_perf, close_raw):
    errors = []
    while perf_fds:
        tid, fd = perf_fds.pop()
        try:
            need(close_perf(fd) == 0, 'perf close returned error')
        except BaseException as exception:
            errors.append('perf close ' + str(tid) + ' ' + repr(exception))
    while raw_fds:
        event, fd = raw_fds.pop()
        try:
            close_raw(fd)
        except BaseException as exception:
            errors.append('raw close ' + event + ' ' + repr(exception))
    return errors


def close_uprobes(links, close_fd, detach_event):
    errors = []
    while links:
        link = links.pop()
        for label, function, value in [('uprobe fd', close_fd, link['fd']), ('uprobe event', detach_event, link['event'])]:
            try:
                need(function(value) == 0, label + ' close returned error')
            except BaseException as error:
                errors.append(label + ' ' + repr(error))
    return errors


class TestRunAttr(ct.Structure):
    _fields_ = [('prog_fd', ct.c_uint32), ('retval', ct.c_uint32), ('data_size_in', ct.c_uint32),
        ('data_size_out', ct.c_uint32), ('data_in', ct.c_uint64), ('data_out', ct.c_uint64),
        ('repeat', ct.c_uint32), ('duration', ct.c_uint32), ('ctx_size_in', ct.c_uint32),
        ('ctx_size_out', ct.c_uint32), ('ctx_in', ct.c_uint64), ('ctx_out', ct.c_uint64),
        ('flags', ct.c_uint32), ('cpu', ct.c_uint32)]


def test_run_control(fd, invoke=None):
    need(type(fd) is int and fd >= 0, 'control program fd')
    packet, output = ct.create_string_buffer(64), ct.create_string_buffer(64)
    attr = TestRunAttr(prog_fd=fd, data_size_in=64, data_size_out=64,
                       data_in=ct.addressof(packet), data_out=ct.addressof(output), repeat=1)
    if invoke is None:
        need(platform.machine() == 'x86_64' and platform.system() == 'Linux', 'test_run qualified architecture')
        libc = ct.CDLL(None, use_errno=True)
        libc.syscall.restype = ct.c_long
        result = libc.syscall(ct.c_long(321), ct.c_int(10), ct.byref(attr), ct.c_size_t(ct.sizeof(attr)))
        if result:
            raise OSError(ct.get_errno(), 'BPF_PROG_TEST_RUN control')
    else:
        need(invoke(attr) == 0, 'test_run failure')
    return int(attr.retval)


def uprobe_plan(sdk_leader_pid, object_path):
    need(type(sdk_leader_pid) is int and sdk_leader_pid > 0, 'positive owned SDK leader PID')
    return [dict(attach_pid=sdk_leader_pid, return_probe=ret, symbol=GET_SYMBOL, object_path=object_path,
                 function=b'on_get_return' if ret else b'on_get_entry')
            for ret in (0, 1)]


def cookie_snapshot(allowlist, bound):
    need(len(allowlist) == len(bound) == TASKS, 'exact8 cookie snapshot')
    rows = [dict(slot=i, tid=row['tid'], tgid=row['tgid'], task_start_boottime_ns=int(value.task),
                 leader_start_boottime_ns=int(value.leader)) for i, (row, value) in enumerate(zip(allowlist, bound))]
    need(all(row['task_start_boottime_ns'] > 0 and row['leader_start_boottime_ns'] > 0 for row in rows), 'missing cookie')
    need(len({row['leader_start_boottime_ns'] for row in rows}) == 1, 'leader cookie mismatch')
    need(len({(row['tid'], row['task_start_boottime_ns'], row['leader_start_boottime_ns']) for row in rows}) == TASKS, 'duplicate identity')
    return rows


def preflight(args, receipt, allowlist):
    owner = validate_inputs(receipt, allowlist)
    need(platform.release() == receipt['kernel_release'] and sys.byteorder == 'little', 'kernel/endianness')
    need(os.geteuid() == 0 and os.sched_getaffinity(0) == {35} and os.getpriority(os.PRIO_PROCESS, 0) == 10, 'observer role')
    need(os.sched_getscheduler(0) == os.SCHED_OTHER and os.sched_getparam(0).sched_priority == 0, 'observer policy')
    need(digest('/sys/kernel/btf/vmlinux') == receipt['kernel_btf_sha256'], 'kernel BTF changed')
    need(digest('/proc/' + str(owner['pid']) + '/exe') == DRIVER, 'SDK executable changed')
    need(digest(receipt['ready_path']) == receipt['ready_sha256'], 'cold READY changed')
    ready = json.loads(Path(receipt['ready_path']).read_text())
    need(ready['pid'] == owner['pid'] and ready['tids'] == [row['tid'] for row in allowlist]
         and ready['callers'] == TASKS and ready['rate_per_second'] == 500, 'cold READY8 order')
    profile = ready.get('admission_profile')
    need(type(profile) is dict and profile.keys() == PROFILE.keys() and all(type(profile[k]) is type(v) and profile[k] == v for k, v in PROFILE.items()), 'typed owner8 profile')
    object_path = Path(receipt['object_path'])
    need(object_path.is_absolute() and object_path.is_file() and digest(object_path) == GET_OBJECT_SHA, 'Get object binding')
    stat = object_path.stat()
    matches = []
    for line in Path('/proc/' + str(owner['pid']) + '/maps').read_text().splitlines():
        fields = line.split(None, 5)
        if len(fields) >= 5 and int(fields[4]) == stat.st_ino:
            major, minor = [int(value, 16) for value in fields[3].split(':')]
            if (major, minor) == (os.major(stat.st_dev), os.minor(stat.st_dev)):
                matches.append(line)
    need(bool(matches), 'Get object is not mapped in target')
    check_live(receipt, allowlist)
    possible = Path('/sys/devices/system/cpu/possible').read_text().strip()
    cpus = set()
    for part in possible.split(','):
        ends = [int(value) for value in part.split('-')]
        cpus.update(range(ends[0], ends[-1] + 1))
    need(cpus and min(cpus) == 0 and max(cpus) < CPU_SLOTS, 'all possible CPU gates')
    metadata = subprocess.run(['bpftool', 'btf', 'dump', 'file', '/sys/kernel/btf/vmlinux', 'format', 'raw'], check=True, capture_output=True, text=True).stdout
    offsets = btf_offsets(metadata)
    del metadata
    gc.collect()
    formats = {}
    for event in ('sched_wakeup', 'sched_wakeup_new', 'sched_switch'):
        text = (Path('/sys/kernel/tracing/events/sched') / event / 'format').read_text()
        need('name: ' + event in text, 'tracepoint unavailable')
        formats[event] = hashlib.sha256(text.encode()).hexdigest()
    from bcc import BPF
    import bcc
    from bcc.libbcc import lib
    need(getattr(bcc, '__version__', None) == '0.35.0', 'BCC version')
    need(digest(bcc.__file__) == 'd828a02fd8761c4da759d10900beffdee3131d7be1f9bdeec12ad0947d4d0c3a', 'BCC Python source changed')
    need(digest('/usr/lib64/libbcc.so.0.35.0') == 'b36944e40964b829fe652fbbcbcc189d3aa294f3ab40752bebfd031504d41e36', 'libbcc changed')
    need(os.readlink('/proc/self/ns/time') == os.readlink('/proc/1/ns/time') == os.readlink('/proc/' + str(owner['pid']) + '/ns/time'), 'time namespace')
    lib.bpf_attach_perf_event_raw.restype = ct.c_int
    lib.bpf_attach_perf_event_raw.argtypes = [ct.c_int, ct.c_void_p, ct.c_int, ct.c_int, ct.c_int, ct.c_uint]
    lib.bpf_close_perf_event_fd.restype = ct.c_int
    lib.bpf_close_perf_event_fd.argtypes = [ct.c_int]
    lib.bpf_attach_raw_tracepoint.restype = ct.c_int
    lib.bpf_attach_raw_tracepoint.argtypes = [ct.c_int, ct.c_char_p]
    lib.bpf_attach_uprobe.restype = ct.c_int
    lib.bpf_attach_uprobe.argtypes = [ct.c_int, ct.c_int, ct.c_char_p, ct.c_char_p, ct.c_ulonglong, ct.c_int]
    lib.bpf_detach_uprobe.restype = ct.c_int
    lib.bpf_detach_uprobe.argtypes = [ct.c_char_p]
    source_path = Path(__file__).with_suffix('.bpf.c')
    return owner, offsets, formats, source_path, render_program(source_path.read_text(), offsets), BPF, lib, possible


class TargetEnded(Exception):
    pass


def execute(args, receipt, allowlist):
    try:
        owner, offsets, formats, source_path, source, BPF, lib, possible = preflight(args, receipt, allowlist)
    except BaseException as error:
        return dict(status='REJECT_PREFLIGHT', error=repr(error), detached=True, all_bootstrap_fds_closed=True,
                    gate_closed_drained=False, no_resources_created=True, trusted_observed_prefix=False, RPC30_accepted=False)
    b = None
    pidfd = None
    perf_fds, raw_fds, uprobe_links, errors, calls = [], [], [], [], []
    program_records, map_records, cookie_rows = [], [], []
    result, snapshot = {}, None
    enabled_begin = enabled_end = disabled_begin = disabled_end = None
    sdk_go = cookies_bound = data_deadline = None
    mode_requested = 0
    gate_drained = False
    status, failure = 'REJECT_DIAGNOSTIC', None
    peak = 0

    def alive():
        need(not _stop_requested, 'owned stop requested')
        if select.select([pidfd], [], [], 0)[0]:
            raise TargetEnded('SDK leader exited')
        try:
            check_live(receipt, allowlist)
        except (FileNotFoundError, ProcessLookupError):
            raise TargetEnded('native caller exited')

    def command(timeout):
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        line = sys.stdin.readline()
        need(bool(line), 'controller stdin closed')
        return line.strip()

    def control(name):
        begin = time.monotonic_ns()
        value = test_run_control(b.load_func(name.encode(), BPF.SOCKET_FILTER).fd)
        calls.append(dict(program=name, begin_ns=begin, end_ns=time.monotonic_ns(), retval=value, skb_bytes=64, no_network=True))
        need(value in (0, 1), 'cold gate fault or partial-open failure')
        return value

    def open_phase(selected):
        nonlocal mode_requested
        mode_requested = selected
        b['requested_mode'][ct.c_int(0)] = ct.c_uint(selected)
        need(control('open_gate') == 0, 'gate open busy')

    def seal_first():
        nonlocal disabled_begin, disabled_end
        begin = time.monotonic_ns()
        value = control('seal_gate')
        end = time.monotonic_ns()
        if mode_requested == 2 and disabled_begin is None:
            disabled_begin, disabled_end = begin, end
        return value

    def drain(value):
        deadline = time.monotonic() + 2
        while value:
            need(time.monotonic() < deadline, 'authorised writer drain timeout')
            time.sleep(.001)
            value = control('seal_gate')

    def close_perf():
        errors.extend(close_owned(perf_fds, [], lib.bpf_close_perf_event_fd, os.close))

    try:
        pidfd = os.pidfd_open(owner['pid'], 0)
        alive()
        b = BPF(text=source)
        functions = [(b'bootstrap', BPF.PERF_EVENT), (b'on_wakeup', BPF.RAW_TRACEPOINT),
            (b'on_wakeup_new', BPF.RAW_TRACEPOINT), (b'on_switch', BPF.RAW_TRACEPOINT),
            (b'on_get_entry', BPF.KPROBE), (b'on_get_return', BPF.KPROBE),
            (b'open_gate', BPF.SOCKET_FILTER), (b'seal_gate', BPF.SOCKET_FILTER)]
        for name, kind in functions:
            function = b.load_func(name, kind)
            info = dict(line.split(':', 1) for line in Path('/proc/self/fdinfo/' + str(function.fd)).read_text().splitlines() if ':' in line)
            need('prog_id' in info, 'program identity unavailable')
            program_records.append(dict(name=name.decode(), program_id=int(info['prog_id'].strip())))
        map_bytes = 0
        for name in MAPS:
            fd = b[name].get_fd()
            info = dict(line.split(':', 1) for line in Path('/proc/self/fdinfo/' + str(fd)).read_text().splitlines() if ':' in line)
            need('memlock' in info and 'map_id' in info, 'map identity/accounting')
            size = int(info['memlock'].strip())
            map_bytes += size
            map_records.append(dict(name=name, map_id=int(info['map_id'].strip()), memlock_bytes=size))
        need(map_bytes <= MAP_LIMIT and len({row['map_id'] for row in map_records}) == len(MAPS), 'map budget/identity')
        need(rss() <= RSS_LIMIT, 'retained RSS budget')
        for slot, row in enumerate(allowlist):
            b['allowed'][ct.c_uint(row['tid'])] = b['allowed'].Leaf(row['tgid'], slot)
        open_phase(1)
        for row in allowlist:
            attr = ct.create_string_buffer(perf_attr())
            fd = lib.bpf_attach_perf_event_raw(b.load_func(b'bootstrap', BPF.PERF_EVENT).fd, ct.byref(attr), row['tid'], -1, -1, 0)
            need(fd >= 0, 'task-bound bootstrap attach')
            perf_fds.append((row['tid'], fd))
            os.set_inheritable(fd, False)
        print('READY_BOOTSTRAP_FDS ' + json.dumps(dict(ready_count=TASKS, cold_GO_held=True, raw_attached=False)), flush=True)
        until = time.monotonic() + 15
        while True:
            alive()
            line = command(.01)
            if line is not None:
                parts = line.split(' ')
                need(len(parts) == 2 and parts[0] == 'GO' and parts[1].isdecimal(), 'GO requires SDK send monotonic epoch')
                sdk_go = int(parts[1])
                need(0 < sdk_go <= time.monotonic_ns(), 'SDK GO clock')
                break
            need(time.monotonic() < until, 'GO timeout')
        bootstrap_deadline = sdk_go + 1_000_000_000
        b['deadline'][ct.c_int(0)] = ct.c_ulonglong(bootstrap_deadline)
        while True:
            alive()
            need(time.monotonic_ns() <= bootstrap_deadline, 'bootstrap cookie deadline')
            need(not any(int(value.value) for _, value in b['bad'].items()), 'bootstrap guard')
            bound = [b['births'][ct.c_int(index)] for index in range(TASKS)]
            if all(value.task and value.leader for value in bound):
                cookies_bound = time.monotonic_ns()
                break
            need(command(.01) is None, 'unexpected bootstrap command')
        pending = seal_first()
        close_perf()
        drain(pending)
        need(not errors, 'bootstrap FD closure')
        cookie_rows = cookie_snapshot(allowlist, [b['births'][ct.c_int(index)] for index in range(TASKS)])
        alive()
        for event, name in [('sched_wakeup', b'on_wakeup'), ('sched_wakeup_new', b'on_wakeup_new'), ('sched_switch', b'on_switch')]:
            fd = lib.bpf_attach_raw_tracepoint(b.load_func(name, BPF.RAW_TRACEPOINT).fd, event.encode())
            need(fd >= 0, 'raw attach')
            raw_fds.append((event, fd))
            os.set_inheritable(fd, False)
        for link in uprobe_plan(owner['pid'], receipt['object_path']):
            path, address = BPF._check_path_symbol(os.fsencode(link['object_path']), GET_SYMBOL, None, link['attach_pid'])
            event = b._get_uprobe_evname(b'r' if link['return_probe'] else b'p', path, address, link['attach_pid'])
            need(all(item['event'] != event for item in uprobe_links), 'duplicate Get link')
            fd = lib.bpf_attach_uprobe(b.load_func(link['function'], BPF.KPROBE).fd, link['return_probe'], event, path, address, link['attach_pid'])
            need(fd >= 0, 'owned-mm Get probe attach')
            uprobe_links.append(dict(fd=fd, event=event, attach_pid=link['attach_pid'], return_probe=link['return_probe']))
            os.set_inheritable(fd, False)
        alive()
        data_deadline = sdk_go + args.duration * 1_000_000_000
        need(time.monotonic_ns() < data_deadline, 'attach consumed data horizon')
        b['deadline'][ct.c_int(0)] = ct.c_ulonglong(data_deadline)
        enabled_begin = time.monotonic_ns()
        open_phase(2)
        enabled_end = time.monotonic_ns()
        print('FULL_SCHED_ROI_ENABLED ' + json.dumps(dict(enabled_begin_ns=enabled_begin, enabled_end_ns=enabled_end,
            sdk_GO_ns=sdk_go, data_deadline_ns=data_deadline, exact_cookie_count=TASKS, Get_links=len(uprobe_links), bootstrap_fds_closed=True)), flush=True)
        while time.monotonic_ns() < data_deadline:
            alive()
            peak = max(peak, rss())
            need(peak <= RSS_LIMIT, 'runtime RSS budget')
            need(not any(int(value.value) for _, value in b['bad'].items()), 'kernel guard')
            line = command(.01)
            if line == 'STOP':
                raise TargetEnded('controller STOP')
            need(line is None, 'unknown ROI command')
        alive()
        status = 'FULL_SCHED_DIAGNOSTIC_COMPLETE'
    except TargetEnded as error:
        status, failure = 'FULL_SCHED_PREFIX_TARGET_ENDED', repr(error)
    except BaseException as error:
        failure = repr(error)
    finally:
        pending = None
        if b is not None and mode_requested:
            try:
                pending = seal_first()
            except BaseException as error:
                errors.append('seal ' + repr(error))
        if b is not None:
            errors.extend(close_owned(perf_fds, raw_fds, lib.bpf_close_perf_event_fd, os.close))
            errors.extend(close_uprobes(uprobe_links, lib.bpf_close_perf_event_fd, lib.bpf_detach_uprobe))
            if pending is not None:
                try:
                    drain(pending)
                    gate_drained = True
                except BaseException as error:
                    errors.append('drain ' + repr(error))
            try:
                if enabled_end is not None and gate_drained and not errors:
                    snapshot = dump_snapshot(b, args.events_output, allowlist, cookie_rows,
                        enabled_begin, enabled_end, disabled_begin, disabled_end, data_deadline, receipt)
            except BaseException as error:
                errors.append('snapshot ' + repr(error))
            try:
                b.cleanup()
            except BaseException as error:
                errors.append('module cleanup ' + repr(error))
        if pidfd is not None:
            try:
                os.close(pidfd)
            except BaseException as error:
                errors.append('pidfd close ' + repr(error))
    detached = not perf_fds and not raw_fds and not uprobe_links and not errors
    if errors:
        status = 'REJECT_DIAGNOSTIC_CLEANUP'
    if snapshot is not None:
        snapshot['detach_errors'] = list(errors)
        snapshot['gate_closed_drained'] = gate_drained
        metadata_path = Path(args.output).with_name(Path(args.output).stem + '.snapshot.json')
        with metadata_path.open('x') as stream:
            json.dump(snapshot, stream, indent=2)
            stream.write('\n')
        result.update(snapshot_metadata=snapshot, snapshot_metadata_path=str(metadata_path.resolve()),
            snapshot_metadata_sha256=digest(metadata_path), events_path=str(Path(args.events_output).resolve()),
            events_sha256=digest(args.events_output), events_bytes=Path(args.events_output).stat().st_size)
    trusted = bool(snapshot is not None and detached and gate_drained and not any(snapshot['bad']) and snapshot['commit_errors'] == 0)
    result.update(status=status, error=failure, detached=detached, all_bootstrap_fds_closed=not perf_fds and not any(value.startswith('perf close') for value in errors),
        gate_closed_drained=gate_drained, cleanup_errors=errors, trusted_observed_prefix=trusted,
        enabled_begin_ns=enabled_begin, enabled_end_ns=enabled_end, disabled_begin_ns=disabled_begin,
        disabled_end_ns=disabled_end, disabled_ns=disabled_end, data_deadline_ns=data_deadline, sdk_GO_ns=sdk_go,
        cookies_bound_ns=cookies_bound, target_duration_s=args.duration, map_budget_bytes=MAP_LIMIT,
        runtime_RSS_proposal=RSS_LIMIT, max_observed_runtime_RSS=peak, owned_maps=map_records,
        owned_programs=program_records, control_calls=calls, trace_format_sha256=formats, possible_CPUs=possible,
        full_phase_coverage=False, RPC30_accepted=False, target_kind=TARGET_KIND,
        scope=TARGET_KIND + ': caller scheduling and exact function-boundary diagnostic only')
    return result


def dump_snapshot(b, path, allowlist, cookies, enabled_begin, enabled_end, disabled_begin, disabled_end, deadline, receipt):
    counts = [int(b['next_record'][ct.c_int(index)].value) for index in range(TASKS)]
    stored = [min(value, OWNER_CAPACITY) for value in counts]
    commit_errors = 0
    with Path(path).open('xb') as stream:
        for owner, count in enumerate(stored):
            for index in range(count):
                value = b['records'][ct.c_int(owner * OWNER_CAPACITY + index)]
                fields = [int(getattr(value, name)) for name in ['event_ns', 'commit_index_plus_one', 'owner_slot', 'cpu', 'kind', 'flags', 'value']]
                if fields[1] != index + 1 or fields[2] != owner:
                    commit_errors += 1
                stream.write(struct.pack(EVENT_FORMAT, *fields))
                if index % 4096 == 0:
                    need(rss() <= RSS_LIMIT, 'snapshot RSS budget')
    bounds = b['seal_bounds'][ct.c_int(2)]
    return dict(schema='owner8-full-sched-snapshot-v1', record_bytes=EVENT_BYTES, byte_order='little',
        layout='owner-major-stored-prefixes', owner_capacity=OWNER_CAPACITY, owner_tids=[row['tid'] for row in allowlist],
        owner_attempted_counts=counts, owner_stored_counts=stored,
        get_next=[int(b['get_next'][ct.c_int(index)].value) for index in range(TASKS)],
        get_pending=[int(b['get_pending'][ct.c_int(index)].value) for index in range(TASKS)],
        bad=[int(b['bad'][ct.c_int(index)].value) for index in range(4)],
        enabled_begin_ns=enabled_begin, enabled_end_ns=enabled_end, data_deadline_ns=deadline,
        disabled_begin_ns=disabled_begin, disabled_end_ns=disabled_end,
        seal_begin_ns=int(bounds.begin), seal_end_ns=int(bounds.end), gate_closed_drained=True,
        detach_errors=[], clock_alignment_verified=receipt['clock_alignment_verified'], owner_cookies=cookies,
        commit_errors=commit_errors, target_kind=TARGET_KIND,
        source_sha256=digest(Path(__file__).with_suffix('.bpf.c')))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--receipt', required=True)
    parser.add_argument('--allowlist', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--events-output', required=True)
    parser.add_argument('--duration', type=int, default=80)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    need(1 <= args.duration <= 80, 'duration must be1..80 seconds')
    need(EVENT_BYTES == 32, 'event layout')
    receipt = json.loads(Path(args.receipt).read_text())
    allowlist = json.loads(Path(args.allowlist).read_text())
    validate_inputs(receipt, allowlist)
    metadata_path = Path(args.output).with_name(Path(args.output).stem + '.snapshot.json')
    need(all(not path.exists() for path in [Path(args.output), Path(args.events_output), metadata_path]), 'outputs must be new')
    need(Path(args.output).resolve() != Path(args.events_output).resolve(), 'distinct outputs')
    if not args.execute:
        print(json.dumps(dict(status='PLAN_ONLY_NO_BCC_IMPORT_OR_ATTACH', callers=TASKS, duration_max=args.duration, RPC30_accepted=False)))
        return 0
    global _stop_requested
    _stop_requested = False
    def interrupted(_signal, _frame):
        global _stop_requested
        _stop_requested = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        result = execute(args, receipt, allowlist)
    except BaseException as error:
        result = dict(status='REJECT_OUTER_EXCEPTION', error=repr(error), detached=False,
            all_bootstrap_fds_closed=False, gate_closed_drained=False, trusted_observed_prefix=False, RPC30_accepted=False)
    with Path(args.output).open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print('PROBER_STOPPED ' + json.dumps({key: result.get(key) for key in ['status', 'detached', 'all_bootstrap_fds_closed', 'gate_closed_drained', 'trusted_observed_prefix']}), flush=True)
    return 0 if result['status'] == 'FULL_SCHED_DIAGNOSTIC_COMPLETE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
