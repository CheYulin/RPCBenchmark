import os
import unittest
from gate_model import Gate, UnsafeLegacyGate, Reservation, OPEN, PHASE, FAULT, COUNT


class GateRaces(unittest.TestCase):
    GateType = UnsafeLegacyGate if os.environ.get('OWNER8_GATE_RED') == '1' else Gate

    def test_pause_before_CAS_cannot_write_after_seal(self):
        gate = self.GateType()
        gate.open(1)
        observed = gate.prepare(0)
        self.assertTrue(gate.seal(1))
        self.assertIsNone(gate.enter(0, 1, observed))

    def test_held_writer_blocks_snapshot_until_release(self):
        gate = self.GateType()
        gate.open(1)
        lease = gate.enter(0, 1)
        self.assertFalse(gate.seal(1))
        writes = []
        gate.mutate(lease, writes, 'before release')
        gate.leave(lease)
        self.assertTrue(gate.drained())
        self.assertEqual(writes, ['before release'])

    def test_old_phase_does_not_borrow_new_open(self):
        gate = self.GateType()
        gate.open(1)
        observed = gate.prepare(0)
        self.assertTrue(gate.seal(1))
        gate.open(2)
        self.assertIsNone(gate.enter(0, 1, observed))

    def test_partial_open_always_seals_all_CPU_slots(self):
        gate = self.GateType(cpus=8)
        with self.assertRaisesRegex(ValueError, 'partial'):
            gate.open(1, fail_after=3)
        self.assertTrue(gate.seal(1))
        self.assertEqual(gate.words, [0] * 8)

    def test_seal_between_increment_and_authorization_cannot_grant_writer(self):
        gate = self.GateType()
        gate.open(2)
        seal_results = []
        lease = gate.enter(0, 2, after_increment=lambda: seal_results.append(gate.seal(2)))
        self.assertIsNone(lease)
        self.assertEqual(seal_results, [False])
        self.assertTrue(gate.drained())


class GateBoundaries(unittest.TestCase):
    def test_fallback_acquires_without_losing_other_lease(self):
        gate = Gate()
        gate.open(1)
        observed = gate.prepare(0)
        first = gate.enter(0, 1)
        second = gate.enter(0, 1, observed)
        self.assertEqual(gate.words[0] & COUNT, 2)
        self.assertFalse(gate.seal(1))
        gate.leave(first)
        self.assertFalse(gate.drained())
        gate.leave(second)
        self.assertTrue(gate.drained())

    def test_seal_between_failed_CAS_and_fallback_rolls_back(self):
        gate = Gate()
        gate.open(1)
        observed = gate.prepare(0)
        first = gate.enter(0, 1)
        self.assertIsNone(gate.enter(0, 1, observed, lambda: gate.seal(1)))
        self.assertEqual(gate.words[0] & COUNT, 1)
        gate.leave(first)
        self.assertTrue(gate.drained())

    def test_repeated_close_and_open_do_not_reset_counters(self):
        gate = Gate()
        gate.open(1)
        lease = gate.enter(0, 1)
        self.assertFalse(gate.seal(1))
        self.assertFalse(gate.seal(1))
        self.assertEqual(gate.words[0] & COUNT, 1)
        gate.leave(lease)
        with self.assertRaises(ValueError):
            gate.open(1)
        gate.open(2)
        with self.assertRaises(ValueError):
            gate.open(2)

    def test_count_overflow_is_faulted_never_a_drained_snapshot(self):
        gate = Gate()
        gate.open(1)
        gate.words[0] = OPEN | COUNT
        self.assertIsNone(gate.enter(0, 1))
        self.assertTrue(gate.words[0] & FAULT)
        self.assertFalse(gate.seal(1))

    def test_release_is_consumed_once(self):
        gate = Gate()
        gate.open(1)
        lease = gate.enter(0, 1)
        gate.leave(lease)
        with self.assertRaises(ValueError):
            gate.leave(lease)


class ReservationBoundaries(unittest.TestCase):
    def test_CAS_retry_budget_and_counter_overflow_keep_loss_under_lease(self):
        gate = Gate()
        gate.open(2)
        lease = gate.enter(0, 2)
        bad = []
        counter = Reservation()
        def fifteen_contenders(value, attempt):
            if attempt < 15:
                value.value += 1
        self.assertEqual(counter.reserve(gate, lease, bad, 3, fifteen_contenders), 15)
        self.assertEqual(counter.value, 16)
        counter = Reservation()
        def sixteen_contenders(value, attempt):
            if attempt < 16:
                value.value += 1
        self.assertIsNone(counter.reserve(gate, lease, bad, 3, sixteen_contenders,
                                         os.environ.get('OWNER8_RESERVE_RED') == '1'))
        self.assertEqual(counter.value, 16)
        self.assertEqual(bad, [3])
        maximum = Reservation((1 << 64) - 1)
        self.assertIsNone(maximum.reserve(gate, lease, bad, 2))
        self.assertEqual(maximum.value, (1 << 64) - 1)
        self.assertEqual(bad, [3, 2])
        self.assertFalse(gate.seal(2))
        gate.leave(lease)
        self.assertTrue(gate.drained())


class LoaderHelpers(unittest.TestCase):
    def test_owned_SDK_leader_gets_only_one_entry_return_pair(self):
        import owned_sched8 as loader
        plan = loader.uprobe_plan(987, '/object-fixture')
        self.assertEqual(len(plan), 2)
        self.assertEqual({(row['attach_pid'], row['return_probe']) for row in plan},
                         {(987, 0), (987, 1)})
        self.assertEqual({row['function'] for row in plan}, {b'on_get_entry', b'on_get_return'})
        self.assertTrue(all(row['symbol'] == loader.GET_SYMBOL and
                            row['object_path'] == '/object-fixture' for row in plan))
        for invalid in (None, 0, -1, True, 1.5, '987', list(range(1001, 1009))):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, 'positive owned SDK leader PID'):
                    loader.uprobe_plan(invalid, '/object-fixture')

    def test_failed_close_still_detaches_and_never_recloses_fd(self):
        import owned_sched8 as loader
        links = [dict(fd=42, event=b'own-event')]
        events = []
        def close(fd):
            events.append(('close', fd))
            raise OSError('injected close error')
        def detach(name):
            events.append(('detach', name))
            return 0
        self.assertEqual(len(loader.close_uprobes(links, close, detach)), 1)
        self.assertEqual(loader.close_uprobes(links, close, detach), [])
        self.assertEqual(events, [('close', 42), ('detach', b'own-event')])

    def test_cold_test_run_has_64B_skb_and_zero_context_flags(self):
        import ctypes as ct
        import owned_sched8 as loader
        def invoke(attr):
            self.assertEqual(attr.prog_fd, 19)
            self.assertEqual(attr.data_size_in, 64)
            self.assertEqual(attr.data_size_out, 64)
            self.assertEqual(attr.repeat, 1)
            self.assertEqual((attr.flags, attr.ctx_size_in, attr.ctx_size_out), (0, 0, 0))
            self.assertEqual(ct.string_at(attr.data_in, 64), bytes(64))
            attr.retval = 1
            return 0
        self.assertEqual(loader.test_run_control(19, invoke), 1)

    def test_typed8_does_not_accept_old32_receipt(self):
        import owned_sched8 as loader
        with self.assertRaisesRegex(ValueError, 'schema'):
            loader.validate_inputs(dict(schema='OWNED_SDK32_COLD_READY_RUNQUEUE_V1'), [])


if __name__ == '__main__':
    unittest.main()
