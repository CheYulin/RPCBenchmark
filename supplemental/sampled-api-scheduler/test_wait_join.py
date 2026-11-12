import unittest
from wait_join import join_waits
class Join(unittest.TestCase):
 def row(self,out=10,wake=20,end=30,state=1,duplicate=False):return dict(out_ns=out,wake_ns=wake,in_ns=end,state=state,duplicate=duplicate)
 def test_same_rpc_clips_known_blocked_and_runnable(self):
  r=join_waits((15,25),[self.row()]);self.assertEqual((r['offcpu_ns'],r['known_blocked_ns'],r['runnable_lower_ns'],r['unknown_split_ns']),(10,5,5,0))
 def test_unknown_is_not_zero_or_falsely_blocked(self):
  r=join_waits((10,40),[self.row(wake=0)]);self.assertEqual((r['offcpu_ns'],r['runnable_lower_ns'],r['unknown_split_ns'],r['unaccounted_rpc_ns']),(20,0,20,10))
 def test_runnable_preempt_and_nonoverlap(self):
  r=join_waits((15,25),[self.row(wake=0,state=0),self.row(out=30,wake=35,end=40)]);self.assertEqual((r['offcpu_ns'],r['known_blocked_ns'],r['runnable_lower_ns']),(10,0,10))
 def test_overlap_and_bad_rpc_fail_closed(self):
  with self.assertRaises(ValueError):join_waits((10,40),[self.row(),self.row(out=29,wake=35,end=40)])
  with self.assertRaises(ValueError):join_waits((20,10),[])
 def test_empty_rows_conserved(self):
  self.assertEqual(join_waits((10,20),[])['unaccounted_rpc_ns'],10)
if __name__=='__main__':unittest.main()
