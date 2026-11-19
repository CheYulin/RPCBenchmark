import copy,unittest
from calibration import evaluate

def rows():
 result=[]
 for smoke,op,mode in [(True,o,m)for o in ['get','set']for m in ['off','enabled']]+[(False,o,m)for o in ['get','set']for m in ['off','enabled','enabled','off']]:
  phase=dict(phase_id=2,api_p50_upper_us=100,api_p99_upper_us=200,observer_p50_upper_us=100,observer_p99_upper_us=200,CPU_us_per_success=100,qps=10000)
  result.append(dict(smoke=smoke,operation=op,mode=mode,status='SDK_DIAGNOSTIC_VALID',metric={'phases':[phase]}))
 return result
class Gate(unittest.TestCase):
 def test_full_matrix_pass_remains_not_RPC30(self):
  d=evaluate(rows());self.assertTrue(d['observer_calibrated']);self.assertFalse(d['RPC30_accepted'])
 def test_missing_slot(self):self.assertFalse(evaluate(rows()[:-1])['observer_calibrated'])
 def test_bad_quality(self):
  r=rows();r[3]['status']='FAILED';self.assertFalse(evaluate(r)['observer_calibrated'])
 def test_wrong_order(self):
  r=rows();r[4],r[5]=r[5],r[4];self.assertFalse(evaluate(r)['observer_calibrated'])
 def test_one_direction_regression_even_good_median(self):
  r=rows();r[5]['metric']['phases'][0]['api_p99_upper_us']=208;r[6]['metric']['phases'][0]['api_p99_upper_us']=180;self.assertFalse(evaluate(r)['observer_calibrated'])
 def test_CPU_gate(self):
  r=rows();r[6]['metric']['phases'][0]['CPU_us_per_success']=104;self.assertFalse(evaluate(r)['observer_calibrated'])
 def test_QPS_gate(self):
  r=rows();r[5]['metric']['phases'][0]['qps']=9600;self.assertFalse(evaluate(r)['observer_calibrated'])
 def test_observer_span_gate(self):
  r=rows();r[5]['metric']['phases'][0]['observer_p99_upper_us']=208;self.assertFalse(evaluate(r)['observer_calibrated'])
 def test_control_drift_even_on_ratios_favorable(self):
  r=rows();r[7]['metric']['phases'][0]['api_p99_upper_us']=220;self.assertFalse(evaluate(r)['observer_calibrated'])
if __name__=='__main__':unittest.main()
