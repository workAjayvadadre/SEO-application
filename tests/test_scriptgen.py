"""Deliverable brief: request building and the post-generation NMC check, with a fake Claude client."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from market_scraper import Settings
from market_scraper.scriptgen import ScriptGenError, build_request, generate_content

ITEM = {"date": "2026-12-08", "channel": "instagram", "channel_label": "Instagram", "format": "Reel",
        "keyword": "ivf success rate in kolkata", "title": "What Affects IVF Outcomes?", "intent": "informational",
        "funnel": "awareness", "hashtags": ["#ivf"]}


def content(caption: str, verdict: str = "GREEN") -> dict:
    """A brief whose caption direction is `caption`; its avoid list names banned words on purpose."""
    return {"content_class": "A", "title": "t", "objective": "o", "audience": "a", "regional_angle": "Kolkata",
            "language_plan": "Bengali", "hook_options": [{"english": "h", "regional": "", "back_translation": ""}],
            "key_messages": [], "questions_to_answer": [], "outline": [], "must_include": [],
            "avoid": ["'best', '100% success', testimonials"],
            "visual_direction": {"canvas": "1080x1920", "look_and_feel": "warm", "motifs": [],
                                 "exclusions": ["no pink/blue gender cues"], "ai_mark": "AI-generated image",
                                 "alt_text": "x"},
            "caption_direction": caption, "hashtags": ["#FertilityAwareness"], "allowed_cta": "none",
            "sources_to_cite": [], "kpis": [],
            "compliance": {"verdict": verdict, "notes": "", "checklist": [], "human_approval": []}}


class FakeClient:
    def __init__(self, payload: dict, stop_reason: str = "end_turn"):
        self.calls = []

        async def create(**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(stop_reason=stop_reason, model="claude-opus-5-5",
                                   content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=20))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_request_carries_region_language_festivals_and_keyword_flags():
    prompt = build_request(ITEM, month="2026-12", region="Salt Lake Kolkata", country="in", brand=None)
    brief = json.loads(prompt.split("\n\n", 1)[1])
    assert brief["location"]["state"] == "West Bengal" and brief["regional_language"] == "Bengali"
    assert any("Christmas" in e for e in brief["festivals_and_days_this_month"])
    assert brief["keyword_claim_flags"] and brief["institution_name"] == "[INSTITUTION NAME]"


def test_post_check_downgrades_green_when_copy_contains_a_claim():
    client = FakeClient(content("Kolkata's best IVF care. AI-assisted content"))
    result = asyncio.run(generate_content(Settings(), ITEM, month="2026-12", region="Kolkata", client=client))
    assert result["auto_check"] and result["content"]["compliance"]["verdict"] == "AMBER"
    call = client.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema" and call["fallbacks"] == "default"


def test_clean_copy_stays_green_and_errors_are_user_facing():
    ok = asyncio.run(generate_content(Settings(), ITEM, month="2026-12", region="Kolkata",
                                      client=FakeClient(content("How IVF works, step by step. AI-assisted content"))))
    assert ok["auto_check"] == [] and ok["content"]["compliance"]["verdict"] == "GREEN"
    with pytest.raises(ScriptGenError, match="ANTHROPIC_API_KEY"):
        asyncio.run(generate_content(replace(Settings(), anthropic_api_key=None), ITEM, month=None, region=None))
    with pytest.raises(ScriptGenError, match="declined"):
        asyncio.run(generate_content(Settings(), ITEM, month=None, region=None,
                                     client=FakeClient(content("x"), stop_reason="refusal")))


def test_ai_assistant_questions_are_labelled_estimated_and_exported():
    from market_scraper.aiquestions import ai_assistant_questions
    from market_scraper.export import answersocrates_rows

    payload = {"questions": [
        {"question": "I'm 38, is IVF still worth trying?", "stage": "exploring", "label": "TOFU", "why_people_ask": "age"},
        {"question": "Which is the best IVF centre in Kolkata?", "stage": "deciding", "label": "Local", "why_people_ask": "choice"},
        {"question": "I'm 38, is IVF still worth trying?", "stage": "exploring", "label": "TOFU", "why_people_ask": "dup"},
    ]}
    out = asyncio.run(ai_assistant_questions(Settings(), "ivf", "in", "Kolkata", client=FakeClient(payload)))
    assert len(out["questions"]) == 2 and all(q["estimated"] for q in out["questions"])
    assert out["questions"][1]["nmc_note"]  # "best" flagged
    rows = answersocrates_rows([{"source": "google", "keywords": [], "ai_questions": out}])
    assert {r["type"] for r in rows} == {"ai assistants (estimated)"}
