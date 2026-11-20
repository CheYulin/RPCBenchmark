import copy
import struct
import unittest
from analyze_full_sched import analyze

RECORD = struct.Struct('<QQIIHHI')
WAKE, WAKE_NEW, IN, OUT, ENTRY, RETURN = range(1, 7)


def fixture(events, *, pending=None):
    raw = bytearray(); counts = []
    for owner in range(8):
        population = [e for e in events if e[0] == owner]
        counts.append(len(population))
        for sequence, (_, stamp, kind, value, flags, cpu) in enumerate(population, 1):
            raw += RECORD.pack(stamp, sequence, owner, cpu, kind, flags, value)
    meta = dict(schema='owner8-full-sched-snapshot-v1', record_bytes=32,
        byte_order='little', layout='owner-major-stored-prefixes', owner_capacity=65536,
        owner_tids=list(range(101, 109)), owner_attempted_counts=counts,
        owner_stored_counts=counts.copy(), bad=[0, 0, 0, 0],
        get_next=[max([e[3] for e in events if e[0] == owner and e[2] == ENTRY] or [0]) for owner in range(8)],
        get_pending=pending or [0]*8, enabled_begin_ns=99, enabled_end_ns=100,
        data_deadline_ns=200, disabled_begin_ns=201, disabled_end_ns=204,
        seal_begin_ns=202, seal_end_ns=203, gate_closed_drained=True,
        detach_errors=[], clock_alignment_verified=True,
        owner_cookies=[dict(slot=i, tid=101+i, tgid=50, task_start_boottime_ns=1000+i,
                           leader_start_boottime_ns=900) for i in range(8)])
    return meta, bytes(raw)


def e(t, kind, value=0, flags=0, cpu=1, owner=0):
    return owner, t, kind, value, flags, cpu


def row(arrival=0, *, caller=0, scheduled=105, begin=135, end=155, observer=165, state='returned'):
    attempted = state == 'returned'
    return dict(phase_id=1, arrival_id=arrival, caller=caller, tid=101+caller,
        scheduled_ns=scheduled, dispatch_checked_ns=begin,
        api_begin_ns=begin if attempted else None, api_end_ns=end if attempted else None,
        validation_end_ns=end+1 if attempted else None, observer_end_ns=observer if attempted else None,
        state=state, attempted=attempted, status=0 if attempted else None,
        selected=True, mismatch=False, required_methods=2)


def selected(report, arrival=0, caller=0):
    return next(r for r in report['rows'] if r['key'] == [1, arrival, caller])


def interval(report, name, arrival=0, caller=0):
    return selected(report, arrival, caller).get('intervals', {}).get(name, {})


def issue(report, word):
    return any(word in str(v) for v in report['integrity']['issues'])


class FullSchedOffline(unittest.TestCase):
    def test_union_three_states_duplicate_wake_migration_and_exact_pair(self):
        meta, raw = fixture([e(110, OUT, 1), e(120, WAKE), e(125, WAKE),
            e(130, IN, cpu=2), e(140, ENTRY, 1, cpu=2), e(150, RETURN, 1, cpu=2),
            e(160, OUT, 2, 1, cpu=2), e(170, IN, cpu=3)])
        result = analyze(meta, raw, [row()])
        self.assertEqual(interval(result, 'scheduled_to_checked').get('states_ns'),
            dict(unknown=5, nonrunnable=10, runnable=10, scheduled=5))
        self.assertEqual(interval(result, 'get_entry_to_return').get('states_ns', {}).get('scheduled'), 10)
        self.assertEqual(interval(result, 'api_end_to_observer').get('states_ns'),
            dict(unknown=0, nonrunnable=0, runnable=5, scheduled=5))
        self.assertEqual(selected(result)['get'].get('status'), 'matched')
        self.assertEqual(len(result['duplicate_wakes']), 1)
        self.assertEqual([(m['from_cpu'],m['to_cpu'])for m in result['migrations']], [(1,2),(2,3)])

    def test_half_open_boundaries_do_not_double_count(self):
        meta, raw = fixture([e(100, IN), e(110, OUT, 0), e(120, IN), e(130, OUT, 1)])
        result = analyze(meta, raw, [row(scheduled=100, begin=110, end=120, observer=130)])
        self.assertEqual(interval(result, 'scheduled_to_checked').get('states_ns'),
            dict(unknown=0, nonrunnable=0, runnable=0, scheduled=10))
        self.assertEqual(interval(result, 'api_end_to_observer').get('states_ns', {}).get('scheduled'), 10)

    def test_left_right_censor_and_pending_Get_remain_explicit(self):
        meta, raw = fixture([e(120, IN), e(140, ENTRY, 1)], pending=[1]+[0]*7)
        result = analyze(meta, raw, [row(scheduled=90, begin=135, end=210, observer=220)])
        self.assertEqual(selected(result)['get'].get('status'), 'right_censored')
        self.assertTrue(interval(result, 'scheduled_to_checked').get('left_censored'))
        self.assertTrue(interval(result, 'api_end_to_observer').get('right_censored'))
        self.assertEqual(interval(result, 'api_end_to_observer').get('states_ns', {}).get('unknown'), 10)

    def test_missing_wake_and_double_switch_in_never_invent_state(self):
        for events, reason in [([e(110, OUT, 1), e(130, IN)], 'missing_wake'),
                               ([e(100, IN), e(110, IN)], 'double_switch_in')]:
            with self.subTest(reason=reason):
                meta, raw = fixture(events)
                result = analyze(meta, raw, [row(scheduled=115, begin=125)])
                self.assertTrue(issue(result, reason))
                self.assertEqual(interval(result, 'scheduled_to_checked').get('states_ns', {}).get('unknown'), 10)

    def test_ties_are_unknown_but_distinct_times_can_be_reordered(self):
        meta, raw = fixture([e(110, OUT, 1), e(110, IN)])
        result = analyze(meta, raw, [row(scheduled=115, begin=125)])
        self.assertTrue(issue(result, 'tied'))
        self.assertEqual(interval(result, 'scheduled_to_checked').get('states_ns', {}).get('unknown'), 10)
        meta, raw = fixture([e(120, WAKE), e(110, OUT, 1), e(130, IN)])
        result = analyze(meta, raw, [row(scheduled=111, begin=119)])
        self.assertEqual(result['reordered_owners'], [0])
        self.assertEqual(interval(result, 'scheduled_to_checked').get('states_ns', {}).get('nonrunnable'), 8)

    def test_overflow_and_torn_commit_invalidate_conservation(self):
        meta, raw = fixture([]); meta['owner_attempted_counts'][0] = 65537; meta['bad'][3] = 1
        result = analyze(meta, raw, [row()])
        self.assertFalse(result['integrity']['complete'])
        self.assertTrue(issue(result, 'overflow'))
        meta, raw = fixture([e(100, IN), e(110, OUT, 0)])
        broken = bytearray(raw); struct.pack_into('<Q', broken, 32+8, 0)
        result = analyze(meta, bytes(broken), [row()])
        self.assertTrue(issue(result, 'commit'))
        self.assertEqual(interval(result, 'scheduled_to_checked').get('states_ns', {}).get('unknown'), 30)

    def test_nested_or_orphan_Get_is_not_a_nearest_pair(self):
        for events in [[e(100, IN), e(136, ENTRY, 1), e(137, ENTRY, 2, 2), e(145, RETURN, 2, 2), e(150, RETURN, 1)],
                       [e(100, IN), e(145, RETURN, 7, 2)]]:
            meta, raw = fixture(events)
            result = analyze(meta, raw, [row()])
            self.assertIn(selected(result)['get'].get('status'), ['ambiguous', 'missing'])
            self.assertFalse(interval(result, 'get_entry_to_return').get('available', False))

    def test_after_rejection_interval_does_not_explain_other_owner(self):
        meta, raw = fixture([e(100, IN, owner=1), e(120, OUT, 0, owner=1),
            e(130, IN, owner=1), e(135, ENTRY, 1, owner=1), e(155, RETURN, 1, owner=1)])
        rows = [row(0, scheduled=110, begin=125, state='rejected_late'),
                row(1, caller=1, scheduled=105, begin=130, end=160, observer=165),
                row(8, scheduled=180, begin=None, state='cancelled_window')]
        result = analyze(meta, raw, rows)
        self.assertEqual(len(result['rows']), 3)
        self.assertEqual(selected(result)['get'].get('status'), 'no_API')
        self.assertEqual(selected(result, 8)['get'].get('status'), 'no_API')
        self.assertEqual(interval(result, 'get_entry_to_return', 1, 1).get('after_rejection_check_ns'), 20)
        self.assertFalse(result['causal_attribution'])

    def test_clock_unverified_forbids_precise_ledger_intersection(self):
        meta, raw = fixture([e(100, IN)]); meta['clock_alignment_verified'] = False
        result = analyze(meta, raw, [row()])
        self.assertIsNone(interval(result, 'scheduled_to_checked').get('states_ns'))
        self.assertTrue(issue(result, 'clock'))

    def test_unsealed_bad_and_outside_cutoff_never_claim_complete(self):
        for field, value in [('gate_closed_drained', False), ('bad', [0,1,0,0]), ('detach_errors', ['not detached'])]:
            meta, raw = fixture([e(100, IN)]); meta[field] = value
            with self.subTest(field=field):
                self.assertFalse(analyze(meta, raw, [row()])['integrity']['complete'])
        meta, raw = fixture([e(200, IN)])
        self.assertTrue(issue(analyze(meta, raw, [row()]), 'cutoff'))

    def test_Get_anchor_contradicts_sleep_or_other_running_CPU(self):
        cases = [[e(100, IN), e(120, OUT, 1), e(135, ENTRY, 1), e(145, RETURN, 1), e(150, WAKE), e(160, IN)],
                 [e(100, IN, cpu=1), e(135, ENTRY, 1, cpu=2), e(145, RETURN, 1, cpu=2), e(160, OUT, 1, cpu=1)]]
        for events in cases:
            with self.subTest(events=events):
                meta, raw = fixture(events)
                result = analyze(meta, raw, [row(begin=130, end=155)])
                self.assertTrue(issue(result, 'Get_anchor_state_conflict'))
                self.assertFalse(result['integrity']['complete'])
                self.assertEqual(interval(result, 'get_entry_to_return').get('states_ns', {}).get('unknown'), 10)
                self.assertFalse(interval(result, 'get_entry_to_return').get('attribution_qualified'))

    def test_complete_pair_must_be_uniquely_contained_in_API(self):
        meta, raw = fixture([e(100, IN), e(120, ENTRY, 1), e(150, RETURN, 1)])
        result = analyze(meta, raw, [row(begin=135)])
        self.assertEqual(selected(result)['get'].get('status'), 'missing')
        self.assertTrue(issue(result, 'outside_API'))
        self.assertFalse(interval(result, 'api_begin_to_get_entry').get('available', False))


if __name__ == '__main__':
    unittest.main()
