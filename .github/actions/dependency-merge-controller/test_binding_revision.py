"""Current membership is proved against the final same-snapshot run revision."""
import copy
import unittest
import urllib.parse

from policy import decide, initial_intent
from runner import ApiError
from test_api_economy import LATER, Response
from test_job_binding import JobServer
from test_policy import REPO, fixtures


class RefreshServer(JobServer):
    def __init__(self, current_gate):
        super().__init__()
        self.current_gate = current_gate
        self.job_reads = 0
        self.checks.append(self.check(300, 'Auxiliary same-run job',
            check_suite={'id': 10020}, completed_at=LATER, run_attempt=2,
            details_url=f'https://github.com/{REPO}/actions/runs/20/job/300'))
        self.run_responses[20] = copy.deepcopy(self.runs[20]) | {'run_attempt': 2, 'updated_at': LATER}

    def open(self, request, **kwargs):
        parsed = urllib.parse.urlsplit(request.full_url)
        if parsed.path == f'/repos/{REPO}/actions/runs/20/jobs':
            self.job_reads += 1
            query = urllib.parse.parse_qs(parsed.query)
            self.calls.append(('actions/runs/20/jobs', query))
            original = copy.deepcopy(self.jobs[(20, 'latest')][0])
            if self.job_reads == 1:
                rows = [original]
            else:
                auxiliary = original | {'id': 300, 'run_attempt': 2, 'name': 'Auxiliary same-run job',
                    'status': 'completed', 'conclusion': 'success',
                    'check_run_url': f'https://api.github.com/repos/{REPO}/check-runs/300'}
                rows = [auxiliary]
                if self.current_gate != 'missing':
                    status, conclusion = ('in_progress', None) if self.current_gate == 'pending' else ('completed', 'failure')
                    rows.append(original | {'id': 220, 'run_attempt': 2, 'status': status, 'conclusion': conclusion,
                        'check_run_url': f'https://api.github.com/repos/{REPO}/check-runs/220'})
            return Response({'total_count': len(rows), 'jobs': rows})
        return super().open(request, **kwargs)


class BindingRevisionTest(unittest.TestCase):
    def test_later_refresh_invalidates_earlier_required_current_proof(self):
        for gate in ('missing', 'pending', 'failure'):
            server = RefreshServer(gate)
            api = server.client()
            with self.subTest(current_gate=gate), server.transport(), self.assertRaisesRegex(ApiError, 'membership proof'):
                api.snapshot(42)
            self.assertEqual(api.api_calls, 20)
            self.assertEqual(api.mutation_attempts, 0)

    def test_later_refresh_also_invalidates_current_controller_exemption(self):
        server = RefreshServer('missing')
        server.checks[0].update(name='ci / Dependency merge controller', status='in_progress', conclusion=None)
        server.jobs[(20, 'latest')][0].update(name=server.checks[0]['name'], status='in_progress', conclusion=None)
        api = server.client()
        with server.transport(), self.assertRaisesRegex(ApiError, 'membership proof'):
            api.snapshot(42)
        self.assertEqual(api.api_calls, 20)
        self.assertEqual(api.mutation_attempts, 0)

    def test_old_origin_job_is_valid_when_latest_proof_uses_current_run_revision(self):
        server = JobServer()
        server.runs[20]['run_attempt'] = 2
        server.jobs[(20, 'latest')][0]['run_attempt'] = 1
        api = server.client()
        with server.transport():
            snapshot = api.snapshot(42)
        context, original = fixtures()
        self.assertEqual(decide(context, snapshot, server.policy, initial_intent(context, original, server.policy))[0], 'ready')
        check = next(item for item in snapshot['checks'] if item['id'] == 20)
        self.assertTrue(check['job_current'])
        self.assertEqual(check['attempt'], 1)
        self.assertEqual(api.api_calls, 18)
        self.assertEqual(api.mutation_attempts, 0)


if __name__ == '__main__':
    unittest.main()
