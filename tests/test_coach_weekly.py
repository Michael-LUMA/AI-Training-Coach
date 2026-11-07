"""Synthetic offline weekly summaries: no API calls or persistent state."""
import copy
import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coach_weekly import build_weekly_summary

START = '2026-11-02'

def activity(day, load=50, **extra):
    return {'start_date_local': day, 'moving_time':3600, 'distance':30000,
            'icu_training_load':load, 'icu_weighted_avg_watts':175,
            'average_heartrate':130, 'trainer':False, **extra}

def summary(rows, wellness=None, **kwargs):
    return build_weekly_summary(rows, wellness, week_start=START, **kwargs)

class CoachWeeklyTests(unittest.TestCase):
    def test_two_weeks_totals_comparison_and_single_suggestion(self):
        result = summary([activity('2026-10-26',40),activity('2026-11-02',50),activity('2026-11-08',50)],ctl=40,atl=45)
        facts = result['facts']
        self.assertEqual(facts['current']['activity_count'],2)
        self.assertEqual(facts['current']['total_duration_s'],7200)
        self.assertEqual(facts['current']['total_distance_m'],60000)
        self.assertEqual(facts['current']['total_training_load'],100)
        self.assertEqual(facts['comparison']['total_training_load']['delta'],60)
        self.assertEqual(facts['comparison']['total_training_load']['percentage'],150)
        self.assertEqual(facts['reported_context'],{'ctl':40,'atl':45})
        self.assertEqual(len(result['suggestions']),1)
        self.assertTrue(all(item['basis_refs'] for item in result['inferences']))
        json.dumps(result,allow_nan=False)

    def test_single_week_does_not_invent_previous(self):
        result = summary([activity(START)])
        self.assertIsNone(result['facts']['previous'])
        self.assertIsNone(result['facts']['load_trend'])
        self.assertIn('Insufficient data',result['suggestions'][0]['text'])

    def test_empty_week(self):
        result=summary([])
        self.assertEqual(result['facts']['current']['activity_count'],0)
        self.assertEqual(result['facts']['current']['total_duration_s'],0)
        self.assertIsNone(result['facts']['current']['power_coverage']['percentage'])
        self.assertEqual(len(result['suggestions']),1)
        self.assertFalse(result['inferences'])

    def test_previous_only_no_claim_of_load_collapse(self):
        result=summary([activity('2026-10-26')])
        self.assertIsNone(result['facts']['load_trend'])
        self.assertIsNone(result['facts']['comparison']['total_training_load']['delta'])

    def test_missing_wellness_insufficient(self):
        for item in summary([activity(START)])['facts']['recovery_signals'].values():
            self.assertEqual(item['status'],'insufficient data')

    def test_monotonic_load_up_down_and_flat(self):
        for previous,current,direction in ((40,80,'up'),(80,40,'down'),(40,40,'flat')):
            with self.subTest(direction=direction):
                result=summary([activity('2026-10-26',previous),activity(START,current)])
                self.assertEqual(result['facts']['load_trend'],direction)
                self.assertEqual(result['inferences'][0]['direction'],direction)
                self.assertEqual(len(result['suggestions']),1)

    def test_missing_invalid_metrics_not_zero(self):
        for value in (None,-1,True,'bad',float('inf'),float('nan')):
            with self.subTest(value=value):
                result=summary([activity(START,50),activity('2026-11-03',value)])
                current=result['facts']['current']
                self.assertIsNone(current['total_training_load'])
                self.assertEqual(current['coverage']['total_training_load']['known'],1)
                self.assertEqual(current['coverage']['total_training_load']['observed_sum'],50)
                json.dumps(result,allow_nan=False)

    def test_power_coverage_not_intensity_distribution(self):
        result=summary([activity(START),activity('2026-11-03',icu_weighted_avg_watts=None)])
        coverage=result['facts']['current']['power_coverage']
        self.assertEqual(coverage['percentage'],50)
        self.assertIn('insufficient data for intensity zones',coverage['limitation'])

    def test_wellness_exact_preceding_fourteen_days_excludes_today(self):
        today=date.fromisoformat(START)
        health=[{'id':(today-timedelta(days=i)).isoformat(),'sleep_secs':28800,'resting_hr':50} for i in range(1,15)]
        health.append({'id':START,'sleep_secs':25920,'resting_hr':55})
        health.append({'id':'2026-11-09','sleep_secs':99999,'resting_hr':999})
        result=summary([activity(START)],health)
        signals=result['facts']['recovery_signals']
        self.assertAlmostEqual(signals['sleep_secs']['percentage'],-10)
        self.assertAlmostEqual(signals['resting_hr']['percentage'],10)
        self.assertEqual(signals['resting_hr']['baseline_mean'],50)
        self.assertEqual(len(signals['resting_hr']['basis_refs']),15)
        self.assertTrue(all('No recovery or health diagnosis' in item['text'] for item in result['inferences']))

    def test_short_baseline_and_duplicate_dates_not_diagnosed(self):
        health=[{'id':'2026-11-01','sleep_secs':28000,'resting_hr':50},
                {'id':'2026-11-01','sleep_secs':29000,'resting_hr':51},
                {'id':START,'sleep_secs':27000,'resting_hr':60}]
        result=summary([],health)
        self.assertEqual(result['facts']['recovery_signals']['resting_hr']['status'],'insufficient data')
        self.assertTrue(result['facts']['warnings'])
        self.assertFalse(result['inferences'])

    def test_boundaries_aliases_and_no_mutation(self):
        rows=[{'date':START,'duration':'3600','distance':'20000','training_load':'50','weighted_avg_watts':'160'},activity('2026-11-09',999),activity('2026-10-25',999)]
        before=copy.deepcopy(rows)
        result=summary(rows)
        self.assertEqual(rows,before)
        self.assertEqual(result['facts']['current']['total_training_load'],50)
        self.assertIsNone(result['facts']['previous'])

    def test_zero_previous_load_undefined_percentage(self):
        result=summary([activity('2026-10-26',0),activity(START,50)])
        self.assertEqual(result['facts']['load_trend'],'up')
        self.assertIsNone(result['facts']['comparison']['total_training_load']['percentage'])

    def test_invalid_dates_rows_and_duplicate_activity_ids(self):
        for start in ('2026-11-03','bad',None,'0001-01-01','9999-12-27'):
            with self.subTest(start=start),self.assertRaises(ValueError):
                build_weekly_summary([],week_start=start)
        for rows in (None,{},[None]):
            with self.assertRaises(ValueError):
                summary(rows)
        result=summary([activity(START,id='same'),activity(START,id='same'),activity('bad')])
        self.assertEqual(result['facts']['current']['activity_count'],1)
        self.assertEqual(len(result['facts']['warnings']),2)

    def test_aggregate_overflow_is_missing_not_infinity(self):
        result=summary([activity(START,1e308),activity('2026-11-03',1e308)])
        self.assertIsNone(result['facts']['current']['total_training_load'])
        json.dumps(result,allow_nan=False)

if __name__ == '__main__':
    unittest.main()
