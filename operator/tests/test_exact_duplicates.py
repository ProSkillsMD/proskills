import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('dedupe',Path(__file__).parents[1]/'scripts/backlog/close_exact_duplicates.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def issue(n,**kw):
    d={'number':n,'title':'same','body':'same body','created_at':'2026-01-01','state':'open','comments':0,'assignees':[],'milestone':None,'labels':[{'name':'submission'},{'name':'auto-discovered'}]};d.update(kw);return d
class ExactDuplicates(unittest.TestCase):
    def test_retains_oldest(self):self.assertEqual(m.make_plan([issue(2),issue(1)])[0]['canonical'],1)
    def test_changed_body_not_duplicate(self):self.assertEqual(m.make_plan([issue(1),issue(2,body='new version')]),[])
    def test_preserve_reviews_and_holds(self):
        for d in [issue(2,comments=1),issue(2,assignees=[{}]),issue(714),issue(2,labels=[{'name':'rocket-pass'}])]:
            self.assertEqual(m.make_plan([issue(1),d]),[])
    def test_skip_changed_live_evidence(self):
        with tempfile.TemporaryDirectory() as d,patch.object(m,'api',side_effect=[issue(2,body='changed'),issue(1)]) as api:
            out=m.apply_plan(m.make_plan([issue(1),issue(2)]),Path(d)/'journal')
            self.assertEqual(out['closed'],0);self.assertEqual(api.call_count,2)
    def test_closes_with_journal_no_comment(self):
        with tempfile.TemporaryDirectory() as d,patch.object(m,'api',side_effect=[issue(2),issue(1),{'state':'closed'}]) as api:
            j=Path(d)/'journal';out=m.apply_plan(m.make_plan([issue(1),issue(2)]),j,limit=1)
            self.assertEqual(out['closed'],1);self.assertIn('original',j.read_text())
            self.assertEqual(api.call_args.args[1]['state_reason'],'not_planned')
