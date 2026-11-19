"""Conservative observer gate for one preregistered finite-work diagnostic matrix."""
import statistics

def evaluate(rows):
 expected=[(True,o,m)for o in ['get','set']for m in ['off','enabled']]+[(False,o,m)for o in ['get','set']for m in ['off','enabled','enabled','off']]
 if len(rows)!=12 or [(r['smoke'],r['operation'],r['mode'])for r in rows]!=expected or any(r['status']!='SDK_DIAGNOSTIC_VALID'for r in rows):
  return dict(observer_calibrated=False,RPC30_accepted=False,reason='incomplete_or_invalid_fixed_matrix')
 results=[]
 keys=['api_p50_upper_us','api_p99_upper_us','observer_p50_upper_us','observer_p99_upper_us','CPU_us_per_success','qps']
 for op in ['get','set']:
  slots=[r for r in rows if not r['smoke']and r['operation']==op]
  phases=[next(p for p in r['metric']['phases']if p['phase_id']==2)for r in slots]
  pairs=[]
  for control,candidate in [(phases[0],phases[1]),(phases[3],phases[2])]:
   ratios={k:candidate[k]/control[k] for k in keys}
   passed=all(v>=.97 if k=='qps'else v<=1.03 for k,v in ratios.items())
   pairs.append(dict(ratios=ratios,pass_gate=passed))
  median_ratios={k:statistics.median(phases[i][k]for i in [1,2])/statistics.median(phases[i][k]for i in [0,3])for k in keys}
  drift={k:phases[3][k]/phases[0][k]-1 for k in ['api_p99_upper_us','observer_p99_upper_us']}
  passed=all(p['pass_gate']for p in pairs)and all(v>=.97 if k=='qps'else v<=1.03 for k,v in median_ratios.items())and all(abs(v)<=.03 for v in drift.values())
  results.append(dict(operation=op,pairs=pairs,median_ratios=median_ratios,control_drift=drift,pass_gate=passed))
 return dict(observer_calibrated=all(r['pass_gate']for r in results),operations=results,RPC30_accepted=False,limits=['Finiteclosedloop API observer check only; cannot prove internal RPC phase perturbation<=3%.','No bRPC counterpart, fixedofferedload, formalRPC30, or DataSystem performance acceptance in this matrix.','Full cohort retained, with caller completion taper explicitly measured.'])
