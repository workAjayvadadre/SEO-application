# Market Scraper (Keyword Studio)

A research and campaign-planning tool for Indira IVF's marketing team. It has two sections:

1. **Research**: what people search for, ask and discuss about a topic, across Google, YouTube, Instagram,
   Facebook, Reddit and Quora, plus Google Trends.
2. **Indira campaigns**: pick a territory (zone, state, district, tehsil/taluka or village), a channel and a
   month. The tool researches the territory on the web, combines that with local search demand, and returns a
   calendar where every slot has a deliverable brief. Your team writes the final content.

Everything that suggests content follows the NMC "Guidelines on Ethical Advertising and Public Communication"
(Public Notice R-13014/01/2024-Ethics, 06-10-2026). The tool flags problems; it is not legal advice or NMC
approval, and every plan needs human compliance sign-off.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
copy .env.example .env            # then fill in the keys you have
python server.py --open           # web UI at http://127.0.0.1:8000
```

Keys in `.env`:

| Key | Needed for |
|-----|------------|
| `SERPER_API_KEY` (or `SERPAPI_API_KEY`) | Google results, Quora, Reddit fallback, Instagram/Facebook discovery, scan location targeting |
| `ANTHROPIC_API_KEY` | Campaign planner, regional web research, deliverable briefs, AI-assistant questions |
| `BRAND_NAME` | Institution name printed on every brief (NMC Cl. 8.3 i) |
| `YOUTUBE_API_KEY` | YouTube videos and comments |
| `REDDIT_CLIENT_ID/SECRET` | Reddit comments (without them Reddit runs on the SERP key, posts only) |
| `META_ACCESS_TOKEN` + IDs | Instagram / Facebook Graph API (otherwise SERP discovery only) |
| `OWN_BRAND_TERMS`, `COMPETITOR_TERMS` | Labelling brand searches as ours or a competitor's |

Corporate networks that inspect HTTPS work out of the box: the app trusts the Windows certificate store
(`truststore`).

## Section 1: Research

| Page | What you get |
|------|--------------|
| **Keyword research** | Google or YouTube autocomplete expanded with 103 modifiers (questions, past tense, prepositions, comparisons, letters before and after), 15 suggestions each. Labels: TOFU / MOFU / BOFU / Long Tail / Location / Local, plus own-brand / competitor. Topic clusters, People Also Ask, optional AI-assistant questions (estimated). Export: CSV or Excel in the same layout as an AnswerSocrates export. |
| **Social listening** | One query across six platforms, with optional location (any place in India) and time range (24 h / week / month / year). Trending topics, sentiment, pain points, **open queries** (questions asked with interest but answered thinly) and a per-platform breakdown. |
| **Trends** | Google Trends for India or one state: interest over time (relative, 100 = peak), rising and top related searches, interest by region. Cached 12 hours; retries politely when Google rate-limits. |

## Section 2: Indira campaigns

| Page | What you get |
|------|--------------|
| **Campaign planner** | Territory picker from the government's Local Government Directory (36 states/UTs, 790 districts, 7,099 tehsils/talukas/mandals; villages typed in). Channels: Instagram, YouTube, Facebook, blog, Google Business Profile, Google Ads. Step 1, regional research: Claude searches the web for that territory and month (festivals with real local impact and their dates, language and code-mixing, season, cultural and health context, sensitivities, sources). Step 2, the calendar: each slot has a date, format, why now, regional hook, NMC class and a **deliverable brief** (objective, key messages, questions to answer, outline, must-include, avoid, visual direction, allowed CTA, evidence, KPIs). Google Ads gets weekly ad groups with keywords, negatives, geo targets and RSA assets, length- and claim-checked. Exports: CSV and Markdown. Regional research is cached 14 days. |
| **Keyword calendar** | The quick, rule-based monthly calendar from keyword research, with state festivals, local-keyword priority and NMC reframing. "Generate brief" on any card gives the same kind of deliverable brief. |
| **Reply queue** | Answer open questions as the official account (ORM). In Social listening > Open queries, open a question's answers (fetched, or pasted by hand when the site blocks reading) and click **Add to reply queue**. Each reply is drafted by Claude or written by hand, then approved by a named reviewer. Approval is refused unless the reply names `BRAND_NAME`, has no NMC claim patterns and no `[PLACEHOLDER]`s, and editing withdraws it. Approved replies are posted through the official API (Reddit thread/comment, YouTube video/comment, your own Facebook Page post or Instagram media) after a confirmation. Quora and Google have no posting API, so you copy those replies, post them by hand and click **Mark as posted**. Limits: one reply per question, at most `REPLY_DAILY_LIMIT` auto-posts per platform per day, no automatic retries (a timeout may mean the reply is live). Every step is logged in `data/replies.json`. Setup: see the "Reply queue" block in `.env.example`, and run `python scripts/youtube_oauth.py` once for YouTube. |

## CLI (social listening only)

```bash
python main.py "standing desk" -p google,reddit,youtube -n 20 -c 15 -o results.json
```

## Layout

```
server.py                   web UI + JSON API
main.py                     CLI
scripts/build_geo.py        rebuilds ui/geo.json from the LGD open-data mirror
market_scraper/
  config.py                 .env -> Settings
  http.py, serp.py          HTTP client (retries, rate limits), Serper / SerpApi
  sources/                  one module per platform
  analysis.py               sentiment, trending topics, questions, open queries, pain points
  keywords.py, export.py    keyword research engine, Excel exports (incl. AnswerSocrates format)
  trends.py                 Google Trends
  geo.py, regional.py       territories, languages, festival list, NMC claim checks
  regional_research.py      Claude + web search regional brief (cached)
  campaign.py               Claude campaign calendar + deliverable briefs
  scriptgen.py              per-card deliverable brief + shared NMC rules
  aiquestions.py            AI-assistant questions (estimated)
  planner.py                rule-based keyword calendar
ui/                         static web UI (no build step)
tests/                      offline tests (mock HTTP and fake Claude clients)
```

## Limits worth knowing

- **Search volume and CPC** aren't available from any source here; popularity is autocomplete rank and
  Trends values are relative. Google Ads Keyword Planner is the source for volumes.
- **Reddit and Quora** may block direct page fetches on some networks; then you get posts and questions from
  search results but not comments or answers.
- **AI-assistant questions** are generated by Claude from typical phrasing, not measured from real assistant
  traffic, and are labelled "estimated" everywhere.
- **Lunar festival dates** shift by region; anything marked "verify" needs checking against the local panchang
  or state holiday list.

## Tests

```bash
python -m pytest -q
```
