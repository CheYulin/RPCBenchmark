"""Read-only finite closed-loop ABI2 diagnostic analysis; never RPC30 acceptance.

File naming and phase orchestration belong to the controller. This module takes
independent driver ledgers, one cumulative final capture prefix and full API counts.
"""
import copy
import struct

MASK64 = (1 << 64) - 1
SAMPLE_FIELDS = ('arrival_id send_ns recv_ns server_processing_ns server_execution_ns '
                 'phase_id rpc_ordinal caller_index method controller_error ds_error flags').split()
END_FIELDS = ('phase_id arrival_id scheduled_ns api_begin_ns api_end_ns validation_end_ns '
              'observed_methods capture_errors rpc_count caller_index outcome flags').split()
LAYOUTS = {'rpc': ('5Q2I2H2iI', SAMPLE_FIELDS, 64), 'end': ('8Q2IiI', END_FIELDS, 80)}
PRIMARY_ERROR_FLAGS = 2 | 4 | 8 | 16 | 32
KNOWN_FLAGS = 1 | PRIMARY_ERROR_FLAGS | 256 | 512 | 1024 | 2048 | 4096
MAX_PLAN = 2_000_000
MAX_CAPTURE_BYTES = 128 * 1024 * 1024


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _mix(value):
    value = ((value ^ (value >> 30)) * 0xbf58476d1ce4e5b9) & MASK64
    value = ((value ^ (value >> 27)) * 0x94d049bb133111eb) & MASK64
    return value ^ (value >> 31)


def _validate_plan(plan):
    bounds = {'callers': (1, 128), 'warm_per_caller': (1, MAX_PLAN),
              'measure_per_caller': (1, MAX_PLAN), 'seed': (0, MASK64),
              'modulus': (1, (1 << 32) - 1)}
    for key, (lower, upper) in bounds.items():
        value = plan.get(key)
        if not _integer(value) or not lower <= value <= upper:
            raise ValueError('invalid finite plan field: ' + key)
    if plan.get('operation') not in ('get', 'set'):
        raise ValueError('operation must be get or set')
    total = plan['callers'] * (plan['warm_per_caller'] + plan['measure_per_caller'])
    if total > MAX_PLAN:
        raise ValueError('finite plan exceeds ABI helper maximum')


def expected_selected_keys(plan):
    """Return the complete preselected (phase, arrival, caller) population."""
    _validate_plan(plan)
    result = set()
    for phase, count in [(1, plan['warm_per_caller']), (2, plan['measure_per_caller'])]:
        for arrival in range(count * plan['callers']):
            hashed = _mix(plan['seed'] ^ _mix(phase) ^ _mix((arrival + 0x9e3779b97f4a7c15) & MASK64))
            if hashed % plan['modulus'] == 0:
                result.add((phase, arrival, arrival % plan['callers']))
    if not result:
        raise ValueError('finite capture plan selects no API IDs')
    return result


def nearest_rank_stats(values):
    ordered = sorted(values)
    if any(not _integer(value) or value < 0 for value in ordered):
        raise ValueError('durations must be nonnegative integer nanoseconds')
    count = len(ordered)
    return {'count': count,
            'p50_ns': ordered[(count * 50 + 99) // 100 - 1] if count else None,
            'p99_ns': ordered[(count * 99 + 99) // 100 - 1] if count else None,
            'min_ns': ordered[0] if count else None,
            'max_ns': ordered[-1] if count else None,
            'mean_ns': sum(ordered) / count if count else None}


def decode_pod_rows(data, kind, byte_order):
    """Decode a single blob after its external ABI/prefix metadata was checked."""
    if kind not in LAYOUTS or byte_order not in ('little', 'big'):
        raise ValueError('explicit RPC/end kind and little/big byte order required')
    layout, fields, width = LAYOUTS[kind]
    if not isinstance(data, bytes) or len(data) > MAX_CAPTURE_BYTES or len(data) % width:
        raise ValueError('POD byte length violates ABI2 layout/budget')
    decoder = struct.Struct(('<' if byte_order == 'little' else '>') + layout)
    if decoder.size != width:
        raise ValueError('POD decoder layout mismatch')
    return [dict(zip(fields, row)) for row in decoder.iter_unpack(data)]


def _row_key(row, kind):
    fields = ['phase_id', 'arrival_id', 'caller_index']
    if kind == 'rpc':
        fields.append('rpc_ordinal')
    values = tuple(row.get(field) for field in fields)
    if any(not _integer(value) for value in values):
        raise ValueError('record key fields must be integers')
    return values


def merge_prefix_rows(warm_rows, final_rows, kind):
    """Check a warm snapshot against final cumulative rows, returning final once.

    Repeated rows within either snapshot are errors. Equal warm rows appearing in
    the final snapshot are expected; divergent or absent rows invalidate the input.
    """
    if kind not in LAYOUTS:
        raise ValueError('unknown prefix row kind')
    def index(rows):
        result = {}
        for row in rows:
            key = _row_key(row, kind)
            if key in result:
                raise ValueError('duplicate within a single prefix')
            result[key] = row
        return result
    warm, final = index(warm_rows), index(final_rows)
    for key, row in warm.items():
        if key not in final or row != final[key]:
            raise ValueError('warm prefix absent or changed in cumulative final prefix')
    return copy.deepcopy(final_rows)


def _metadata(row, duration):
    flags = row['flags']
    result = {}
    for name, valid, invalid in [('processing', 256, 512), ('execution', 1024, 2048)]:
        value = row.get('server_' + name + '_ns')
        state = 'invalid' if flags & invalid else 'valid' if flags & valid else 'missing'
        if state == 'valid' and (not _integer(value) or not 0 <= value <= duration):
            state = 'invalid'
        result[name] = (state, value if state == 'valid' else None)
    processing, execution = result['processing'], result['execution']
    if processing[0] == execution[0] == 'valid' and execution[1] > processing[1]:
        result['execution'] = ('invalid', None)
    return result


def analyze_rows(plan, ledgers, ends, rpcs, slots, summary):
    """Validate independent selected ledgers and one cumulative capture prefix.

    All supplied rows, including errors or unselected anomalies, remain in raw_rows.
    Any primary/conservation error disables reported quantiles; optional server
    metadata errors affect only diagnostic span counts, never primary RPC membership.
    """
    expected = expected_selected_keys(plan)
    errors = []
    def error(message):
        errors.append(message)
    def check(condition, message):
        if not condition:
            error(message)
        return bool(condition)
    def index(rows, kind):
        result = {}
        for number, row in enumerate(rows):
            if not isinstance(row, dict):
                error(kind + ' non-object row at ' + str(number)); continue
            try:
                key = _row_key(row, 'end')
            except ValueError:
                error(kind + ' invalid key at ' + str(number)); continue
            if key in result:
                error(kind + ' duplicate key ' + str(key)); continue
            result[key] = row
            if key not in expected:
                error(kind + ' unexpected/unselected key ' + str(key))
        return result
    ledger_index, end_index = index(ledgers, 'ledger'), index(ends, 'end')
    for name, values in [('ledger', ledger_index), ('end', end_index)]:
        missing = expected - values.keys()
        if missing:
            error(name + ' missing selected keys: ' + str(sorted(missing)))

    slot_index = {}
    for slot in slots:
        caller = slot.get('caller') if isinstance(slot, dict) else None
        if not check(_integer(caller) and 0 <= caller < plan['callers'], 'invalid slot caller'):
            continue
        if caller in slot_index:
            error('duplicate slot caller ' + str(caller)); continue
        slot_index[caller] = slot
        check(_integer(slot.get('cached_tid')) and slot['cached_tid'] > 0, 'invalid slot TID')
        for field in ['errors', 'error_generation', 'first_driver_status', 'active']:
            if field in slot:
                check(slot[field] == 0, 'nonzero slot ' + field + ': ' + str(caller))
        if 'api_count' in slot and slot.get('counters_available', True):
            check(slot['api_count'] == plan['warm_per_caller'] + plan['measure_per_caller'],
                  'slot API count mismatch: ' + str(caller))
        for field in ['api_failures', 'rpc_failures']:
            if field in slot and slot.get('counters_available', True):
                check(slot[field] == 0, 'slot failure count: ' + field)
    check(set(slot_index) == set(range(plan['callers'])), 'caller slot set mismatch')

    for phase, count in [(1, plan['warm_per_caller']), (2, plan['measure_per_caller'])]:
        phases = summary.get('phases', {})
        counts = phases.get(phase, phases.get(str(phase), {}))
        population = count * plan['callers']
        for field, wanted in [('attempts', population), ('success', population),
                              ('errors', 0), ('histogram_count', population)]:
            check(_integer(counts.get(field)) and counts[field] == wanted,
                  'full API ' + field + ' mismatch in phase ' + str(phase))
    for field in ['global_errors', 'control_status']:
        if field in summary:
            check(summary[field] == 0, 'nonzero ' + field)

    primary_method = 1 if plan['operation'] == 'get' else 4
    rpc_groups = {}
    for number, row in enumerate(rpcs):
        if not isinstance(row, dict):
            error('RPC non-object row at ' + str(number)); continue
        try:
            key = _row_key(row, 'end')
        except ValueError:
            error('RPC invalid key at ' + str(number)); continue
        rpc_groups.setdefault(key, []).append(row)
        check(key in expected, 'RPC unexpected/unselected key ' + str(key))

    joined = []
    for key in sorted(expected):
        ledger, end = ledger_index.get(key), end_index.get(key)
        if ledger is None or end is None:
            continue
        phase, arrival, caller = key
        check(ledger.get('linux_tid') == slot_index.get(caller, {}).get('cached_tid'),
              'ledger/slot TID mismatch ' + str(key))
        for field in ['scheduled_ns', 'api_begin_ns', 'api_end_ns', 'validation_end_ns']:
            check(_integer(ledger.get(field)) and ledger[field] >= 0,
                  'invalid ledger timestamp ' + field + ' ' + str(key))
            check(ledger.get(field) == end.get(field), 'End/ledger timestamp mismatch ' + field + ' ' + str(key))
        begin, finish, validated = [ledger.get(field) for field in
                                    ['api_begin_ns', 'api_end_ns', 'validation_end_ns']]
        timeline_ok = all(_integer(value) for value in [begin, finish, validated])
        timeline_ok = timeline_ok and 0 < begin <= finish <= validated
        check(timeline_ok, 'invalid API timeline ' + str(key))
        check(ledger.get('scheduled_ns') == begin, 'closed-loop scheduled must equal begin ' + str(key))
        check(_integer(ledger.get('status_code')) and _integer(end.get('outcome'))
              and ledger['status_code'] == end['outcome'] == 0, 'API outcome error ' + str(key))
        check(end.get('capture_errors') == 0, 'capture End errors ' + str(key))
        flags = end.get('flags')
        check(_integer(flags) and bool(flags & 1) and not flags & PRIMARY_ERROR_FLAGS and not flags & ~KNOWN_FLAGS,
              'capture End flags invalid ' + str(key))
        methods = end.get('observed_methods')
        check(_integer(methods) and bool(methods & (1 << primary_method)), 'required primary method missing ' + str(key))
        group = rpc_groups.get(key, [])
        count = end.get('rpc_count')
        valid_count = _integer(count) and 1 <= count <= 16
        check(valid_count and len(group) == count, 'RPC count mismatch ' + str(key))
        ordinals = [row.get('rpc_ordinal') for row in group]
        check(valid_count and all(_integer(value) for value in ordinals)
              and len(set(ordinals)) == len(ordinals)
              and set(ordinals) == set(range(1, count + 1)), 'RPC ordinal prefix invalid ' + str(key))
        check(any(row.get('method') == primary_method for row in group), 'primary RPC absent ' + str(key))
        derived_methods = 0
        for row in group:
            method = row.get('method')
            if _integer(method) and 1 <= method <= 10:
                derived_methods |= 1 << method
        check(methods == derived_methods, 'observed method mask differs from RPC rows ' + str(key))
        for row in group:
            method = row.get('method')
            check(_integer(method) and 1 <= method <= 10, 'unknown RPC method ' + str(key))
            check(row.get('controller_error') == row.get('ds_error') == 0, 'RPC failure ' + str(key))
            flags = row.get('flags')
            flags_ok = (_integer(flags) and bool(flags & 1) and not flags & PRIMARY_ERROR_FLAGS
                        and not flags & ~KNOWN_FLAGS)
            check(flags_ok, 'RPC primary flags invalid ' + str(key))
            send, receive = row.get('send_ns'), row.get('recv_ns')
            endpoint_ok = timeline_ok and all(_integer(value) for value in [send, receive])
            endpoint_ok = endpoint_ok and begin <= send <= receive <= finish
            check(endpoint_ok, 'RPC endpoint outside API ' + str(key))
            if endpoint_ok and _integer(flags):
                duration = receive - send
                joined.append({'phase': phase, 'key': key, 'method': method,
                               'rpc_ns': duration, 'api_ns': finish - begin,
                               'rpc_count': count, 'flags': flags,
                               'metadata': _metadata(row, duration)})

    valid = not errors
    phases_out = {}
    for phase in [1, 2]:
        rows = [row for row in joined if row['phase'] == phase]
        selected_apis = [row for key, row in ledger_index.items() if key in expected and key[0] == phase]
        states = {name: {state: 0 for state in ['valid', 'missing', 'invalid']}
                  for name in ['processing', 'execution']}
        values = {name: [] for name in ['rpc', 'primary_rpc', 'server_processing', 'server_execution',
                                       'rpc_minus_server_processing', 'processing_minus_execution',
                                       'single_rpc_api_minus_rpc']}
        by_method = {}
        for row in rows:
            values['rpc'].append(row['rpc_ns'])
            by_method.setdefault(str(row['method']), []).append(row['rpc_ns'])
            if row['method'] == primary_method:
                values['primary_rpc'].append(row['rpc_ns'])
            if row['rpc_count'] == 1:
                values['single_rpc_api_minus_rpc'].append(row['api_ns'] - row['rpc_ns'])
            for name in ['processing', 'execution']:
                state, value = row['metadata'][name]
                states[name][state] += 1
                if state == 'valid':
                    values['server_' + name].append(value)
            processing = row['metadata']['processing'][1]
            execution = row['metadata']['execution'][1]
            if processing is not None and not row['flags'] & 4096:
                values['rpc_minus_server_processing'].append(row['rpc_ns'] - processing)
            if processing is not None and execution is not None:
                values['processing_minus_execution'].append(processing - execution)
        phase_result = {'selected_api_count': sum(key[0] == phase for key in expected),
                        'statistics_complete': valid, 'metadata_states': states,
                        'rpc_by_method': {method: nearest_rank_stats(data) for method, data in by_method.items()} if valid else {},
                        'sampled_api': nearest_rank_stats([row['api_end_ns'] - row['api_begin_ns'] for row in selected_apis]) if valid else None}
        phase_result.update({name: nearest_rank_stats(data) if valid else None for name, data in values.items()})
        phases_out[str(phase)] = phase_result
    return {'diagnostic_valid': valid, 'validation_errors': errors, 'RPC30_accepted': False,
            'phases': phases_out, 'raw_rows': copy.deepcopy({'ledgers': ledgers, 'ends': ends, 'rpcs': rpcs}),
            'raw_slots': copy.deepcopy(slots), 'full_api_summary': copy.deepcopy(summary),
            'limits': ['Empirical preselected sample quantiles are not full-population RPC P99 or formal RPC30 acceptance.',
                       'Full API count/histogram conservation is separate from sampled API/RPC rows.',
                       'The finite closed-loop workload tapers as callers finish and is not fixed offered load.',
                       'Server spans start/end inside generated adapters; RPC-minus-server is a mixed residual, not pure network.',
                       'Optional diagnostic metadata never filters primary RPCs; missing/invalid counts remain explicit.',
                       'All raw anomaly/error rows are retained; any primary error disables quantiles.',
                       'Off/on SDK API observer calibration does not prove each internal phase has <=3% perturbation.']}
