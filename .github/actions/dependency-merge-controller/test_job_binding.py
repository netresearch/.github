"""Server-owned Actions job/check joins through the real offline HTTP adapter."""
import copy
import unittest
import urllib.parse

from policy import Blocked, decide, initial_intent, require_additional_checks
from runner import ApiError
from test_api_economy import Response, Server, run_record
from test_policy import REPO, SHA, fixtures


class JobServer(Server):
    def __init__(self):
        super().__init__()
        self.jobs = {}
        self.job_responses = {}
        for run in self.runs.values():
            run['check_suite_id'] = 10000 + run['id']
        for check in self.checks:
            check['check_suite'] = {'id': 10000 + check['id']}
            run_id = check['id']
            job = {'id': check['id'], 'run_id': run_id, 'run_attempt': 1,
                   'run_url': f'https://api.github.com/repos/{REPO}/actions/runs/{run_id}',
                   'head_sha': SHA, 'name': check['name'], 'status': check['status'],
                   'conclusion': check['conclusion'],
                   'check_run_url': f'https://api.github.com/repos/{REPO}/check-runs/{check["id"]}'}
            self.jobs[(run_id, 'latest')] = [job]
            self.jobs[(run_id, 'all')] = copy.deepcopy([job])

    def open(self, request, **kwargs):
        parsed = urllib.parse.urlsplit(request.full_url)
        resource = parsed.path.removeprefix(f'/repos/{REPO}/')
        if resource.startswith('actions/runs/') and resource.endswith('/jobs'):
            query = urllib.parse.parse_qs(parsed.query)
            run_id, view, page = int(resource.split('/')[2]), query['filter'][0], int(query['page'][0])
            self.calls.append((resource, query))
            if (run_id, view, page) in self.job_responses:
                body, headers = self.job_responses[(run_id, view, page)]
                return Response(body, headers)
            return Response({'total_count': len(self.jobs.get((run_id, view), [])),
                             'jobs': self.jobs.get((run_id, view), [])})
        return super().open(request, **kwargs)


class JobBindingTest(unittest.TestCase):
    def decision(self, server):
        api = server.client()
        with server.transport():
            snapshot = api.snapshot(42)
        context, original = fixtures()
        return decide(context, snapshot, server.policy, initial_intent(context, original, server.policy)), api, snapshot

    def test_matching_server_owned_membership_is_ready(self):
        result, api, _ = self.decision(JobServer())
        self.assertEqual(result[0], 'ready')
        self.assertEqual(api.api_calls, 18)

    def test_same_app_suite_head_forged_check_is_not_a_job(self):
        server = JobServer()
        real = server.checks.pop(0)
        server.checks.append(real | {'id': 200, 'details_url': f'https://github.com/{REPO}/actions/runs/20/job/20'})
        with self.assertRaises(ApiError):
            self.decision(server)

    def test_suite_and_exact_head_must_match(self):
        for field in ('suite', 'head'):
            server = JobServer()
            if field == 'suite':
                server.checks[0]['check_suite']['id'] = 999999
            else:
                server.checks[0]['head_sha'] = 'c' * 40
            with self.subTest(field=field), self.assertRaises(ApiError):
                self.decision(server)

    def test_server_job_name_cannot_be_replaced_by_caller_check_name(self):
        server = JobServer()
        server.jobs[(20, 'latest')][0]['name'] = 'different native job'
        with self.assertRaises(ApiError):
            self.decision(server)

    def test_historical_success_cannot_fill_a_current_missing_gate(self):
        server = JobServer()
        server.jobs[(20, 'latest')] = []
        result, _, snapshot = self.decision(server)
        self.assertNotEqual(result[0], 'ready')
        self.assertEqual(len(snapshot['checks']), 10)

    def test_newest_successful_run_cannot_borrow_an_older_real_gate(self):
        server = JobServer()
        newer = copy.deepcopy(server.runs[20])
        newer.update(id=200, check_suite_id=10200)
        server.runs[200] = newer
        server.listed.append(newer)
        result, _, _ = self.decision(server)
        self.assertNotEqual(result[0], 'ready')

    def test_job_status_cannot_be_hidden_by_successful_check(self):
        server = JobServer()
        server.jobs[(20, 'latest')][0].update(status='in_progress', conclusion=None)
        with self.assertRaises(ApiError):
            self.decision(server)

    def test_job_pagination_requires_stable_complete_counts(self):
        for body in ({'total_count': 2, 'jobs': []}, {'total_count': True, 'jobs': []}, {'jobs': []}):
            server = JobServer()
            server.job_responses[(20, 'latest', 1)] = (body, {})
            with self.subTest(body=body), self.assertRaises(ApiError):
                self.decision(server)

    def test_missing_malformed_server_suite_identity_blocks(self):
        for value in (None, True, '10020', 0, -1):
            server = JobServer()
            server.checks[0]['check_suite']['id'] = value
            with self.subTest(value=value), self.assertRaises(ApiError):
                self.decision(server)

    def test_job_identity_and_check_url_are_strict(self):
        changes = ({'id': True}, {'run_id': True}, {'run_attempt': None}, {'run_attempt': 2},
                   {'head_sha': 'c' * 40}, {'run_url': 'https://api.github.com/repos/evil/repo/actions/runs/20'},
                   {'check_run_url': 'https://api.github.com/repos/evil/repo/check-runs/20'},
                   {'check_run_url': f'https://api.github.com/repos/{REPO}/check-runs/020'})
        for change in changes:
            server = JobServer()
            server.jobs[(20, 'latest')][0].update(change)
            with self.subTest(change=change), self.assertRaises(ApiError):
                self.decision(server)

    def test_duplicate_jobs_and_check_mappings_block(self):
        for duplicate in ('job', 'check'):
            server = JobServer()
            extra = copy.deepcopy(server.jobs[(20, 'latest')][0])
            if duplicate == 'check':
                extra['id'] = 99999
            server.jobs[(20, 'latest')].append(extra)
            with self.subTest(duplicate=duplicate), self.assertRaises(ApiError):
                self.decision(server)

    def test_job_id_need_not_equal_check_id_but_link_must_name_the_job(self):
        server = JobServer()
        server.jobs[(20, 'latest')][0]['id'] = 99999
        server.checks[0]['details_url'] = f'https://github.com/{REPO}/actions/runs/20/job/99999'
        self.assertEqual(self.decision(server)[0][0], 'ready')
        server.checks[0]['details_url'] = f'https://github.com/{REPO}/actions/runs/20/job/20'
        with self.assertRaises(ApiError):
            self.decision(server)

    def test_run_only_details_link_still_requires_server_membership(self):
        server = JobServer()
        server.checks[0]['details_url'] = f'https://github.com/{REPO}/actions/runs/20'
        self.assertEqual(self.decision(server)[0][0], 'ready')
        server.checks[0]['id'] = 99999
        with self.assertRaises(ApiError):
            self.decision(server)

    def test_all_inventory_must_contain_exact_latest_records(self):
        server = JobServer()
        historical = copy.deepcopy(server.checks[0])
        historical.update(id=200, details_url=f'https://github.com/{REPO}/actions/runs/20/job/200')
        server.checks.append(historical)
        server.jobs[(20, 'all')] = [server.jobs[(20, 'all')][0] | {'id': 200,
            'check_run_url': f'https://api.github.com/repos/{REPO}/check-runs/200'}]
        with self.assertRaises(ApiError):
            self.decision(server)

    def test_stable_multiple_job_pages_complete_and_changing_count_blocks(self):
        for total in (2, 3):
            server = JobServer()
            job = server.jobs[(20, 'latest')][0]
            extra = job | {'id': 200, 'name': 'other native job',
                           'check_run_url': f'https://api.github.com/repos/{REPO}/check-runs/200'}
            server.job_responses[(20, 'latest', 1)] = ({'total_count': 2, 'jobs': [job]},
                {'Link': f'<https://api.github.com/repos/{REPO}/actions/runs/20/jobs?filter=latest&per_page=100&page=2>; rel="next"'})
            server.job_responses[(20, 'latest', 2)] = ({'total_count': total, 'jobs': [extra]}, {})
            with self.subTest(total=total):
                if total == 2:
                    self.assertEqual(self.decision(server)[0][0], 'ready')
                else:
                    with self.assertRaises(ApiError):
                        self.decision(server)

    def test_foreign_repeated_and_wrong_view_next_links_block(self):
        targets = ('https://evil.invalid/page',
                   f'https://api.github.com/repos/{REPO}/actions/runs/20/jobs?filter=latest&per_page=100&page=1',
                   f'https://api.github.com/repos/{REPO}/actions/runs/20/jobs?filter=all&per_page=100&page=2',
                   f'https://api.github.com/repos/{REPO}/actions/runs/20/jobs?filter=latest&per_page=100&page=2&extra=')
        for target in targets:
            server = JobServer()
            server.job_responses[(20, 'latest', 1)] = ({'total_count': 2, 'jobs': server.jobs[(20, 'latest')]},
                                                      {'Link': f'<{target}>; rel="next"'})
            with self.subTest(target=target), self.assertRaises(ApiError):
                self.decision(server)

    def test_oversized_page_and_explicit_safety_cap_never_accept_partial_jobs(self):
        server = JobServer()
        server.job_responses[(20, 'latest', 1)] = ({'total_count': 101, 'jobs': server.jobs[(20, 'latest')] * 101}, {})
        with self.assertRaises(ApiError):
            self.decision(server)

    def test_historical_failed_same_context_does_not_hide_new_successful_run(self):
        server = JobServer()
        server.runs[20].update(status='completed', conclusion='cancelled')
        server.checks[0].update(conclusion='failure')
        server.jobs[(20, 'latest')][0]['conclusion'] = 'failure'
        newer = run_record(200, 'ci.yml')
        server.runs[200] = newer
        server.listed.append(newer)
        check = server.check(200, server.checks[0]['name'])
        server.checks.append(check)
        job = copy.deepcopy(server.jobs[(20, 'latest')][0])
        job.update(id=200, run_id=200, conclusion='success',
                   run_url=f'https://api.github.com/repos/{REPO}/actions/runs/200',
                   check_run_url=f'https://api.github.com/repos/{REPO}/check-runs/200')
        server.jobs[(200, 'latest')] = [job]
        self.assertEqual(self.decision(server)[0][0], 'ready')
        server.checks.pop()  # Successful newer workflow with no named native gate cannot borrow old success.
        server.checks[0]['conclusion'] = 'success'
        server.jobs[(20, 'latest')][0]['conclusion'] = 'success'
        self.assertNotEqual(self.decision(server)[0][0], 'ready')

    def test_current_controller_exemption_requires_verified_current_membership(self):
        context, _ = fixtures()
        check = {'name': 'auto / Dependency merge controller', 'run_id': context['run_id'],
                 'workflow': context['caller_path'].split('/')[-1], 'app_id': 15368,
                 'status': 'in_progress', 'conclusion': None}
        for fields in ({}, {'job_verified': True}, {'job_current': True}):
            with self.subTest(fields=fields), self.assertRaises(Blocked):
                # Failure makes the generic additional-check rejection unambiguous.
                require_additional_checks(context, {check['name']: check | fields | {'conclusion': 'failure'}}, set())
        require_additional_checks(context, {check['name']: check | {'job_verified': True, 'job_current': True}}, set())

    def test_unreferenced_dynamic_run_need_not_have_a_native_suite(self):
        server = JobServer()
        dynamic = run_record(300, 'unused.yml', path='dynamic/agents/copilot-pull-request-reviewer',
                             event='dynamic', check_suite_id=None)
        server.listed.append(dynamic)
        result, api, _ = self.decision(server)
        self.assertEqual(result[0], 'ready')
        self.assertNotIn(300, api.runs)

    def test_explicit_safety_cap_blocks_incomplete_inventory(self):
        server = JobServer()
        for page in range(1, 11):
            server.job_responses[(20, 'latest', page)] = ({'total_count': 11, 'jobs': server.jobs[(20, 'latest')]},
                {'Link': f'<https://api.github.com/repos/{REPO}/actions/runs/20/jobs?filter=latest&per_page=100&page={page + 1}>; rel="next"'})
        with self.assertRaises(ApiError):
            self.decision(server)

    def test_exactly_one_thousand_jobs_is_not_subject_to_workflow_search_limit(self):
        server = JobServer()
        original = server.jobs[(20, 'latest')][0]
        rows = [original] + [original | {'id': number, 'name': f'Native matrix {number}',
                'check_run_url': f'https://api.github.com/repos/{REPO}/check-runs/{number}'}
                for number in range(1000, 1999)]
        for page in range(1, 11):
            headers = {} if page == 10 else {'Link': f'<https://api.github.com/repos/{REPO}/actions/runs/20/jobs?filter=latest&per_page=100&page={page + 1}>; rel="next"'}
            server.job_responses[(20, 'latest', page)] = ({'total_count': 1000, 'jobs': rows[(page - 1) * 100:page * 100]}, headers)
        self.assertEqual(self.decision(server)[0][0], 'ready')

    def test_all_ten_retried_producers_use_twenty_inventory_reads(self):
        server = JobServer()
        for check in list(server.checks):
            run_id = check['id']
            server.runs[run_id]['run_attempt'] = 2
            job = server.jobs[(run_id, 'latest')][0]
            cloned = check | {'id': 200 + run_id,
                'details_url': f'https://github.com/{REPO}/actions/runs/{run_id}/job/{200 + run_id}'}
            current_job = job | {'id': 200 + run_id, 'run_attempt': 2,
                'check_run_url': f'https://api.github.com/repos/{REPO}/check-runs/{200 + run_id}'}
            server.checks.append(cloned)
            server.jobs[(run_id, 'latest')] = [current_job]
            server.jobs[(run_id, 'all')] = [job, current_job]
        result, api, snapshot = self.decision(server)
        self.assertEqual(result[0], 'ready')
        self.assertEqual(api.api_calls, 28)
        self.assertEqual(len(snapshot['checks']), 20)


if __name__ == '__main__':
    unittest.main()
