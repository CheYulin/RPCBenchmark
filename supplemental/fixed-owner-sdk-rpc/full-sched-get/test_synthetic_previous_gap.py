import struct
import unittest
from analyze_full_sched import analyze
from test_analyze_full_sched import fixture, e, row, selected, IN, ENTRY, RETURN


def scenario(scheduled=150):
    metadata, raw = fixture([e(100, IN), e(120, ENTRY, 1), e(130, RETURN, 1)])
    rows = [row(begin=115, end=135, observer=165),
            row(8, scheduled=scheduled, begin=None, state='cancelled_window')]
    return metadata, raw, rows


class SyntheticPreviousGap(unittest.TestCase):
    def test_cancelled_overlap_retains_sign_and_original_endpoints(self):
        result = analyze(*scenario())
        self.assertTrue(result['integrity']['complete'], result['integrity']['issues'])
        cancelled = selected(result, 8)
        span = cancelled['intervals']['previous_observer_to_scheduled']
        self.assertEqual(span.get('recorded_endpoints_ns'), [165, 150])
        self.assertEqual(span.get('signed_gap_ns'), -15)
        self.assertEqual(span.get('orientation'), 'scheduled_to_previous_observer_overlap')
        self.assertEqual([span['start_ns'], span['end_ns']], [150, 165])
        self.assertEqual(sum(span['states_ns'].values()), 15)
        self.assertEqual(cancelled['get']['status'], 'no_API')
        self.assertEqual(len(result['rows']), 2)
        self.assertEqual([p['pair_id'] for p in result['Get_pairs']], [1])

    def test_positive_gap_keeps_forward_orientation(self):
        result = analyze(*scenario(scheduled=180))
        span = selected(result, 8)['intervals']['previous_observer_to_scheduled']
        self.assertEqual(span.get('signed_gap_ns'), 15)
        self.assertEqual(span.get('orientation'), 'previous_observer_to_scheduled_gap')
        self.assertEqual(span.get('recorded_endpoints_ns'), [165, 180])
        self.assertEqual(span['duration_ns'], 15)

    def test_reversed_API_still_rejects_pair_containment(self):
        metadata, raw, rows = scenario()
        rows[0]['api_end_ns'] = rows[0]['api_begin_ns'] - 1
        result = analyze(metadata, raw, rows)
        self.assertFalse(result['integrity']['complete'])
        self.assertTrue(any(i['code'] == 'Get_pair_outside_API_or_ambiguous'
                            for i in result['integrity']['issues']))
        self.assertNotEqual(selected(result)['get']['status'], 'matched')

    def test_reversed_Get_still_rejects_exact_pair(self):
        metadata, raw, rows = scenario()
        data = bytearray(raw)
        struct.pack_into('<Q', data, 64, 119)
        result = analyze(metadata, bytes(data), rows)
        self.assertFalse(result['integrity']['complete'])
        self.assertTrue(any(i['code'] == 'ambiguous_Get_pair'
                            for i in result['integrity']['issues']))
        self.assertNotEqual(selected(result)['get']['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
