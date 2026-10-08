"""Focused checks for fixed inputs, labels, shared math and metric direction."""
import importlib.util
from pathlib import Path
import unittest

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('parity_summary',HERE/'summarize.py')
summary=importlib.util.module_from_spec(spec); spec.loader.exec_module(summary)
respec=importlib.util.spec_from_file_location('parity_rescore',HERE/'rescore.py')
rescore=importlib.util.module_from_spec(respec); respec.loader.exec_module(rescore)
runspec=importlib.util.spec_from_file_location('parity_run',HERE/'run.py')
runner=importlib.util.module_from_spec(runspec); runspec.loader.exec_module(runner)

class ParitySummaryTests(unittest.TestCase):
 def test_lower_purity_is_positive_contamination_risk(self):
    labels=[True,True,False,False]; scores=[.2,.3,.8,.9]
    self.assertEqual(summary.auc(labels,scores),1.0); self.assertEqual(summary.ap(labels,scores),1.0)

 def test_failures_are_not_scored_as_clean(self):
    rows=[{'label':False,'raw_sp_score':None,'failure':'embedding_failed'}]
    self.assertEqual(summary.metrics(rows)['successfully_scored'],0)

 def test_bootstrap_resamples_sources_with_all_clips(self):
    a=[]; b=[]
    for source, labels in [('s1',[False,True]),('s2',[False,True])]:
        for ix,label in enumerate(labels):
            a.append({'source_id':source,'clip_id':str(ix),'label':label,'raw_sp_score':.2 if label else .8,'failure':None})
            b.append({'source_id':source,'clip_id':str(ix),'label':label,'raw_sp_score':.25 if label else .75,'failure':None})
    result,paired=summary.boot_delta(a,b,nboot=20)
    self.assertEqual(result['sources'],2); self.assertEqual(result['paired_scored_clip_count'],4)
    self.assertGreater(result['definitions']['any_overlap']['bootstrap_replicates_valid'],0)

 def test_clip_set_mismatch_is_detectable(self):
    a={('s','1'):{'label':True}}; b={('s','2'):{'label':True}}
    with self.assertRaises(ValueError): summary.validate_clip_maps(a,b)
    b={('s','1'):{'label':False}}
    with self.assertRaises(ValueError): summary.validate_clip_maps(a,b)
    b={('s','1'):{'label':True}}
    summary.validate_clip_maps(a,b)

 def test_backend_input_audio_and_ordered_ids_must_match(self):
    a={'input_hashes':{'enroll:s:e0':'same','window:s/c:0001':'same'}}
    b={'input_hashes':{'enroll:s:e0':'same','window:s/c:0001':'same'}}
    summary.validate_exact_inputs(a,b)
    b['input_hashes']['window:s/c:0001']='different'
    with self.assertRaises(ValueError): summary.validate_exact_inputs(a,b)
    b['input_hashes'].pop('window:s/c:0001')
    with self.assertRaises(ValueError): summary.validate_exact_inputs(a,b)

 def test_only_finite_nonzero_192d_vectors_reach_centroid_math(self):
    import numpy as np
    v=rescore.checked_embedding(np.ones(192), 'sample')
    self.assertEqual(v.shape,(192,)); self.assertTrue(np.isfinite(v).all())
    for bad in (np.zeros(192),np.full(192,np.nan),np.full(192,np.inf),np.ones(191)):
        with self.assertRaises(ValueError): rescore.checked_embedding(bad,'bad')

 def test_both_backends_use_one_production_scoring_function(self):
    source=(HERE/'rescore.py').read_text()
    self.assertEqual(source.count('score_candidate_clip('),1) # one shared invocation for either backend
    self.assertIn('from speechcraft_dataset.eval_speaker_purity import',source)

 def test_gold_is_not_read_by_embedding_extraction(self):
    source=(HERE/'run.py').read_text()
    extraction=source[source.index('    def one('):source.index('    for source_id, row, path in enroll:')]
    self.assertIn('provider.embed(samples)',extraction)
    self.assertNotIn('gold_',extraction)
    extraction_done=source.index('    extraction_sec = time.perf_counter() - started')
    self.assertGreater(source.index('gold_clean'),extraction_done)
    self.assertGreater(source.index('gold_wrong_speaker'),extraction_done)

 def test_clip_and_enrollment_order_is_deterministic(self):
    clips=[{'source_id':'b','clip_id':'c2'},{'source_id':'a','clip_id':'c2'},{'source_id':'a','clip_id':'c1'}]
    ordered=runner.ordered_rows(clips,'source_id','clip_id')
    self.assertEqual([(r['source_id'],r['clip_id']) for r in ordered],[('a','c1'),('a','c2'),('b','c2')])
    chunks=[{'chunk_id':'enroll_002'},{'chunk_id':'enroll_000'},{'chunk_id':'enroll_001'}]
    self.assertEqual([r['chunk_id'] for r in runner.ordered_rows(chunks,'chunk_id')],['enroll_000','enroll_001','enroll_002'])

if __name__=='__main__': unittest.main()
