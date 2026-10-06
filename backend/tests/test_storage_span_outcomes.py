"""Verify storage absence classification in real, locally exported Logfire spans."""

import logfire
import pytest
from botocore.exceptions import ClientError
from logfire.testing import TestExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from oddish import observability
from oddish.db.storage import storage_operation
from oddish.timing import begin_request_timing, reset_request_timing


@pytest.fixture
def storage_spans(monkeypatch):
    exporter = TestExporter()
    tracer = logfire.configure(
        local=True,
        send_to_logfire=False,
        console=False,
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )
    monkeypatch.setattr(observability, "_configured", True)
    monkeypatch.setattr(logfire, "span", tracer.span)
    return exporter


@pytest.mark.parametrize("operation", ["get", "head"])
@pytest.mark.parametrize(
    "response, missing",
    [
        ({"Error": {"Code": "NoSuchKey"}}, True),
        ({"ResponseMetadata": {"HTTPStatusCode": 404}}, True),
        ({"Error": {"Code": ""}, "ResponseMetadata": {"HTTPStatusCode": 404}}, True),
        ({"Error": {"Code": ""}}, False),
        ({"ResponseMetadata": {"HTTPStatusCode": 403}}, False),
        ({"ResponseMetadata": {"HTTPStatusCode": 503}}, False),
        (
            {
                "Error": {"Code": "NoSuchBucket"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            False,
        ),
    ],
)
def test_read_span_distinguishes_absence(
    storage_spans, caplog, operation, response, missing
):
    error = ClientError(response, operation)
    timing, *tokens = begin_request_timing()
    try:
        with observability.span("caller"):
            with pytest.raises(ClientError) as caught:
                with storage_operation(operation, "task/file.txt"):
                    raise error
        assert caught.value is error
        assert timing.storage_request_count == 1
        assert timing.durations_ms[f"storage_{operation}"] >= 0
    finally:
        reset_request_timing(tuple(tokens))

    span, caller = storage_spans.exported_spans_as_dict()
    assert span["name"] == f"backend.storage.{operation}"
    assert span["attributes"]["storage.key"] == "task/file.txt"
    assert span["attributes"].get("storage.outcome") == (
        "not_found" if missing else None
    )
    assert (span["attributes"].get("logfire.level_num", 0) >= 17) is not missing
    assert bool(span.get("events")) is not missing
    assert (f"S3 {operation} failed" in caplog.text) is not missing
    assert not caller.get("events")


@pytest.mark.parametrize("operation", ["get", "list", "delete"])
def test_required_missing_object_still_fails_caller(storage_spans, operation):
    error = ClientError({"Error": {"Code": "NoSuchKey"}}, operation)
    with pytest.raises(ClientError) as caught:
        with observability.span("required_file"):
            with storage_operation(operation, "task/file.txt"):
                raise error
    assert caught.value is error
    span, caller = storage_spans.exported_spans_as_dict()
    assert bool(span.get("events")) is (operation != "get")
    assert caller["attributes"]["logfire.level_num"] >= 17
    assert caller["events"][0]["attributes"]["exception.type"].endswith("ClientError")


def test_success_and_transport_failure_remain_visible(storage_spans):
    with storage_operation("get", "task/file.txt"):
        pass
    with pytest.raises(TimeoutError):
        with storage_operation("get", "task/file.txt"):
            raise TimeoutError("storage timed out")
    success, failure = storage_spans.exported_spans_as_dict()
    assert not success.get("events")
    assert "storage.outcome" not in success["attributes"]
    assert failure["attributes"]["logfire.level_num"] >= 17
    assert failure["events"][0]["attributes"]["exception.type"] == "TimeoutError"
