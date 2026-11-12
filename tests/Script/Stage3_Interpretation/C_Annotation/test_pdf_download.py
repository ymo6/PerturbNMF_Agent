"""pdf_download.pmid_to_ids: ID converter first, E-utilities esummary when the converter gives nothing (no network)."""

import importlib.util
from pathlib import Path

import pytest

requests = pytest.importorskip("requests")

LIB = (
    Path(__file__).resolve().parents[4]
    / "src/Stage3_Interpretation/C_Annotation/agent/paper-fetch-skill/scripts/_lib/pdf_download.py"
)
spec = importlib.util.spec_from_file_location("pdf_download_under_test", LIB)
pdl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pdl)


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


def fake_get(converter, esummary, seen):
    def get(url, params=None, timeout=None, headers=None, **kw):
        seen.append((url, headers))
        return converter if url == pdl.ID_CONVERTER else esummary

    return get


def test_converter_answer_is_used(monkeypatch):
    seen = []
    conv = Resp(200, {"records": [{"pmcid": "PMC1", "doi": "10.1/x"}]})
    monkeypatch.setattr(pdl.requests, "get", fake_get(conv, None, seen))
    assert pdl.pmid_to_ids("123") == {"pmcid": "PMC1", "doi": "10.1/x"}
    assert len(seen) == 1 and "python-requests" not in seen[0][1]["User-Agent"]


def test_esummary_fallback_when_converter_refuses(monkeypatch):
    seen = []
    summ = Resp(
        200,
        {
            "result": {
                "123": {
                    "articleids": [
                        {"idtype": "pmc", "value": "PMC9"},
                        {"idtype": "doi", "value": "10.2/y"},
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(pdl.requests, "get", fake_get(Resp(403, {}), summ, seen))
    assert pdl.pmid_to_ids("123") == {"pmcid": "PMC9", "doi": "10.2/y"}
    assert [u for u, _ in seen] == [pdl.ID_CONVERTER, pdl.ESUMMARY]


def test_unknown_pmid_gives_empty_dict(monkeypatch):
    summ = Resp(200, {"result": {"123": {"articleids": []}}})
    monkeypatch.setattr(pdl.requests, "get", fake_get(Resp(403, {}), summ, []))
    assert pdl.pmid_to_ids("123") == {}
    monkeypatch.setattr(pdl.requests, "get", fake_get(Resp(403, {}), Resp(500, {}), []))
    assert pdl.pmid_to_ids("123") == {}
