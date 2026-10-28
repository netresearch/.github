"""GitHub's numeric-repository Link alias is data, never a request target."""
import unittest

from runner import ApiError
import test_job_binding
from test_job_binding import JobServer
from test_policy import REPO


REPOSITORY_ID = 1124082336


class JobPaginationAliasTest(unittest.TestCase):
    def server(self, target=None):
        server = JobServer()
        for run in server.runs.values():
            run['repository']['id'] = REPOSITORY_ID
        job = server.jobs[(20, 'latest')][0]
        extra = job | {'id': 200, 'name': 'Another native job',
                       'check_run_url': f'https://api.github.com/repos/{REPO}/check-runs/200'}
        target = target or f'https://api.github.com/repositories/{REPOSITORY_ID}/actions/runs/20/jobs?filter=latest&per_page=100&page=2'
        server.job_responses[(20, 'latest', 1)] = ({'total_count': 2, 'jobs': [job]}, {'Link': f'<{target}>; rel="next"'})
        server.job_responses[(20, 'latest', 2)] = ({'total_count': 2, 'jobs': [extra]}, {})
        return server

    def decision(self, server):
        return test_job_binding.JobBindingTest().decision(server)

    def test_numeric_repository_alias_is_accepted_without_following_it(self):
        server = self.server()
        result, api, _ = self.decision(server)
        self.assertEqual(result[0], 'ready')
        self.assertEqual(api.api_calls, 19)
        jobs = [resource for resource, _ in server.calls if '/jobs' in resource]
        self.assertTrue(all(resource.startswith('actions/runs/') for resource in jobs))
        self.assertEqual(api.mutation_attempts, 0)

    def test_owner_name_next_path_is_still_accepted(self):
        server = self.server(f'https://api.github.com/repos/{REPO}/actions/runs/20/jobs?filter=latest&per_page=100&page=2')
        self.assertEqual(self.decision(server)[0][0], 'ready')

    def test_alias_has_no_authenticated_api_route(self):
        api = self.server().client()
        with self.assertRaises(ApiError):
            api.api_url(f'/repositories/{REPOSITORY_ID}/actions/runs/20/jobs?filter=latest')

    def test_positive_json_repository_identity_is_mandatory(self):
        for value in (None, True, False, 0, -1, float(REPOSITORY_ID), str(REPOSITORY_ID)):
            server = self.server()
            server.runs[20]['repository']['id'] = value
            with self.subTest(value=value), self.assertRaises(ApiError):
                self.decision(server)
        server = self.server()
        del server.runs[20]['repository']['id']
        with self.assertRaises(ApiError):
            self.decision(server)

    def test_alias_must_name_the_exact_server_repository(self):
        for value in ('1124082337', '0', '-1', 'True', '01124082336', '', '1124082336/extra'):
            target = f'https://api.github.com/repositories/{value}/actions/runs/20/jobs?filter=latest&per_page=100&page=2'
            with self.subTest(value=value), self.assertRaises(ApiError):
                self.decision(self.server(target))

    def test_same_https_host_exact_run_view_size_and_advance_are_required(self):
        base = f'https://api.github.com/repositories/{REPOSITORY_ID}/actions/runs/20/jobs?filter=latest&per_page=100&page=2'
        targets = (base.replace('https:', 'http:'), base.replace('api.github.com', 'evil.invalid'),
                   base.replace('/runs/20/', '/runs/21/'), base.replace('filter=latest', 'filter=all'),
                   base.replace('per_page=100', 'per_page=5'), base.replace('page=2', 'page=1'),
                   base + '&page=2', base + '&filter=latest', base + '&extra=', base + '#fragment')
        for target in targets:
            with self.subTest(target=target), self.assertRaises(ApiError):
                self.decision(self.server(target))


if __name__ == '__main__':
    unittest.main()
