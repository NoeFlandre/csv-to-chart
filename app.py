import json
import shutil
from pathlib import Path
from typing import cast
from uuid import uuid4

# Matplotlib's backend must be selected before pyplot is imported.
# isort: off
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from flask import Flask, jsonify, render_template, request
from openai import OpenAI, OpenAIError
from werkzeug.utils import secure_filename
# isort: on


app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["CHART_FOLDER"] = "static"

_ALLOWED_CHART_TYPES = {"bar", "line", "scatter", "histogram", "pie"}
_ALLOWED_AGGREGATIONS = {"count", "sum", "mean", "median"}
_MAX_LABEL_LENGTH = 120
_MAX_CATEGORIES = 100


class InvalidChartSpec(ValueError):
    """Raised when the model returns a chart request outside the supported schema."""


def _configured_directory(config_key: str) -> Path:
    directory = Path(app.config[config_key])
    if not directory.is_absolute():
        directory = Path(app.root_path) / directory
    return directory.resolve()


def _uploaded_csv_path(filepath: object) -> Path | None:
    if not isinstance(filepath, str) or not filepath.strip():
        return None

    requested = Path(filepath)
    if not requested.is_absolute():
        requested = Path(app.root_path) / requested
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    upload_directory = _configured_directory("UPLOAD_FOLDER")
    if (
        resolved.parent != upload_directory
        or resolved.suffix.lower() != ".csv"
        or not resolved.is_file()
    ):
        return None
    return resolved


def _chart_output_path() -> Path:
    output_path = _configured_directory("CHART_FOLDER") / "chart.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path


def _optional_text(payload: dict, field: str) -> str | None:
    if field not in payload:
        return None
    value = payload[field]
    if not isinstance(value, str):
        raise InvalidChartSpec(f"{field} must be text")
    value = value.strip()
    if not value or len(value) > _MAX_LABEL_LENGTH:
        raise InvalidChartSpec(
            f"{field} must contain 1 to {_MAX_LABEL_LENGTH} characters"
        )
    return value


def validate_chart_spec(payload: object, frame: pd.DataFrame) -> dict:
    if not isinstance(payload, dict):
        raise InvalidChartSpec("chart specification must be an object")

    allowed_fields = {
        "chart_type",
        "x",
        "y",
        "aggregation",
        "bins",
        "title",
        "x_label",
        "y_label",
    }
    if set(payload) - allowed_fields:
        raise InvalidChartSpec("chart specification contains unsupported fields")

    chart_type = payload.get("chart_type")
    if not isinstance(chart_type, str) or chart_type not in _ALLOWED_CHART_TYPES:
        raise InvalidChartSpec("chart type is unsupported")

    columns = {str(column) for column in frame.columns}
    x_column = payload.get("x")
    if not isinstance(x_column, str) or x_column not in columns:
        raise InvalidChartSpec("x must name a CSV column")

    y_column = payload.get("y")
    if y_column is not None and (
        not isinstance(y_column, str) or y_column not in columns
    ):
        raise InvalidChartSpec("y must name a CSV column")

    aggregation = payload.get("aggregation")
    if aggregation is not None and (
        not isinstance(aggregation, str) or aggregation not in _ALLOWED_AGGREGATIONS
    ):
        raise InvalidChartSpec("aggregation is unsupported")

    bins = payload.get("bins", 20)
    if chart_type == "histogram":
        if y_column is not None or aggregation is not None:
            raise InvalidChartSpec(
                "histograms use one numeric column and no aggregation"
            )
        if isinstance(bins, bool) or not isinstance(bins, int) or not 5 <= bins <= 50:
            raise InvalidChartSpec("histogram bins must be an integer from 5 to 50")
        if not pd.api.types.is_numeric_dtype(frame[x_column]):
            raise InvalidChartSpec("histograms require a numeric x column")
    else:
        if "bins" in payload:
            raise InvalidChartSpec("bins are only supported for histograms")
        bins = None

    if chart_type in {"bar", "pie"}:
        if aggregation is None:
            aggregation = "mean" if y_column is not None else "count"
        if y_column is None and aggregation != "count":
            raise InvalidChartSpec("an aggregation other than count requires y")
        if (
            y_column is not None
            and aggregation != "count"
            and not pd.api.types.is_numeric_dtype(frame[y_column])
        ):
            raise InvalidChartSpec("this aggregation requires a numeric y column")
    elif chart_type == "line":
        if y_column is None:
            raise InvalidChartSpec("line charts require y")
        if aggregation is None and not pd.api.types.is_numeric_dtype(frame[y_column]):
            raise InvalidChartSpec("line charts require a numeric y column")
        if aggregation not in {None, *_ALLOWED_AGGREGATIONS}:
            raise InvalidChartSpec("line aggregation is unsupported")
        if aggregation in {
            "sum",
            "mean",
            "median",
        } and not pd.api.types.is_numeric_dtype(frame[y_column]):
            raise InvalidChartSpec("this aggregation requires a numeric y column")
    elif chart_type == "scatter":
        if y_column is None or aggregation is not None:
            raise InvalidChartSpec("scatter charts require x and y without aggregation")
        if not (
            pd.api.types.is_numeric_dtype(frame[x_column])
            and pd.api.types.is_numeric_dtype(frame[y_column])
        ):
            raise InvalidChartSpec("scatter charts require numeric x and y columns")

    title = _optional_text(payload, "title")
    x_label = _optional_text(payload, "x_label")
    y_label = _optional_text(payload, "y_label")

    return {
        "chart_type": chart_type,
        "x": x_column,
        "y": y_column,
        "aggregation": aggregation,
        "bins": bins,
        "title": title or f"{chart_type.title()} chart",
        "x_label": x_label or x_column,
        "y_label": y_label or (y_column or aggregation or ""),
    }


def _aggregate_categories(
    frame: pd.DataFrame,
    x_column: str,
    y_column: str | None,
    aggregation: str,
) -> pd.Series:
    grouped = frame.groupby(x_column, dropna=False, sort=False)
    if y_column is None:
        return cast(pd.Series, grouped.size())
    values = grouped[y_column]
    if aggregation == "count":
        return cast(pd.Series, values.count())
    if aggregation == "sum":
        return values.sum()
    if aggregation == "mean":
        return values.mean()
    if aggregation == "median":
        return cast(pd.Series, values.median())
    raise InvalidChartSpec("aggregation is unsupported")


def _category_label(value: object) -> str:
    return "(missing)" if pd.isna(value) else str(value)


def render_chart(frame: pd.DataFrame, spec: dict, output_path: Path) -> None:
    chart_type = spec["chart_type"]
    x_column = spec["x"]
    y_column = spec["y"]
    aggregation = spec["aggregation"]

    figure, axes = plt.subplots(figsize=(10, 6))
    try:
        if chart_type in {"bar", "pie"} or (
            chart_type == "line" and aggregation is not None
        ):
            values = _aggregate_categories(frame, x_column, y_column, aggregation)
            if len(values) > _MAX_CATEGORIES:
                raise InvalidChartSpec(
                    f"categorical charts support at most {_MAX_CATEGORIES} x values"
                )
            labels = [_category_label(value) for value in values.index]
            if chart_type == "bar":
                axes.bar(labels, values.to_list())
                axes.tick_params(axis="x", labelrotation=45)
            elif chart_type == "pie":
                if (values < 0).any() or values.sum() <= 0:
                    raise InvalidChartSpec("pie chart values must be nonnegative")
                axes.pie(values.to_list(), labels=labels, autopct="%1.1f%%")
                axes.set_ylabel("")
            else:
                axes.plot(labels, values.to_list(), marker="o")
                axes.tick_params(axis="x", labelrotation=45)
        elif chart_type == "line":
            axes.plot(frame[x_column], frame[y_column], marker="o")
        elif chart_type == "scatter":
            axes.scatter(frame[x_column], frame[y_column])
        elif chart_type == "histogram":
            axes.hist(frame[x_column].dropna(), bins=spec["bins"])
        else:
            raise InvalidChartSpec("chart type is unsupported")

        axes.set_title(spec["title"])
        if chart_type != "pie":
            axes.set_xlabel(spec["x_label"])
            axes.set_ylabel(spec["y_label"])
        figure.tight_layout()
        figure.savefig(output_path, format="png")
    finally:
        plt.close(figure)


def _chart_prompt(frame: pd.DataFrame, question: str) -> str:
    columns = [
        {"name": str(column), "dtype": str(frame[column].dtype)}
        for column in frame.columns
    ]
    sample = frame.head(5).to_csv(index=False)
    return f"""Choose a chart specification for the user's question using only the CSV columns below.
Return exactly one JSON object and no Markdown or source code.
The object must use these fields only: chart_type, x, y, aggregation, bins, title, x_label, y_label.
Allowed chart_type values: bar, line, scatter, histogram, pie.
Use exact CSV column names. Use y for bar, line, scatter, and optionally pie; omit y for count charts and histograms.
Allowed aggregation values: count, sum, mean, median. Use aggregation only for bar, line, or pie.
For histogram, use bins as an integer from 5 to 50.
Example: {{"chart_type":"bar","x":"Department","y":"Salary","aggregation":"mean","title":"Average salary by department"}}

CSV columns: {json.dumps(columns, ensure_ascii=False)}
Sample rows:
{sample}

User question: {question}
"""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    uploaded_file = request.files["file"]
    if not uploaded_file.filename:
        return jsonify({"error": "No selected file"}), 400

    safe_filename = secure_filename(uploaded_file.filename)
    if not safe_filename or Path(safe_filename).suffix.lower() != ".csv":
        return jsonify({"error": "Invalid file type. Please upload a CSV file."}), 400

    upload_directory = _configured_directory("UPLOAD_FOLDER")
    upload_directory.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex}_{safe_filename}"
    destination = upload_directory / filename
    uploaded_file.save(destination)
    return jsonify({"filepath": str(destination)})


@app.route("/use-sample", methods=["POST"])
def use_sample():
    sample_path = Path(app.root_path) / "sample.csv"
    if not sample_path.is_file():
        return jsonify({"error": "Sample file not found"}), 404

    upload_directory = _configured_directory("UPLOAD_FOLDER")
    upload_directory.mkdir(parents=True, exist_ok=True)
    destination = upload_directory / f"sample_{uuid4().hex}.csv"
    try:
        shutil.copy2(sample_path, destination)
    except OSError:
        return jsonify({"error": "Unable to prepare sample data"}), 500
    return jsonify({"filepath": str(destination)})


@app.route("/chart", methods=["POST"])
def generate_chart():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON data"}), 400

    csv_path = _uploaded_csv_path(data.get("filepath"))
    question = data.get("question")
    api_key = data.get("api_key")
    if csv_path is None:
        return jsonify({"error": "Invalid file path or file type"}), 400
    if not isinstance(question, str) or not question.strip() or len(question) > 1000:
        return jsonify({"error": "Missing or invalid question"}), 400
    if not isinstance(api_key, str) or not api_key.strip():
        return jsonify(
            {"error": "Missing API key. Please provide your OpenRouter API key."}
        ), 400

    try:
        frame = pd.read_csv(csv_path)
    except pd.errors.EmptyDataError:
        return jsonify({"error": "CSV file is empty or invalid"}), 400
    except pd.errors.ParserError:
        return jsonify({"error": "CSV file format is invalid"}), 400
    if frame.empty:
        return jsonify({"error": "CSV file is empty"}), 400

    try:
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key.strip(),
        )
        completion = client.chat.completions.create(
            model="deepseek/deepseek-chat-v3.1:free",
            messages=[
                {"role": "user", "content": _chart_prompt(frame, question.strip())}
            ],
        )
    except OpenAIError as error:
        if getattr(error, "status_code", None) == 401:
            return jsonify(
                {
                    "error": "API authentication failed. Please check your OpenRouter API key."
                }
            ), 401
        return jsonify({"error": "Chart generation failed. Please try again."}), 502
    except Exception:  # noqa: BLE001 -- provider exceptions can contain request credentials
        return jsonify({"error": "Chart generation failed. Please try again."}), 502

    try:
        content = completion.choices[0].message.content
        if not isinstance(content, str):
            raise InvalidChartSpec("model response must be JSON text")
        spec = validate_chart_spec(json.loads(content), frame)
    except (
        AttributeError,
        IndexError,
        TypeError,
        json.JSONDecodeError,
        InvalidChartSpec,
    ):
        return jsonify(
            {"error": "The chart service returned an invalid chart specification."}
        ), 502

    try:
        render_chart(frame, spec, _chart_output_path())
    except (
        InvalidChartSpec,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        OverflowError,
    ):
        return jsonify({"error": "Chart generation failed. Please try again."}), 502

    return jsonify({"chart_url": "/static/chart.png"})


if __name__ == "__main__":
    _configured_directory("UPLOAD_FOLDER").mkdir(parents=True, exist_ok=True)
    _configured_directory("CHART_FOLDER").mkdir(parents=True, exist_ok=True)
    app.run(debug=False, port=5001)
