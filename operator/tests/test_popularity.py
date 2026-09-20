import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('ranking',Path(__file__).parents[1]/'scripts/backlog/rank_popular.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class PopularityTests(unittest.TestCase):
    def test_no_published_or_archived_or_missing(self):
        rows=[{'number':n,'source':str(n),'disposition':d} for n,d in [(1,'already_published'),(2,'canonical_actionable'),(3,'canonical_actionable'),(4,'canonical_actionable')]]
        meta={'1':{'stargazerCount':100,'forkCount':1},'2':{'stargazerCount':100,'forkCount':1,'isArchived':True},'4':{'stargazerCount':1,'forkCount':0}}
        result=m.rank(rows,meta);self.assertEqual([x['issue'] for x in result],[4]);self.assertTrue(result[0]['reviewRequired'])
    def test_cap_and_order(self):
        rows=[{'number':n,'source':str(n),'disposition':'canonical_actionable'} for n in range(110)]
        meta={str(n):{'stargazerCount':n,'forkCount':0} for n in range(110)}
        result=m.rank(rows,meta);self.assertEqual(len(result),100);self.assertEqual(result[0]['issue'],109)
        with self.assertRaises(ValueError):m.rank(rows,meta,101)

    def test_renamed_repo_is_one_candidate(self):
        rows=[{'number':n,'source':str(n),'disposition':'canonical_actionable'} for n in [1,2]]
        meta={str(n):{'url':'https://github.com/owner/current','stargazerCount':5,'forkCount':0} for n in [1,2]}
        self.assertEqual(len(m.rank(rows,meta)),1)
