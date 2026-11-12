import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from sampled_observer import SampledObserver
class Observer(unittest.TestCase):
 def test_original_identity_and_epoch_not_extended(self):
  x=SampledObserver(SimpleNamespace(record={},bind=Mock()))
  with patch('sampled_observer.task',return_value={'startticks':2}),self.assertRaisesRegex(RuntimeError,'owner changed'):
   x.phase(SimpleNamespace(pid=10),{'identity':{'startticks':1}},[],{'phase_id':1,'target':{'pid':10},'plan':{'epoch_ns':100}})
  x.exact.bind.assert_not_called()
  with patch('sampled_observer.task',return_value={'startticks':1}),patch('sampled_observer.time.monotonic_ns',return_value=100),self.assertRaisesRegex(RuntimeError,'epoch missed'):
   x.phase(SimpleNamespace(pid=10),{'identity':{'startticks':1}},[],{'phase_id':1,'target':{'pid':10},'plan':{'epoch_ns':100}})
  self.assertEqual(x.phases,[])
 def test_coverage_requires_closed_measure_and_kernel_accounting(self):
  with tempfile.TemporaryDirectory()as d:
   rec=dict(kernel_accounting_valid=True,capture_start_ns=10,capture_stop_ns=100,API_active_at_stop=[],pending_right_censored=[])
   exact=SimpleNamespace(record=rec,out=Path(d),close=Mock());x=SampledObserver(exact);x.sdk=SimpleNamespace(poll=lambda:0);x.phases=[{'phase_id':2,'plan':{'epoch_ns':20,'end_ns':90}}]
   x.stop();x.stop();self.assertTrue(rec['full_measure_coverage']);exact.close.assert_called_once_with(partial=False)
 def test_running_or_crashed_target_cannot_claim_full_capture(self):
  for code in [None,1,-9]:
   with self.subTest(code=code),tempfile.TemporaryDirectory()as d:
    rec=dict(kernel_accounting_valid=True,capture_start_ns=10,capture_stop_ns=100);exact=SimpleNamespace(record=rec,out=Path(d),close=Mock());x=SampledObserver(exact);x.sdk=SimpleNamespace(poll=lambda:code);x.phases=[{'phase_id':2,'plan':{'epoch_ns':20,'end_ns':90}}];x.stop();self.assertFalse(rec['full_measure_coverage']);exact.close.assert_called_once_with(partial=True)
 def test_orphan_wake_keeps_coverage_unresolved(self):
  with tempfile.TemporaryDirectory()as d:
   rec=dict(kernel_accounting_valid=True,capture_start_ns=10,capture_stop_ns=100,wake_pending_right_censored=[{'wake_ns':50}]);exact=SimpleNamespace(record=rec,out=Path(d),close=Mock());x=SampledObserver(exact);x.sdk=SimpleNamespace(poll=lambda:0);x.phases=[{'phase_id':2,'plan':{'epoch_ns':20,'end_ns':90}}];x.stop();self.assertFalse(rec['full_measure_coverage'])
if __name__=='__main__':unittest.main()
