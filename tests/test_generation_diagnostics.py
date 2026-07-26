import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import openai

from omnipet.cli import main
from omnipet.diagnostics import CATEGORIES, SafeDiagnostic, openai_diagnostic
from omnipet.generation import ImageRequest
from omnipet.openai_images import (
    OpenAIImageGenerator,
    OpenAIRequestError,
    OpenAIResponseError,
    OpenAIValidationError,
)
from omnipet.package import PackageError
from omnipet.project import load_pet_project
from omnipet.release import hatch_project, init_pet_project, project_status
from omnipet.run import prepare_run
from omnipet.workflow import WorkflowState, load_workflow, mark_blocked


class _Images:
    def __init__(self, error):
        self.error = error

    def generate(self, **_kwargs):
        raise self.error


class _Client:
    max_retries = 0

    def __init__(self, error):
        self.images = _Images(error)

    def with_options(self, **_kwargs):
        return self


class GenerationDiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_categories_are_closed_and_diagnostic_is_frozen(self):
        self.assertEqual(CATEGORIES, (
            "local-validation", "missing-credentials", "authentication",
            "authorization", "rate-limit", "provider-timeout",
            "provider-request", "provider-response", "deterministic-qa",
            "publication",
        ))
        diagnostic = SafeDiagnostic("rate-limit", 429, "req_safe-123", True)
        self.assertEqual(diagnostic.to_dict(), {
            "category": "rate-limit", "status": 429,
            "request_id": "req_safe-123", "retryable": True,
        })
        with self.assertRaises((AttributeError, TypeError)):
            diagnostic.status = 200

    def test_diagnostic_rejects_invalid_or_sensitive_fields(self):
        cases = (
            ("unknown", None, None, False),
            ("provider-request", True, None, False),
            ("provider-request", 99, None, False),
            ("provider-request", None, "x" * 129, False),
            ("provider-request", None, "Bearer private-token", False),
            ("provider-request", None, "req_safe", 1),
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                SafeDiagnostic(*values)

    def test_official_sdk_exceptions_map_without_message_or_body(self):
        request = httpx.Request("POST", "https://api.openai.com/v1/images")

        def status_error(error_type, status):
            response = httpx.Response(
                status, request=request, headers={"x-request-id": "req_safe"}
            )
            return error_type(
                "Bearer private-token prompt text",
                response=response,
                body={"api_key": "sk-private-value"},
            )

        cases = (
            (status_error(openai.AuthenticationError, 401), "authentication", 401, False),
            (status_error(openai.PermissionDeniedError, 403), "authorization", 403, False),
            (status_error(openai.RateLimitError, 429), "rate-limit", 429, True),
            (openai.APITimeoutError(request=request), "provider-timeout", None, True),
            (openai.APIConnectionError(message="sk-private-value", request=request), "provider-request", None, True),
            (status_error(openai.BadRequestError, 400), "provider-request", 400, False),
            (status_error(openai.APIStatusError, 408), "provider-request", 408, True),
            (status_error(openai.APIStatusError, 409), "provider-request", 409, True),
        )
        for error, category, status, retryable in cases:
            with self.subTest(category=category):
                diagnostic = openai_diagnostic(error)
                self.assertEqual(
                    (diagnostic.category, diagnostic.status, diagnostic.retryable),
                    (category, status, retryable),
                )
                self.assertNotIn("private", json.dumps(diagnostic.to_dict()))

    def test_unknown_exception_falls_back_without_inspection(self):
        class HostileError(Exception):
            def __str__(self):
                raise AssertionError("exception text inspected")

        self.assertEqual(
            openai_diagnostic(HostileError()).to_dict(),
            SafeDiagnostic("provider-request", retryable=False).to_dict(),
        )

    def test_credential_like_provider_request_id_is_dropped(self):
        request = httpx.Request("POST", "https://api.openai.com/v1/images")
        response = httpx.Response(
            429, request=request, headers={"x-request-id": "sk-private-value"}
        )
        error = openai.RateLimitError("safe", response=response, body=None)

        self.assertIsNone(openai_diagnostic(error).request_id)

    def test_missing_credentials_and_local_validation_are_distinct_and_sanitized(self):
        request = self._request()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(OpenAIValidationError) as raised:
                OpenAIImageGenerator().generate(request)
        self.assertEqual(raised.exception.diagnostic.category, "missing-credentials")
        self.assertEqual(str(raised.exception), "OpenAI image request validation failed")
        self.assertIsNone(raised.exception.__cause__)

        bad = self._request(destination=self.root / "outside.png")
        with self.assertRaises(OpenAIValidationError) as raised:
            OpenAIImageGenerator(client=_Client(RuntimeError())).generate(bad)
        self.assertEqual(raised.exception.diagnostic.category, "local-validation")

    def test_adapter_preserves_safe_sdk_diagnostics_and_malformed_response(self):
        request = httpx.Request("POST", "https://api.openai.com/v1/images")
        response = httpx.Response(
            429, request=request, headers={"x-request-id": "req_rate-safe"}
        )
        error = openai.RateLimitError(
            "Bearer private-token", response=response, body=b"uploaded bytes"
        )
        with self.assertRaises(OpenAIRequestError) as raised:
            OpenAIImageGenerator(client=_Client(error)).generate(self._request())
        self.assertEqual(raised.exception.diagnostic.to_dict(), {
            "category": "rate-limit", "status": 429,
            "request_id": "req_rate-safe", "retryable": True,
        })
        self.assertEqual(str(raised.exception), "OpenAI image request failed")
        self.assertIsNone(raised.exception.__cause__)

        malformed = SimpleNamespace(data=[])
        client = _Client(RuntimeError())
        client.images.generate = lambda **_kwargs: malformed
        with self.assertRaises(OpenAIResponseError) as raised:
            OpenAIImageGenerator(client=client).generate(self._request())
        self.assertEqual(raised.exception.diagnostic.category, "provider-response")

    def test_workflow_persists_closed_diagnostic_and_loads_legacy_record(self):
        run_dir = self._run_dir()
        diagnostic = SafeDiagnostic("authentication", 401, "req_safe", False)
        state = mark_blocked(
            run_dir, code="generation-failed", job="base", evidence=None,
            diagnostic=diagnostic,
        )
        self.assertEqual(state.blocked["diagnostic"], diagnostic.to_dict())
        self.assertEqual(load_workflow(run_dir), state)

        path = run_dir / "workflow.json"
        legacy = {
            "schema_version": 1, "state": "blocked",
            "blocked": {"code": "job-failed", "job": "base", "evidence": None},
        }
        path.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(load_workflow(run_dir).blocked, legacy["blocked"])

        state.blocked["diagnostic"]["extra"] = "secret"
        path.write_text(json.dumps({
            "schema_version": 1, "state": "blocked", "blocked": state.blocked,
        }), encoding="utf-8")
        with self.assertRaises(Exception):
            load_workflow(run_dir)

    def test_new_blocks_always_persist_nullable_diagnostic(self):
        run_dir = self._run_dir()

        explicit = mark_blocked(
            run_dir, code="manual-review", job="base", evidence=None
        )

        self.assertEqual(explicit.blocked, {
            "code": "manual-review", "job": "base", "evidence": None,
            "diagnostic": None,
        })

        project = self._project("failed-pet")
        failed_run = prepare_run(project, self.root).run_dir
        manifest_path = failed_run / "imagegen-jobs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["jobs"][0]["status"] = "failed"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        refreshed = __import__(
            "omnipet.workflow", fromlist=["refresh_workflow"]
        ).refresh_workflow(failed_run)

        self.assertEqual(refreshed.blocked, {
            "code": "job-failed", "job": "base", "evidence": None,
            "diagnostic": None,
        })

    def test_generation_orchestration_exposes_diagnostic_and_bounded_action(self):
        project = self._project()
        diagnostic = SafeDiagnostic("provider-timeout", retryable=True)

        class FailingGenerator:
            def generate(self, _request):
                raise OpenAIRequestError(diagnostic)

        state = hatch_project(project, generator_factory=lambda _project: FailingGenerator())
        status = project_status(project)
        self.assertEqual(state.blocked["diagnostic"], diagnostic.to_dict())
        self.assertEqual(status["blocked"]["diagnostic"], diagnostic.to_dict())
        self.assertEqual(status["next_action"], "omnipet hatch my-pet --reset-failed base")
        self.assertLessEqual(len(status["next_action"]), 256)

    def test_only_status_cli_exposes_blocked_diagnostic(self):
        self._project()
        diagnostic = SafeDiagnostic("rate-limit", 429, "req_safe", True).to_dict()
        public = {
            "ok": True,
            "pet_id": "my-pet",
            "workflow_state": "blocked",
            "next_action": "omnipet hatch my-pet --reset-failed base",
            "blocked": {
                "code": "generation-failed", "job": "base", "evidence": None,
                "diagnostic": diagnostic,
            },
        }
        commands = (
            (["status", "my-pet"], None),
            (["hatch", "my-pet"], "hatch_project"),
            (["approve", "my-pet", "--stage", "base"], "approve_project_stage"),
            (["qa", "my-pet", "--stage", "base"], "qa_project_stage"),
        )
        state = SimpleNamespace(state="blocked")
        for arguments, operation in commands:
            with self.subTest(command=arguments[0]):
                output = io.StringIO()
                patches = [patch("omnipet.cli.project_status", return_value=public.copy())]
                if operation is not None:
                    patches.append(patch(f"omnipet.cli.{operation}", return_value=state))
                with ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    stack.enter_context(redirect_stdout(output))
                    main([*arguments, "--repo-root", str(self.root)])
                payload = json.loads(output.getvalue())
                blocked = payload["blocked"]
                if arguments[0] == "status":
                    self.assertEqual(blocked["diagnostic"], diagnostic)
                else:
                    self.assertNotIn("diagnostic", blocked)

    def test_status_never_exposes_an_unbounded_action(self):
        project = SimpleNamespace(
            repository_root=self.root,
            root=self.root / "pets" / ("a" * 300),
            pet_id="a" * 300,
        )

        status = project_status(project)

        self.assertEqual(status["next_action"], "none")

    def test_output_qa_and_package_failures_have_stage_specific_diagnostics(self):
        project = self._project()

        class MissingOutputGenerator:
            def generate(self, request):
                return SimpleNamespace(path=request.destination)

        failed = hatch_project(
            project, generator_factory=lambda _project: MissingOutputGenerator()
        )
        self.assertEqual(failed.blocked["diagnostic"]["category"], "deterministic-qa")

        other = self._project("package-pet")
        run_dir = prepare_run(other, self.root).run_dir
        with patch("omnipet.release.prepare_run", return_value=SimpleNamespace(run_dir=run_dir)), patch(
            "omnipet.release.refresh_workflow",
            return_value=WorkflowState("building_package"),
        ), patch("omnipet.release.build_package_evidence", side_effect=RuntimeError("private")):
            blocked = hatch_project(other)
        self.assertEqual(blocked.blocked["diagnostic"]["category"], "publication")

    def test_package_deterministic_failure_is_not_publication(self):
        project = self._project()
        run_dir = prepare_run(project, self.root).run_dir
        with patch(
            "omnipet.release.prepare_run",
            return_value=SimpleNamespace(run_dir=run_dir),
        ), patch(
            "omnipet.release.refresh_workflow",
            return_value=WorkflowState("building_package"),
        ), patch(
            "omnipet.release.build_package_evidence",
            side_effect=PackageError("direction continuity failed"),
        ):
            blocked = hatch_project(project)

        self.assertEqual(
            blocked.blocked["diagnostic"]["category"], "deterministic-qa"
        )

    def _request(self, destination=None):
        generated = self.root / "generated-sources"
        generated.mkdir(exist_ok=True)
        return ImageRequest(
            prompt="private prompt", destination=destination or generated / "result.png",
            run_root=self.root, aspect_ratio="1:1", image_size="1K", task="base",
        )

    def _run_dir(self):
        project = self._project()
        return prepare_run(project, self.root).run_dir

    def _project(self, pet_id="my-pet"):
        init_pet_project(self.root, pet_id)
        return load_pet_project(self.root, pet_id)


if __name__ == "__main__":
    unittest.main()
