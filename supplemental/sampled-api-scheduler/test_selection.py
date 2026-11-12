import unittest
from selection import selected
class Selection(unittest.TestCase):
 def test_unsigned_wrap_and_repeatability(self):
  self.assertEqual(selected(1,2**64-1),selected(1,-1))
  self.assertEqual(selected(1,2**64),selected(1,0))
 def test_fixture_plan_has_both_selected_and_unselected(self):
  ids=[i for i in range(4096)if selected(1,i)]
  self.assertGreater(len(ids),30);self.assertLess(len(ids),100)
 def test_only_frozen_phases(self):
  for phase in [-1,0,3]:
   with self.assertRaises(ValueError):selected(phase,1)
if __name__=='__main__':unittest.main()
