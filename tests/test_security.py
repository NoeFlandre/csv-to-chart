import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

import app as app_module


class ChartSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.upload_directory = self.root / "uploads"
        self.chart_directory = self.root / "charts"
        self.upload_directory.mkdir()
        self.chart_directory.mkdir()
        self.original_config = {
            key: app_module.app.config.get(key)
            for key in ("UPLOAD_FOLDER", "CHART_FOLDER", "TESTING")
        }
        app_module.app.config.update(
            UPLOAD_FOLDER=str(self.upload_directory),
            CHART_FOLDER=str(self.chart_directory),
            TESTING=True,
        )
        self.client = app_module.app.test_client()
        self.csv_path = self.upload_directory / "input.csv"
        self.csv_path.write_text(
            "Department,Salary,Age\n"
            "Engineering,100,30\n"
            "Sales,80,25\n"
            "Engineering,120,35\n",
            encoding="utf-8",
        )

    def tearDown(self):
        app_module.app.config.update(self.original_config)
        self.temp_directory.cleanup()

    @staticmethod
    def fake_client(content):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )
        return SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=Mock(return_value=response))
            )
        )

    def test_validates_and_renders_allowlisted_bar_spec(self):
        frame = pd.read_csv(self.csv_path)
        spec = app_module.validate_chart_spec(
            {
                "chart_type": "bar",
                "x": "Department",
                "y": "Salary",
                "aggregation": "mean",
                "title": "Average salary",
            },
            frame,
        )

        output_path = self.chart_directory / "chart.png"
        app_module.render_chart(frame, spec, output_path)

        self.assertTrue(output_path.is_file())
        self.assertGreater(output_path.stat().st_size, 0)

    def test_rejects_unknown_fields_and_columns(self):
        frame = pd.read_csv(self.csv_path)
        with self.assertRaises(app_module.InvalidChartSpec):
            app_module.validate_chart_spec(
                {
                    "chart_type": "bar",
                    "x": "Department",
                    "code": "not allowed",
                },
                frame,
            )
        with self.assertRaises(app_module.InvalidChartSpec):
            app_module.validate_chart_spec(
                {"chart_type": "scatter", "x": "Unknown", "y": "Salary"},
                frame,
            )

    def test_histogram_rejects_invalid_bins(self):
        frame = pd.read_csv(self.csv_path)
        with self.assertRaises(app_module.InvalidChartSpec):
            app_module.validate_chart_spec(
                {"chart_type": "histogram", "x": "Age", "bins": 1000},
                frame,
            )

    def test_chart_endpoint_renders_json_spec_without_logging_api_key(self):
        fake_api_key = "test-only-key-do-not-use"
        fake_openai = self.fake_client(
            json.dumps(
                {
                    "chart_type": "bar",
                    "x": "Department",
                    "y": "Salary",
                    "aggregation": "mean",
                }
            )
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(app_module, "OpenAI", return_value=fake_openai) as openai,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            response = self.client.post(
                "/chart",
                json={
                    "filepath": str(self.csv_path),
                    "question": "Average salary by department",
                    "api_key": fake_api_key,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"chart_url": "/static/chart.png"})
        self.assertTrue((self.chart_directory / "chart.png").is_file())
        openai.assert_called_once_with(
            base_url="https://openrouter.ai/api/v1",
            api_key=fake_api_key,
        )
        self.assertNotIn(fake_api_key, stdout.getvalue())
        self.assertNotIn(fake_api_key, stderr.getvalue())

    def test_provider_exception_does_not_echo_api_key(self):
        fake_api_key = "test-only-key-do-not-use"
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch.object(
                app_module,
                "OpenAI",
                side_effect=RuntimeError(f"provider failed for {fake_api_key}"),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            response = self.client.post(
                "/chart",
                json={
                    "filepath": str(self.csv_path),
                    "question": "Show a chart",
                    "api_key": fake_api_key,
                },
            )

        self.assertEqual(response.status_code, 502)
        self.assertNotIn(fake_api_key, response.get_data(as_text=True))
        self.assertNotIn(fake_api_key, stdout.getvalue())
        self.assertNotIn(fake_api_key, stderr.getvalue())

    def test_model_python_text_is_rejected_without_execution(self):
        marker = self.root / "should-not-exist"
        model_text = (
            "__import__('pathlib').Path("
            + repr(str(marker))
            + ").write_text('executed')"
        )
        fake_openai = self.fake_client(model_text)

        with patch.object(app_module, "OpenAI", return_value=fake_openai):
            response = self.client.post(
                "/chart",
                json={
                    "filepath": str(self.csv_path),
                    "question": "Create a chart",
                    "api_key": "test-only-key",
                },
            )

        self.assertEqual(response.status_code, 502)
        self.assertFalse(marker.exists())
        self.assertFalse((self.chart_directory / "chart.png").exists())

    def test_chart_endpoint_rejects_files_outside_upload_directory(self):
        outside_csv = self.root / "outside.csv"
        outside_csv.write_text("secret,value\nprivate,1\n", encoding="utf-8")

        with patch.object(app_module, "OpenAI") as openai:
            response = self.client.post(
                "/chart",
                json={
                    "filepath": str(outside_csv),
                    "question": "Show a chart",
                    "api_key": "test-only-key",
                },
            )

        self.assertEqual(response.status_code, 400)
        openai.assert_not_called()

    def test_upload_sanitizes_client_filename(self):
        response = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(b"name,value\nsample,1\n"), "../../outside.csv")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        response_data = json.loads(response.get_data(as_text=True))
        uploaded_path = Path(response_data["filepath"])
        self.assertEqual(uploaded_path.parent, self.upload_directory)
        self.assertTrue(uploaded_path.is_file())
        self.assertFalse((self.root / "outside.csv").exists())


if __name__ == "__main__":
    unittest.main()
