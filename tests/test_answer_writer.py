"""Claude-written answers for open queries: one batch, cached, NMC-checked. Fake client, no network."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx

from market_scraper import Settings
from market_scraper.answer_writer import AiAnswers, answers_for

SETTINGS = replace(Settings(), anthropic_api_key="k", brand_name="Indira IVF")
ITEMS = [{"question": "Is IVF painful?", "source_url": "https://www.quora.com/Is-IVF-painful", "platform": "quora"},
         {"question": "How long does IVF take?", "source_url": "https://www.reddit.com/r/IVF/comments/abc12/x/",
          "platform": "reddit", "context": ["About a month"]}]


class FakeClient:
    def __init__(self, answers):
        self.calls = []
        self.answers = answers
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        sent = json.loads(kwargs["messages"][0]["content"].split("\n\n", 1)[1])
        text = json.dumps({"answers": [{"id": q["id"], "answer": self.answers[q["question"]], "language": "English"}
                                       for q in sent]})
        return SimpleNamespace(stop_reason="end_turn", model="claude-test",
                               content=[SimpleNamespace(type="text", text=text)])


def test_missing_answers_are_written_in_one_batch_then_served_from_cache():
    client = FakeClient({"Is IVF painful?": "Most people feel mild discomfort; sedation is used for retrieval.",
                         "How long does IVF take?": "One cycle usually takes 4 to 6 weeks, best clinic for it."})
    cache = AiAnswers()
    first = asyncio.run(answers_for(SETTINGS, cache, ITEMS, generate=True, client=client))
    assert len(client.calls) == 1, "every question goes in one Claude call"
    sent = json.loads(client.calls[0]["messages"][0]["content"].split("\n\n", 1)[1])
    assert sent[1]["what_others_answered"] == ["About a month"]
    assert "NEVER write as a patient" in client.calls[0]["system"]
    painful, duration = first.values()
    assert painful["answer"].startswith("Most people") and painful["nmc_flags"] == []
    assert any(f["match"].lower() == "best" for f in duration["nmc_flags"]), "claims are flagged for review"

    again = asyncio.run(answers_for(SETTINGS, cache, ITEMS, generate=True, client=client))
    assert len(client.calls) == 1, "cached answers cost nothing"
    assert {k: v["answer"] for k, v in again.items()} == {k: v["answer"] for k, v in first.items()}
    cached_only = asyncio.run(answers_for(SETTINGS, AiAnswers(), ITEMS, generate=False, client=client))
    assert set(cached_only.values()) == {None}


def test_open_answers_endpoint_and_use_as_reply(monkeypatch):
    from fastapi.testclient import TestClient

    import server
    from market_scraper import answer_writer

    async def fake_write(settings, questions, client=None):
        return {q["key"]: {"answer": f"Plain answer to: {q['question']}", "language": "English", "nmc_flags": []}
                for q in questions}

    monkeypatch.setattr(answer_writer, "write_answers", fake_write)
    app = server.create_app(SETTINGS, transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                            allowed_hosts=["testserver"])
    with TestClient(app) as client:
        body = {"items": ITEMS[:1]}
        assert list(client.post("/api/open-answers", json=body).json()["answers"].values()) == [None]
        written = client.post("/api/open-answers", json={**body, "generate": True}).json()["answers"]
        answer = next(iter(written.values()))["answer"]
        assert answer == "Plain answer to: Is IVF painful?"
        assert next(iter(client.post("/api/open-answers", json=body).json()["answers"].values()))["answer"] == answer

        item = client.post("/api/replies", json={**ITEMS[0], "reply": answer}).json()
        assert item["reply"].startswith(answer) and item["reply"].endswith("- Indira IVF team (official account)")
        assert item["disclosure_ok"] is True
