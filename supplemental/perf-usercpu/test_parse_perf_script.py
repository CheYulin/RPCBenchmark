import io
import json
import struct
import unittest
from parse_perf_script import summarize_script, record_metadata, warning_counts


def sample(pid, period, frames):
    return f'{pid}/ {pid+1} {period} cpu-clock:u:\n'+''.join(f'  7fff0010 {name} (/private/synthetic.so)\n'for name in frames)+'\n'


def data(records, endian='<', suffix=b''):
    header=bytearray(104);header[:8]=b'PERFILE2'if endian=='<'else b'2ELIFREP'
    struct.pack_into(endian+'6Q',header,8,104,0,104,0,104,len(records)+len(suffix))
    return io.BytesIO(header+records+suffix)


def event(kind, payload=b'', endian='<'):
    return struct.pack(endian+'IHH',kind,0,len(payload)+8)+payload


class ParserTests(unittest.TestCase):
    def test_role_leaf_inclusive_period_and_recursive_presence(self):
        text=sample(101,10,['Leaf','Parent','Parent'])+sample(101,20,['Other','Parent'])+sample(202,5,['WorkerLeaf'])
        result=summarize_script(io.StringIO(text),{101:'SDK',202:'Worker'})
        self.assertEqual(result['roles']['SDK']['sample_count'],2)
        self.assertEqual(result['roles']['SDK']['period_weight'],30)
        self.assertEqual(result['roles']['SDK']['leaf']['rows'],[dict(symbol='Other',samples=1,period_weight=20),dict(symbol='Leaf',samples=1,period_weight=10)])
        self.assertEqual(result['roles']['SDK']['inclusive']['rows'][0],dict(symbol='Parent',samples=2,period_weight=30))
        self.assertTrue(result['inclusive_nonadditive'])

    def test_foreign_tgid_excluded_and_no_identity_export(self):
        result=summarize_script(io.StringIO(sample(101,9,['Good'])+sample(303,17,['Foreign'])),{101:'SDK',202:'Worker'})
        self.assertEqual(result['excluded']['foreign_samples'],1)
        self.assertEqual(result['excluded']['foreign_period_weight'],17)
        self.assertEqual(result['roles']['SDK']['sample_count'],1)
        self.assertNotIn('303',json.dumps(result))
        self.assertNotIn('Foreign',json.dumps(result))

    def test_unknown_redaction_offsets_and_depth_censoring(self):
        text=sample(101,10,['[unknown]','foo+0x12','/home/private/path'])+sample(202,4,['deadbeef'])
        result=summarize_script(io.StringIO(text),{101:'SDK',202:'Worker'},max_stack=3)
        self.assertEqual(result['roles']['SDK']['unknown_leaf_samples'],1)
        self.assertEqual(result['roles']['SDK']['depth_limit_samples'],1)
        self.assertEqual(result['roles']['SDK']['redacted_frames'],1)
        raw=json.dumps(result)
        for private in ('/home/','/private/','0x12','deadbeef','7fff0010'):self.assertNotIn(private,raw)
        self.assertIn('foo',raw)

    def test_missing_chain_and_inline_fallback(self):
        text='101/102 8 cpu-clock:u:\n\n202/203 3 cpu-clock:u: 7fff Single (/private/dso)\n\n'
        result=summarize_script(io.StringIO(text),{101:'SDK',202:'Worker'})
        self.assertEqual(result['roles']['SDK']['empty_chain_samples'],1)
        self.assertEqual(result['roles']['Worker']['leaf']['rows'],[dict(symbol='Single',samples=1,period_weight=3)])

    def test_loss_marker_inside_symbol_is_not_metadata(self):
        result=summarize_script(io.StringIO(sample(101,5,['handle_PERF_RECORD_LOST'])),{101:'SDK',202:'Worker'})
        self.assertEqual(result['roles']['SDK']['leaf']['rows'],[dict(symbol='handle_PERF_RECORD_LOST',samples=1,period_weight=5)])
        self.assertEqual(result['format_counts']['script_lost_records'],0)

    def test_bound_output_keeps_other_mass_and_rejects_malformed(self):
        result=summarize_script(io.StringIO(sample(101,2,['A'])+sample(101,3,['B'])),{101:'SDK',202:'Worker'},max_symbols=1)
        self.assertEqual(result['roles']['SDK']['leaf']['other'],dict(symbols=1,samples=1,period_weight=2))
        with self.assertRaises(ValueError):summarize_script(io.StringIO('  7fff orphan\n'),{101:'SDK',202:'Worker'})
        with self.assertRaises(ValueError):summarize_script(io.StringIO('garbage private line\n'),{101:'SDK',202:'Worker'})

    def test_record_loss_throttle_and_byteorder(self):
        for e in ('<','>'):
            records=event(2,struct.pack(e+'QQ',99,7),e)+event(5,b'\0'*24,e)+event(6,b'\0'*24,e)+event(13,struct.pack(e+'Q',3),e)+event(9,b'\0'*8,e)
            result=record_metadata(data(records,e))
            self.assertEqual(result['lost_samples'],10)
            self.assertEqual(result['throttle_records'],1)
            self.assertEqual(result['unthrottle_records'],1)
            self.assertEqual(result['sample_records'],1)
            self.assertTrue(result['record_stream_complete'])

    def test_truncated_or_compressed_metadata_is_explicit(self):
        result=record_metadata(data(event(9,b'\0'*8),suffix=b'bad'))
        self.assertFalse(result['record_stream_complete'])
        self.assertEqual(result['truncated_record_tails'],1)
        result=record_metadata(data(event(81,b'compressed')))
        self.assertFalse(result['record_stream_complete'])
        self.assertEqual(result['opaque_compressed_records'],1)
        with self.assertRaises(ValueError):record_metadata(io.BytesIO(b'not-perf'))

    def test_warning_rollup_retains_uncertainty_without_private_text(self):
        result=warning_counts('Failed to unwind /home/private/lib.so\ntruncated stack at 0xabc\nLOST 3 events\nthrottled\nunknown symbol\n')
        self.assertEqual(result['unwind_related_lines'],1)
        self.assertEqual(result['truncation_related_lines'],1)
        self.assertEqual(result['loss_related_lines'],1)
        self.assertEqual(result['throttle_related_lines'],1)
        self.assertNotIn('/home/',json.dumps(result))
        self.assertFalse(result['unwind_completeness_proven'])


if __name__=='__main__':unittest.main()
