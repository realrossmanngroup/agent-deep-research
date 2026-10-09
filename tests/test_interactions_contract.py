# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai==2.29.0", "rich>=13.0.0", "markdown>=3.5"]
# ///
"""Deterministic Interactions contracts: fake provider, real SDK, no network."""
import contextlib
import hashlib
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from google.genai._gaos.types.interactions import CreateAgentInteraction, Interaction

ROOT = Path(__file__).resolve().parents[1]
MODULE = Path(os.environ.get("RESEARCH_CONTRACT_MODULE", ROOT / "scripts/research.py"))
spec = importlib.util.spec_from_file_location("research", MODULE)
research = importlib.util.module_from_spec(spec)
spec.loader.exec_module(research)
MAX = "deep-research-max-preview-04-2026"
STANDARD = "deep-research-preview-04-2026"


def result(status="completed", agent=MAX, summaries=True, text="Evidence from a primary source.\nConclusion."):
    steps = []
    if summaries:
        steps.append({"type": "thought", "summary": [{"type": "text", "text": "Public progress, not evidence."}]})
    if text is not None:
        steps.append({"type": "model_output", "content": [
            {"type": "text", "text": text[:10], "annotations": [
                {"type": "url_citation", "url": "https://example.org/primary", "title": "Primary evidence", "start_index": 0, "end_index": 10}]},
            {"type": "text", "text": text[10:]},
        ]})
    return Interaction.model_validate({"id": "v1_fixture", "status": status, "agent": agent,
        "agent_config": {"type": "deep-research", "thinking_summaries": "auto"},
        "steps": steps, "usage": {"total_input_tokens": 900000, "total_output_tokens": 80000,
            "total_thought_tokens": 50000, "total_cached_tokens": 450000,
            "total_tool_use_tokens": 100000, "grounding_tool_count": [{"type": "google_search", "count": 160}]}})


class FakeInteractions:
    def __init__(self, completed=None, error=None):
        self.completed = completed or result()
        self.error = error
        self.creates = []
        self.gets = []
    def create(self, **kwargs):
        CreateAgentInteraction.model_validate(kwargs)
        self.creates.append(kwargs)
        if self.error:
            raise self.error
        return Interaction.model_validate({"id": "v1_fixture", "status": "in_progress", "agent": kwargs["agent"]})
    def get(self, iid):
        self.gets.append(iid)
        if len(self.gets) > 8:
            raise SystemExit("Offline fixture safety cutoff: unbounded GET retry")
        if isinstance(self.completed, Exception):
            raise self.completed
        return self.completed


def add_ids(start, count):
    for number in range(start, start + count):
        research.add_research_id(f"parallel-{number}")


def concurrent_start(query, calls, barrier):
    class Provider(FakeInteractions):
        def create(self, **kwargs):
            with calls.get_lock():
                calls.value += 1
            return super().create(**kwargs)
    barrier.wait(timeout=10)
    args = research.build_parser().parse_args(["start", query, "--agent", MAX])
    with patch.object(research, "get_client", return_value=SimpleNamespace(interactions=Provider())):
        try:
            research.cmd_start(args)
        except RuntimeError as exc:
            if "Unresolved research claim" not in str(exc):
                raise


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.old_cwd = Path.cwd()
        os.chdir(self.directory.name)
        self.addCleanup(os.chdir, self.old_cwd)
        self.env = patch.dict(os.environ, {"HOME": self.directory.name, "GEMINI_API_KEY": "offline-dummy-never-send"}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden in contract fixtures"))
        self.network.start(); self.addCleanup(self.network.stop)
        self.output = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stdout.__enter__(); self.addCleanup(self.stdout.__exit__, None, None, None)
        self.sleep = patch.object(research.time, "sleep", return_value=None)
        self.sleep.start(); self.addCleanup(self.sleep.stop)

    def start(self, fake, *extra):
        args = research.build_parser().parse_args(["start", "Research the exact claim", "--agent", MAX, *extra])
        with patch.object(research, "get_client", return_value=SimpleNamespace(interactions=fake)):
            research.cmd_start(args)

    def test_explicit_current_request_and_capabilities(self):
        fake = FakeInteractions()
        self.start(fake)
        request = fake.creates[0]
        self.assertEqual(request["agent"], MAX)
        self.assertTrue(request["background"])
        self.assertTrue(request["store"])
        self.assertEqual(request["agent_config"], {"type": "deep-research", "thinking_summaries": "auto"})
        self.assertNotIn("generation_config", request)
        self.assertEqual(research.capabilities()["sdk_version"], "2.29.0")

    def test_public_summary_list_is_displayable_but_not_final(self):
        texts = research.interaction_texts(result())
        self.assertTrue(all(isinstance(text, str) for text in texts))
        self.assertEqual(research.interaction_final_text(result()), "Evidence from a primary source.\nConclusion.")
        self.assertEqual(research.interaction_final_text(result(text=None)), "")

    def test_missing_last_output_does_not_fall_back_to_earlier_report(self):
        response = result()
        response.steps.append(type(response.steps[-1]).model_validate({"type": "model_output", "content": []}))
        self.assertEqual(research.interaction_final_text(response), "")

    def test_legacy_report_remains_readable(self):
        response = SimpleNamespace(outputs=[SimpleNamespace(type="text", text="Legacy report")])
        self.assertEqual(research.interaction_final_text(response), "Legacy report")

    def test_create_report_cache_canonical_bytes_and_receipts(self):
        fake = FakeInteractions()
        self.start(fake, "--output", "created.md", "--metadata-output", "created.json")
        args = research.build_parser().parse_args(["report", "v1_fixture", "--output", "retrieved.md", "--metadata-output", "retrieved.json"])
        with patch.object(research, "get_client", return_value=SimpleNamespace(interactions=fake)):
            research.cmd_report(args)
        self.start(fake, "--output", "cached.md", "--metadata-output", "cached.json")
        self.assertEqual(len(fake.creates), 1)
        expected = Path("created.md").read_bytes()
        self.assertEqual(expected, Path("retrieved.md").read_bytes())
        self.assertEqual(expected, Path("cached.md").read_bytes())
        for name, origin in (("created", "create"), ("retrieved", "report"), ("cached", "cache")):
            receipt = json.loads(Path(name + ".json").read_text())
            self.assertEqual(receipt["origin"], origin)
            self.assertEqual(receipt["report_sha256"], hashlib.sha256(expected).hexdigest())
            self.assertEqual(receipt["returned_agent"], MAX)
            self.assertEqual(receipt["provider_usage"]["total_thought_tokens"], 50000)
            self.assertEqual(receipt["citation_annotations"][0]["annotation"]["url"], "https://example.org/primary")
            self.assertEqual(receipt["report_urls"], [])
            self.assertFalse(receipt["billing_complete"])
            self.assertIsNone(receipt["cost_usd"])
            raw = receipt["raw_response"]
            self.assertEqual(raw["sha256"], hashlib.sha256(Path(raw["path"]).read_bytes()).hexdigest())
            self.assertEqual(raw["serialization"], "google-genai-model_dump")
            self.assertNotIn("offline-dummy", Path(name + ".json").read_text())

    def test_summary_display_on_and_off(self):
        for show in (True, False):
            fake = FakeInteractions()
            research._poll_and_save(SimpleNamespace(interactions=fake), "v1_fixture", output_path=f"{show}.md", show_thoughts=show, adaptive_poll=False)
        self.assertEqual(Path("True.md").read_bytes(), Path("False.md").read_bytes())

    def test_empty_and_thought_only_completion_fail_without_cache(self):
        for text, summaries in ((None, True), (None, False), ("", True), ("   ", False)):
            with self.subTest(text=text, summaries=summaries):
                fake = FakeInteractions(result(text=text, summaries=summaries))
                with self.assertRaises((RuntimeError, SystemExit)):
                    self.start(fake, "--output", "missing.md", "--no-cache")
                self.assertFalse(Path("missing.md").exists())
                self.assertFalse(research.load_state().get("researchCache"))

    def test_terminal_failure_never_succeeds_or_caches(self):
        for status in ("failed", "cancelled", "incomplete", "budget_exceeded"):
            with self.subTest(status=status):
                with self.assertRaises((RuntimeError, SystemExit)):
                    self.start(FakeInteractions(result(status=status)), "--output", "failed.md", "--no-cache")
                self.assertFalse(Path("failed.md").exists())
                self.assertFalse(research.load_state().get("researchCache"))

    def test_no_silent_grounding_downgrade_or_duplicate_create(self):
        fake = FakeInteractions(error=TimeoutError("Ambiguous transport failure"))
        with self.assertRaises(SystemExit):
            self.start(fake, "--store", "fileSearchStores/fixture")
        self.assertEqual(len(fake.creates), 1)
        self.assertEqual([tool["type"] for tool in fake.creates[0]["tools"]], ["google_search", "url_context", "code_execution", "file_search"])

    def test_permanent_get_error_is_not_retried(self):
        exc = RuntimeError("Forbidden")
        exc.status_code = 403
        fake = FakeInteractions(exc)
        with self.assertRaises(RuntimeError):
            research._poll_and_save(SimpleNamespace(interactions=fake), "v1_fixture", adaptive_poll=False)
        self.assertEqual(len(fake.gets), 1)

    def test_transient_get_errors_are_bounded(self):
        exc = RuntimeError("Unavailable")
        exc.status_code = 503
        fake = FakeInteractions(exc)
        with self.assertRaises(RuntimeError):
            research._poll_and_save(SimpleNamespace(interactions=fake), "v1_fixture", adaptive_poll=False)
        self.assertEqual(len(fake.gets), 6)

    def test_cache_separates_agent_config_and_local_content(self):
        file = Path("context.txt"); file.write_text("First evidence")
        def key(**kwargs):
            return research._get_cache_key("same", True, "standard", context_path=str(file), **kwargs)
        first = key(agent=STANDARD)
        self.assertNotEqual(first, key(agent=MAX))
        self.assertNotEqual(first, key(agent=STANDARD, request_config={"thinking_summaries": "none"}))
        file.write_text("Changed evidence")
        self.assertNotEqual(first, key(agent=STANDARD))

    def test_quality_bypass_creates_fresh_completed_request(self):
        fake = FakeInteractions()
        self.start(fake, "--output", "first.md")
        self.start(fake, "--output", "second.md", "--no-cache")
        self.assertEqual(len(fake.creates), 2)

    def test_retrieval_never_relabels_legacy_or_unknown_agent(self):
        for agent in ("deep-research-pro-preview-12-2025", None):
            fake = FakeInteractions(result(agent=agent))
            args = research.build_parser().parse_args(["report", "v1_fixture", "--output", "old.md", "--metadata-output", "old.json"])
            with patch.object(research, "get_client", return_value=SimpleNamespace(interactions=fake)):
                research.cmd_report(args)
            receipt = json.loads(Path("old.json").read_text())
            self.assertEqual(receipt["returned_agent"], agent)
            self.assertIsNone(receipt["requested_agent"])
            self.assertEqual(fake.creates, [])

    def test_state_concurrency_retains_ids_and_unrelated_keys(self):
        Path(".gemini-research.json").write_text(json.dumps({"unrelated": {"preserve": True}}))
        processes = [multiprocessing.get_context("fork").Process(target=add_ids, args=(n * 15, 15)) for n in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        state = research.load_state()
        self.assertEqual(len(state["researchIds"]), 60)
        self.assertEqual(state["unrelated"], {"preserve": True})

    def test_corrupt_state_fails_closed_without_replacement(self):
        path = Path(".gemini-research.json"); path.write_text("{broken")
        with self.assertRaises(ValueError):
            research.add_research_id("must-not-overwrite")
        self.assertEqual(path.read_text(), "{broken")

    def test_sensitive_context_and_file_never_create_or_upload(self):
        for name in (".env", ".env.local", "secrets.toml", "credentials.json", "id_rsa", "private.pem", ".npmrc"):
            Path(name).write_text("never-send-this-secret")
            for flag in ("--file", "--context"):
                with self.subTest(name=name, flag=flag):
                    fake = FakeInteractions()
                    with self.assertRaises(ValueError):
                        self.start(fake, flag, name)
                    self.assertEqual(fake.creates, [])

    def test_unfinished_context_is_preserved(self):
        Path("context.txt").write_text("Required evidence")
        fake = FakeInteractions()
        with patch.object(research, "_upload_context_files", return_value=("fileSearchStores/keep", 1, 17)), \
             patch.object(research, "_poll_and_save", side_effect=SystemExit(1)), \
             patch.object(research, "_cleanup_context_store") as cleanup:
            with self.assertRaises(SystemExit):
                self.start(fake, "--context", "context.txt", "--output", "pending.md")
            cleanup.assert_not_called()
        self.assertEqual(research.load_state()["contextInteractions"]["v1_fixture"], "fileSearchStores/keep")

    def test_output_preflight_precedes_create(self):
        fake = FakeInteractions()
        with self.assertRaises(ValueError):
            self.start(fake, "--metadata-output", "missing-directory/receipt.json")
        self.assertEqual(fake.creates, [])

    def test_sensitive_symlink_names_and_targets_never_reach_provider(self):
        Path('public.txt').write_text('Public source')
        Path('.env.real').write_text('never-send-secret')
        Path('.env.link').symlink_to('public.txt')
        Path('friendly.txt').symlink_to('.env.real')
        for name in ('.env.link', 'friendly.txt'):
            for flag in ('--file', '--context', '--input-file'):
                with self.subTest(name=name, flag=flag):
                    argv = ['start', '--agent', MAX, flag, name]
                    if flag != '--input-file':
                        argv.append('Research a claim')
                    args = research.build_parser().parse_args(argv)
                    with patch.object(research, 'get_client', side_effect=AssertionError('Sensitive file reached provider')):
                        with self.assertRaises(ValueError):
                            research.cmd_start(args)

    def test_output_directory_sources_exclude_public_progress_urls(self):
        response = result(text='Final evidence https://example.org/final')
        response.steps[0].summary[0].text = 'Progress https://example.net/unfinished'
        research._save_completed(response, None, 'reports', 'md', None, 'report')
        sources = json.loads(next(Path('reports').rglob('sources.json')).read_text())
        self.assertEqual(sources, ['https://example.org/final'])

    def test_cache_check_is_read_only_and_has_no_api_client(self):
        fake = FakeInteractions()
        self.start(fake, "--output", "completed.md")
        before = {p.name: p.read_bytes() for p in Path('.').iterdir() if p.is_file()}
        self.output.seek(0); self.output.truncate()
        args = research.build_parser().parse_args(["start", "Research the exact claim", "--agent", MAX, "--cache-check"])
        with patch.object(research, "get_client", side_effect=AssertionError("No client allowed")):
            research.cmd_start(args)
        answer = json.loads(self.output.getvalue())
        self.assertEqual(answer['status'], 'cache_hit')
        self.assertEqual(answer['id'], 'v1_fixture')
        self.assertEqual(before, {p.name:p.read_bytes() for p in Path('.').iterdir() if p.is_file()})

    def test_pending_request_resumes_without_a_second_create_even_with_bypass(self):
        fake = FakeInteractions()
        self.start(fake, "--metadata-output", "started.json")
        self.output.seek(0); self.output.truncate()
        self.start(fake, "--cache-check", "--no-cache")
        self.assertEqual(json.loads(self.output.getvalue())['status'], 'in_progress')
        self.start(fake, "--output", "resumed.md", "--metadata-output", "resumed.json", "--no-cache")
        self.assertEqual(len(fake.creates), 1)
        receipt = json.loads(Path('resumed.json').read_text())
        self.assertEqual(receipt['origin'], 'report')
        self.assertFalse(receipt['creation_performed'])
        self.assertEqual(set(fake.gets), {'v1_fixture'})

    def test_caller_timeout_later_resumes_same_id_and_preserves_deadline(self):
        clock = [0.0]
        class PendingProvider(FakeInteractions):
            def get(self, iid):
                clock[0] = 2.0
                return super().get(iid)
        fake = PendingProvider(result(status='in_progress', text=None))
        with patch.object(research.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(SystemExit):
                self.start(fake, '--output', 'pending.md', '--timeout', '1', '--metadata-output', 'pending.json')
        self.assertFalse(Path('pending.md').exists())
        self.assertEqual(research.request_status()['requests'][0]['id'], 'v1_fixture')
        fake.completed = result()
        self.start(fake, '--output', 'resumed.md', '--timeout', '1', '--metadata-output', 'resumed.json')
        self.assertEqual(len(fake.creates), 1)
        self.assertEqual(set(fake.gets), {'v1_fixture'})
        self.assertFalse(json.loads(Path('resumed.json').read_text())['creation_performed'])

    def test_accepted_identity_survives_claim_update_failure(self):
        fake = FakeInteractions()
        with patch.object(research, '_update_claim', side_effect=ValueError('Claim changed')), \
             patch.object(research.console, 'print') as output:
            with self.assertRaises(ValueError):
                self.start(fake, '--metadata-output', 'accepted.json')
        invocation = json.loads(Path('accepted.json.invocation.json').read_text())
        self.assertEqual(invocation['id'], 'v1_fixture')
        self.assertTrue(invocation['creation_performed'])
        self.assertIn('v1_fixture', research.load_state()['researchIds'])
        self.assertTrue(any('v1_fixture' in str(call) for call in output.call_args_list))

    def test_accepted_id_without_status_cannot_create_again(self):
        class Provider(FakeInteractions):
            def create(self, **kwargs):
                super().create(**kwargs)
                return SimpleNamespace(id='v1_fixture', status=None)
        fake = Provider()
        self.start(fake)
        self.output.seek(0); self.output.truncate()
        self.start(fake, '--cache-check', '--no-cache')
        self.assertEqual(json.loads(self.output.getvalue())['status'], 'in_progress')
        self.start(fake, '--no-cache')
        self.assertEqual(len(fake.creates), 1)

    def test_inline_payload_identity_excludes_retry_run_directory(self):
        keys = []
        for run in ('run-first', 'run-retry'):
            Path(run).mkdir()
            payload = Path(run) / 'part.txt'
            payload.write_text('Stable bounded claim evidence')
            self.output.seek(0); self.output.truncate()
            self.start(FakeInteractions(), '--file', str(payload), '--cache-check')
            keys.append(json.loads(self.output.getvalue())['cache_key'])
        self.assertEqual(keys[0], keys[1])

    def test_invocation_precedes_create_and_unresolved_does_not_expire(self):
        class Provider(FakeInteractions):
            def create(self, **kwargs):
                invocation = json.loads(Path('ambiguous.json.invocation.json').read_text())
                assert invocation['phase'] == 'creating'
                assert invocation['creation_performed'] is None
                return super().create(**kwargs)
        fake = Provider(error=TimeoutError('No trustworthy acceptance answer'))
        with self.assertRaises(SystemExit):
            self.start(fake, "--metadata-output", "ambiguous.json")
        records = research.load_state()['researchRequests']
        key = next(iter(records)); token = records[key]['claim_token']
        research._update_claim(key, token, created_at=0, updated_at=0)
        with self.assertRaises(RuntimeError):
            self.start(fake, "--no-cache")
        self.assertEqual(len(fake.creates), 1)
        self.assertEqual(research.request_status()['requests'][0]['status'], 'unresolved')
        self.assertGreater(research.request_status()['requests'][0]['age_seconds'], 86400)
        other = FakeInteractions()
        args = research.build_parser().parse_args(['start', 'Unrelated request', '--agent', MAX])
        with patch.object(research, 'get_client', return_value=SimpleNamespace(interactions=other)):
            research.cmd_start(args)
        self.assertEqual(len(other.creates), 1)

    def test_rejected429_clears_only_its_claim_and_records_no_create(self):
        error = RuntimeError('Explicit 429 rejection'); error.status_code=429
        fake = FakeInteractions(error=error)
        with self.assertRaises(SystemExit):
            self.start(fake, '--metadata-output', 'rejected.json')
        invocation=json.loads(Path('rejected.json.invocation.json').read_text())
        self.assertFalse(invocation['creation_performed'])
        self.assertEqual(research.request_status()['requests'], [])
        fake.error=None
        self.start(fake)
        self.assertEqual(len(fake.creates),2)
        self.assertEqual(research.load_state()['researchRequestHistory'][0]['status'],'rejected')

    def test_reconciliation_is_explicit_logged_and_claim_guarded(self):
        requested={'agent':MAX,'agent_config':dict(research.AGENT_CONFIG)}
        record,owned=research._claim_request('v2-test',requested)
        self.assertTrue(owned)
        args=research.build_parser().parse_args(['reconcile','v2-test','--expected-claim','wrong','--clear','--reason','Offline operator test'])
        with self.assertRaises(ValueError): research.cmd_reconcile(args)
        args=research.build_parser().parse_args(['reconcile','v2-test','--expected-claim',record['claim_token'],'--attach-id','v1_actual','--reason','Recovered accepted provider ID'])
        research.cmd_reconcile(args)
        state=research.load_state()
        self.assertEqual(state['researchRequests']['v2-test']['id'],'v1_actual')
        self.assertEqual(state['researchReconciliations'][0]['action'],'attach')
        other, _ = research._claim_request('v2-other', requested)
        args = research.build_parser().parse_args(['reconcile', 'v2-other', '--expected-claim', other['claim_token'], '--clear', '--reason', 'Confirmed provider never accepted'])
        research.cmd_reconcile(args)
        state = research.load_state()
        self.assertEqual(state['researchRequests']['v2-other']['status'], 'reconciled_clear')
        self.assertEqual(state['researchRequests']['v2-test']['id'], 'v1_actual')
        self.assertEqual(state['researchReconciliations'][-1]['action'], 'clear')

    def test_same_and_distinct_request_multiprocessing(self):
        context=multiprocessing.get_context('fork')
        for distinct in (False,True):
            with self.subTest(distinct=distinct):
                if Path('.gemini-research.json').exists(): Path('.gemini-research.json').unlink()
                calls=context.Value('i',0); barrier=context.Barrier(4)
                processes=[context.Process(target=concurrent_start,args=(f'Request {n if distinct else 0}',calls,barrier)) for n in range(4)]
                for process in processes: process.start()
                for process in processes:
                    process.join(15)
                    self.assertEqual(process.exitcode,0)
                self.assertEqual(calls.value,4 if distinct else 1)

    def test_real_sdk_does_not_retry_post_after_503_or_transport_error(self):
        import httpx
        real_factory=research.genai.Client
        for transport_error in (False, True):
            with self.subTest(transport_error=transport_error):
                seen=[]
                def handler(request):
                    seen.append(request)
                    if transport_error: raise httpx.ReadTimeout('Ambiguous offline transport failure',request=request)
                    return httpx.Response(503,json={'error':{'code':503,'status':'UNAVAILABLE','message':'Offline fixture'}})
                def factory(**kwargs):
                    options=kwargs.setdefault('http_options',research.types.HttpOptions())
                    options.client_args={'transport':httpx.MockTransport(handler)}
                    return real_factory(**kwargs)
                with patch.object(research.genai,'Client',side_effect=factory):
                    client=research.get_client()
                try:
                    with self.assertRaises(Exception):
                        client.interactions.create(agent=MAX,input='offline',background=True,store=True)
                finally:
                    client.close()
                self.assertEqual(len(seen),1)

    def test_real_sdk_rejected429_has_explicit_no_create_invocation(self):
        import httpx
        real_factory=research.genai.Client
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(429,json={'error':{'code':429,'status':'RESOURCE_EXHAUSTED','message':'Your project has exceeded its monthly spending cap.'}})
        def factory(**kwargs):
            options=kwargs.setdefault('http_options',research.types.HttpOptions())
            options.client_args={'transport':httpx.MockTransport(handler)}
            return real_factory(**kwargs)
        args=research.build_parser().parse_args(['start','offline429','--agent',MAX,'--metadata-output','rejected.json'])
        with patch.object(research.genai,'Client',side_effect=factory):
            with self.assertRaises(SystemExit): research.cmd_start(args)
        self.assertEqual(len(seen),1)
        invocation=json.loads(Path('rejected.json.invocation.json').read_text())
        self.assertEqual(invocation['http_status'],429)
        self.assertFalse(invocation['creation_performed'])
        self.assertEqual(research.request_status()['requests'],[])

    def test_uncacheable_followup_probe_never_fetches_or_changes_state(self):
        args=research.build_parser().parse_args(['start','offline','--follow-up','v1_old','--cache-check'])
        with patch.object(research,'get_client',side_effect=AssertionError('No API client in lookup')):
            research.cmd_start(args)
        answer=json.loads(self.output.getvalue())
        self.assertEqual(answer['status'],'cache_miss')
        self.assertFalse(answer['cacheable'])
        self.assertEqual(list(Path('.').iterdir()),[])


if __name__ == "__main__":
    unittest.main(verbosity=2)
