import unittest
from interval_model import split, overlap
class TestIntervals(unittest.TestCase):
 def test_runnable_preemption(self):self.assertEqual(split(10,0,30,0,False),{'offcpu':(10,30),'blocked':None,'runnable':(10,30)})
 def test_sleep_wake_run(self):self.assertEqual(split(10,25,30,1,False),{'offcpu':(10,30),'blocked':(10,25),'runnable':(25,30)})
 def test_missing_wake_unknown(self):self.assertIsNone(split(10,0,30,1,False)['runnable'])
 def test_duplicate_wake_unknown(self):self.assertIsNone(split(10,20,30,1,True)['runnable'])
 def test_invalid_order(self):
  for args in ((0,10,20,1,False),(20,10,30,1,False),(10,40,30,1,False),(20,0,10,0,False)):
   with self.assertRaises(ValueError):split(*args)
 def test_clip_partial_rpc(self):self.assertEqual(overlap((10,30),(20,40)),10)
 def test_no_overlap(self):self.assertEqual(overlap((10,20),(20,40)),0)
 def test_unknown_not_zero(self):self.assertIsNone(overlap(None,(20,40)))
if __name__=='__main__':unittest.main()
class TestTargets(unittest.TestCase):
 def test_positive_owned_shape(self):
  from exact_profile import validate_targets
  validate_targets(100,list(range(101,133)))
 def test_reject_global_or_duplicate_shape(self):
  from exact_profile import validate_targets
  for pid,tids in ((-1,list(range(101,133))),(100,[101]*32),(100,list(range(101,132))),(True,list(range(101,133)))):
   with self.assertRaises(ValueError):validate_targets(pid,tids)
class TestJoinedObserver(unittest.TestCase):
 def test_nested_original_phase_plan(self):
  from exact_profile import JoinedObserver
  from types import SimpleNamespace
  calls=[]
  inner=SimpleNamespace(pod=object(),phase=lambda *a:calls.append('pod'))
  exact=SimpleNamespace(bind=lambda *a:calls.append(a))
  observer=JoinedObserver(inner,exact);sdk=SimpleNamespace(pid=123)
  observer.phase(sdk,{},[1,2],{'phase_id':1,'plan':{'epoch_ns':999}})
  self.assertEqual(calls,[(123,[1,2],999),'pod'])
 def test_stop_closes_pod_after_scheduler_error(self):
  from exact_profile import JoinedObserver
  from types import SimpleNamespace
  calls=[]
  def fail(**kw):raise RuntimeError('scheduler failed')
  observer=JoinedObserver(SimpleNamespace(pod=None,stop=lambda *a:calls.append('closed')),SimpleNamespace(close=fail))
  with self.assertRaises(RuntimeError):observer.stop(False)
  self.assertEqual(calls,['closed'])
class TestIndependentPublication(unittest.TestCase):
 def test_wake_before_out_publication(self):
  from interval_model import publication
  self.assertEqual(publication([('wake',20),('out',10),('in',30)]),publication([('out',10),('wake',20),('in',30)]))
 def test_multiple_wakes_keep_first_and_mark_unknown(self):
  from interval_model import publication
  self.assertIsNone(publication([('out',10),('wake',20),('wake',21),('in',30)])['runnable'])

class TestExitSafeDetach(unittest.TestCase):
 def test_detaches_owned_events_without_symbol_or_PID_lookup(self):
  from exact_profile import detach_owned_uprobes
  from types import SimpleNamespace
  calls=[]
  def bad(*a,**k):raise RuntimeError('PID disappeared')
  bpf=SimpleNamespace(detach_uprobe=bad,detach_uprobe_event=lambda ev:calls.append(ev))
  detach_owned_uprobes(bpf,[b'owned_begin',b'owned_end'])
  self.assertEqual(calls,[b'owned_end',b'owned_begin'])

class TestWakeCensoring(unittest.TestCase):
 def test_prior_wake_is_unknown_without_dropping_offcpu(self):
  from interval_model import normalize_wake
  wake,flag=normalize_wake(10,9,30)
  self.assertEqual((wake,flag),(0,1));self.assertIsNone(split(10,wake,30,1,True)['runnable'])
 def test_future_wake_is_unknown(self):
  from interval_model import normalize_wake
  self.assertEqual(normalize_wake(10,31,30),(0,2))
 def test_in_range_wake_kept(self):
  from interval_model import normalize_wake
  self.assertEqual(normalize_wake(10,20,30),(20,0))
