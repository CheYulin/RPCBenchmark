#!/usr/bin/env python3
"""Private bounded64K/256K native echo screen; no instrumentation in clean windows."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time

CASES = ('large', 'tiny', 'mixed')
ORDER = ('baseline64k', 'candidate256k', 'candidate256k', 'baseline64k')
THRESHOLDS = {'large_qps_min_gain_pct': 5, 'large_cpu_min_drop_pct': 5,
              'tiny_qps_max_drop_pct': 3, 'tiny_p99_median_max_rise_pct': 5,
              'tiny_p99_pair_max_rise_pct': 10, 'mixed_bulk_qps_max_drop_pct': 3}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def change(before, after):
    if before <= 0:
        raise ValueError('nonpositive comparison denominator')
    return (after / before - 1) * 100


def validate_result(row, concurrency, payload, seconds):
    if row['workers'] != 4 or row['concurrency'] != concurrency or row['payload'] != payload:
        raise ValueError('worker/concurrency/payload mismatch')
    if row['active_lanes'] != concurrency or row['success'] <= 0 or row['elapsed_s'] < seconds:
        raise ValueError('empty/truncated/idle lane measurement')
    if row['attempts'] != row['success'] + row['rpc_errors'] + row['mismatches']:
        raise ValueError('attempt accounting mismatch')
    if row['errors'] != row['rpc_errors'] + row['mismatches'] or row['errors'] != 0:
        raise ValueError('RPC or byte mismatch')
    histogram = row['histogram']
    if not histogram or any(n <= 0 or bound <= 0 for bound, n in histogram):
        raise ValueError('invalid histogram')
    if any(histogram[i][0] >= histogram[i + 1][0] for i in range(len(histogram) - 1)):
        raise ValueError('unsorted histogram')
    if row['samples'] != row['success'] or row['success'] != sum(n for _, n in histogram):
        raise ValueError('histogram/sample count mismatch')
    if not 0 < row['p50_us'] <= row['p95_us'] <= row['p99_us'] <= row['p999_us']:
        raise ValueError('invalid quantiles')


def validate_output(text, case, seconds):
    records = [json.loads(line) for line in text.splitlines() if line.startswith('{')]
    config = next(row for row in records if row.get('record') == ('mixed' if case == 'mixed' else 'pipeline'))
    if config['pipeline'] != 1 or config['connections'] != 32 or config['caller'] != 'coroutine':
        raise ValueError('native connection/caller contract mismatch')
    if len(config['peak_native']) != 32 or any(peak != 1 for peak in config['peak_native']):
        raise ValueError('connection did not exercise exactly one outstanding call')
    rows = [row for row in records if 'framework' in row]
    if case == 'mixed':
        if len(rows) != 2 or {row['framework'] for row in rows} != {'coro_mixed_tiny', 'coro_mixed_bulk'}:
            raise ValueError('missing/duplicate payload group result')
        groups = {('tiny' if row['framework'] == 'coro_mixed_tiny' else 'bulk'): row for row in rows}
        validate_result(groups['tiny'], 24, 4096, seconds)
        validate_result(groups['bulk'], 8, 1048576, seconds)
        start, end, drain = (config[key] for key in ('phase_start_ns', 'phase_end_ns', 'common_drain_end_ns'))
        if end - start != seconds * 10**9 or drain < end:
            raise ValueError('mixed measurement phase mismatch')
        expected_elapsed = (drain - start) / 1e9
        if groups['tiny']['elapsed_s'] != groups['bulk']['elapsed_s']:
            raise ValueError('payload groups use different denominator windows')
        if abs(groups['tiny']['elapsed_s'] - expected_elapsed) > 0.000501:
            raise ValueError('common drain end does not match group elapsed')
        if (config['tiny_lanes'], config['bulk_lanes'], config['tiny_payload'], config['bulk_payload']) != (24, 8, 4096, 1048576):
            raise ValueError('mixed payload allocation mismatch')
    else:
        if len(rows) != 1 or rows[0]['framework'] != 'coro_rpc_pipeline':
            raise ValueError('unexpected pure result')
        groups = {'all': rows[0]}
        validate_result(rows[0], 32, 1048576 if case == 'large' else 4096, seconds)
    return config, groups


def evaluate(rows):
    by_case = {}
    checks = {}
    for case in CASES:
        selected = [row for row in rows if row['case'] == case]
        if len(selected) != 4 or [row['variant'] for row in selected] != list(ORDER):
            raise ValueError('incomplete or nonABBA case')
        groups = ('tiny', 'bulk') if case == 'mixed' else ('all',)
        medians = {}
        for variant in ('baseline64k', 'candidate256k'):
            rr = [row for row in selected if row['variant'] == variant]
            medians[variant] = {
                group: {key: statistics.median(row['groups'][group][key] for row in rr)
                        for key in ('rps', 'p50_us', 'p99_us')} for group in groups}
            medians[variant]['total_cpu_us_per_success'] = statistics.median(row['total_cpu_us_per_success'] for row in rr)
        pairs = []
        for baseline, candidate in ((selected[0], selected[1]), (selected[3], selected[2])):
            pairs.append({group: {key + '_change_pct': change(baseline['groups'][group][key], candidate['groups'][group][key])
                                  for key in ('rps', 'p99_us')} for group in groups})
        delta = {group: {key + '_change_pct': change(medians['baseline64k'][group][key], medians['candidate256k'][group][key])
                         for key in ('rps', 'p99_us')} for group in groups}
        by_case[case] = {'windows': selected, 'medians': medians, 'paired_deltas': pairs, 'median_deltas': delta}
        if case == 'large':
            cpu_delta = change(medians['baseline64k']['total_cpu_us_per_success'], medians['candidate256k']['total_cpu_us_per_success'])
            cpu_pairs = [change(selected[b]['total_cpu_us_per_success'], selected[c]['total_cpu_us_per_success'])
                         for b, c in ((0, 1), (3, 2))]
            by_case[case]['cpu_delta_pct'] = cpu_delta
            by_case[case]['cpu_paired_delta_pct'] = cpu_pairs
            checks['large_qps_gain'] = delta['all']['rps_change_pct'] >= THRESHOLDS['large_qps_min_gain_pct']
            checks['large_cpu_drop'] = cpu_delta <= -THRESHOLDS['large_cpu_min_drop_pct']
            checks['large_pairs_agree'] = all(pair['all']['rps_change_pct'] > 0 for pair in pairs) and all(value < 0 for value in cpu_pairs)
        else:
            group = 'tiny' if case == 'mixed' else 'all'
            checks[case + '_tiny_qps'] = delta[group]['rps_change_pct'] >= -THRESHOLDS['tiny_qps_max_drop_pct']
            checks[case + '_tiny_p99_median'] = delta[group]['p99_us_change_pct'] <= THRESHOLDS['tiny_p99_median_max_rise_pct']
            checks[case + '_tiny_p99_pairs'] = all(pair[group]['p99_us_change_pct'] <= THRESHOLDS['tiny_p99_pair_max_rise_pct'] for pair in pairs)
            checks[case + '_tiny_p99_fine_resolution'] = all(row['groups'][group]['p99_us'] <= 1024 for row in selected)
            if case == 'mixed':
                checks['mixed_bulk_not_starved'] = delta['bulk']['rps_change_pct'] >= -THRESHOLDS['mixed_bulk_qps_max_drop_pct']
    return {'complete': True, 'candidate_gate': 'consider_separate_DS_pilot' if all(checks.values()) else 'reject_migration',
            'checks': checks, 'thresholds': THRESHOLDS, 'cases': by_case,
            'scope': 'native echo only; two samples per variant are a conservative screen,not significance or DS adoption proof'}


def run_case(args, lab, groups, case, variant, index):
    binary = args.baseline if variant == 'baseline64k' else args.candidate
    case_dir = args.out / f'{case}-{index + 1:02d}-{variant}'
    case_dir.mkdir()
    port = lab.free_port()
    server_cmd = [str(binary), 'server', '--port', str(port), '--workers', '4']
    client_cmd = [str(binary), 'client', '--port', str(port), '--workers', '4', '--concurrency', '32',
                  '--pipeline', '1', '--caller', 'coroutine', '--payload', str(1048576 if case == 'large' else 4096),
                  '--warmup', str(args.warmup), '--seconds', str(args.seconds), '--timeout-ms', str(args.timeout_ms)]
    if case == 'mixed':
        client_cmd.append('--mixed')
    write_json(case_dir / 'config.json', {'case': case, 'variant': variant, 'server_cmd': server_cmd,
                'client_cmd': client_cmd, 'server_cpus': args.server_cpus, 'client_cpus': args.client_cpus,
                'binary_sha256': sha(binary)})
    server = client = None
    warmup = b''
    try:
        with (case_dir / 'server.stderr').open('wb') as log:
            server = groups.launch('server', set(args.server_cpus), server_cmd, stdout=subprocess.PIPE, stderr=log, bufsize=0)
            (case_dir / 'server.stdout').write_text(lab.wait_ready(server, 8) + '\n')
            client = groups.launch('client', set(args.client_cpus), client_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            warmup = lab.wait_for_warmup(client, args.warmup + 12)
            counters = next(json.loads(line.removeprefix('WARMUP_COUNTERS ')) for line in warmup.decode().splitlines()
                            if line.startswith('WARMUP_COUNTERS '))
            if counters['rpc_errors'] or counters['mismatches'] or counters['success'] <= 0:
                raise ValueError('invalid warmup counters')
            cpu_window_start_ns = time.monotonic_ns()
            cpu_begin = groups.cpu()
            observed = lab.socket_count(port)
            if observed != 32:
                raise ValueError('expected32realTCPconnections')
            telemetry = [lab.sample(server, client)]
            deadline = time.monotonic() + args.seconds + 12
            while client.poll() is None and time.monotonic() < deadline:
                try:
                    client.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    telemetry.append(lab.sample(server, client))
            cpu_end = groups.cpu()
            cpu_window_end_ns = time.monotonic_ns()
            telemetry.append(lab.sample(server, client))
            stdout, stderr = client.communicate(timeout=2)
            (case_dir / 'client.stdout').write_bytes(stdout)
            (case_dir / 'client.stderr').write_bytes(warmup + stderr)
            write_json(case_dir / 'telemetry.json', {'clock_hz': lab.CLOCK_HZ, 'cpu_begin': cpu_begin,
                       'cpu_end': cpu_end, 'cpu_window_start_ns': cpu_window_start_ns,
                       'cpu_window_end_ns': cpu_window_end_ns,
                       'connections_observed': observed, 'samples': telemetry})
            if client.returncode != 0:
                raise RuntimeError(f'client exited{client.returncode}')
            config, payload_groups = validate_output(stdout.decode(), case, args.seconds)
            cpu = {role: {name: cpu_end[role][name] - cpu_begin[role][name]
                          for name in ('usage_usec', 'user_usec', 'system_usec')} for role in cpu_begin}
            if set(cpu) != {'server', 'client'} or any(row['usage_usec'] <= 0 for row in cpu.values()):
                raise RuntimeError('missing roleCPU evidence')
            successes = sum(row['success'] for row in payload_groups.values())
            cpu_window_s = (cpu_window_end_ns - cpu_window_start_ns) / 1e9
            cpu_total = sum(row['usage_usec'] for row in cpu.values())
            payload_bytes = sum(row['success'] * row['payload'] for row in payload_groups.values())
            result = {'case': case, 'variant': variant, 'index': index + 1, 'groups': payload_groups,
                      'config': config, 'connections_observed': observed, 'cpu': cpu,
                      'cpu_window_s': cpu_window_s,
                      'cpu_cores_by_role': {role: row['usage_usec'] / 1e6 / cpu_window_s for role, row in cpu.items()},
                      'total_cpu_us_per_success': cpu_total / successes,
                      'cpu_us_per_payload_gib': cpu_total * 2**30 / payload_bytes,
                      'cpu_window': 'WARMUP_DONEthroughprocesscompletion;includesphaselead/drain/teardown;not perclassCPU'}
    finally:
        if client and not (case_dir / 'client.stderr').exists():
            lab.stop(client)
            stdout, stderr = client.communicate(timeout=2)
            (case_dir / 'client.stdout').write_bytes(stdout)
            (case_dir / 'client.stderr').write_bytes(warmup + stderr)
        lab.stop(server)
    if server.returncode != 0:
        raise RuntimeError(f'server did not shut down cleanly:{server.returncode}')
    write_json(case_dir / 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--baseline-sha256', required=True)
    parser.add_argument('--candidate-sha256', required=True)
    parser.add_argument('--lab-root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--server-cpus', default='4,6,8,10')
    parser.add_argument('--client-cpus', default='20,22,24,26')
    parser.add_argument('--monitor-cpu', type=int, default=36)
    parser.add_argument('--warmup', type=int, default=1, choices=(1,))
    parser.add_argument('--seconds', type=int, default=4, choices=(4,))
    parser.add_argument('--timeout-ms', type=int, default=1000, choices=(1000,))
    args = parser.parse_args()
    args.baseline, args.candidate, args.out = args.baseline.resolve(), args.candidate.resolve(), args.out.resolve()
    def verify_binaries():
        if sha(args.baseline) != args.baseline_sha256 or sha(args.candidate) != args.candidate_sha256:
            raise RuntimeError('binaryhashdrift')
    verify_binaries()
    for name in ('server_cpus', 'client_cpus'):
        setattr(args, name, [int(value) for value in getattr(args, name).split(',')])
    cpus = args.server_cpus + args.client_cpus + [args.monitor_cpu]
    if len(args.server_cpus) != 4 or len(args.client_cpus) != 4 or len(set(cpus)) != 9 or not set(cpus) <= os.sched_getaffinity(0):
        parser.error('requires four allowed disjoint physicalCPUs perrole and independentmonitor')
    siblings = [(Path('/sys/devices/system/cpu') / f'cpu{cpu}/topology/thread_siblings_list').read_text().strip() for cpu in cpus]
    if len(set(siblings)) != 9:
        parser.error('CPUroles reusephysicalSMTsiblings')
    if not shutil.which('ss'):
        parser.error('ssrequiredforconnectionevidence')
    spec = importlib.util.spec_from_file_location('bulk_lab', args.lab_root / 'tools/run_matrix.py')
    lab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lab)
    os.sched_setaffinity(0, {args.monitor_cpu})
    args.out.mkdir(parents=True, exist_ok=False)
    write_json(args.out / 'plan.json', {'cases': CASES, 'order': ORDER, 'thresholds': THRESHOLDS,
               'binary_hashes': {'baseline64k': args.baseline_sha256, 'candidate256k': args.candidate_sha256},
               'source_runner_sha256': sha(Path(__file__)), 'warmup_s': 1, 'measure_s': 4,
               'server_cpus': args.server_cpus, 'client_cpus': args.client_cpus,
               'instrumentation': 'none: cgroup/proc roleCPU snapshots only;no syscalltrace/perf'})
    groups = lab.Groups(True)
    rows = []
    with (args.out / 'results.jsonl').open('w') as stream:
        for case in CASES:
            for index, variant in enumerate(ORDER):
                verify_binaries()
                row = run_case(args, lab, groups, case, variant, index)
                rows.append(row)
                stream.write(json.dumps(row) + '\n')
                stream.flush()
                print(f'{case} {index + 1}/4 {variant} valid', flush=True)
    verify_binaries()
    result = evaluate(rows)
    write_json(args.out / 'gate.json', result)
    print(json.dumps({'complete': True, 'candidate_gate': result['candidate_gate'], 'checks': result['checks']}), flush=True)


if __name__ == '__main__':
    main()
