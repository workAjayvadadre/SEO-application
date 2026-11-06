"""Excel export of a content plan: one calendar sheet, one sheet per channel, plus keywords and clusters."""

from __future__ import annotations

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

_HEADER_FILL = PatternFill("solid", fgColor="1F2A44")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
CHANNEL_FILLS = {"blog": "E7F0FE", "youtube": "FDE8EA", "instagram": "F8E8F3", "facebook": "E6F0FF", "gbp": "E8F5EC"}

PLAN_COLUMNS = [("date", "Date", 12), ("weekday", "Day", 6), ("channel_label", "Channel", 22), ("format", "Format", 12),
                ("title", "Suggested title", 58), ("keyword", "Target keyword", 38), ("topic", "Topic cluster", 18),
                ("intent", "Intent", 14), ("funnel", "Funnel stage", 14), ("popularity", "Popularity (1–2)", 14),
                ("hashtags", "Hashtags", 48), ("status", "Status", 12)]
KEYWORD_COLUMNS = [("keyword", "Keyword", 48), ("group", "Group", 14), ("modifier", "Modifier", 18),
                   ("position", "Autocomplete rank", 18), ("intent", "Intent", 14), ("funnel", "Funnel stage", 14),
                   ("label", "Keyword label", 12), ("source", "Source", 10)]
# Column layout of an AnswerSocrates "Keywords" export, so files can be merged or compared.
AS_COLUMNS = [("type", "Type", 14), ("modifier", "Modifier", 16), ("question", "Question", 60),
              ("label", "Keyword Label", 14), ("volume", "Search Volume", 14), ("cpc", "CPC", 8)]
_AS_TYPES = {"alphabetical": "letters", "past": "in the past"}


def _as_modifier(group: str, modifier: str) -> str:
    if group == "alphabetical" or not modifier:
        return modifier
    return modifier[:1].upper() + modifier[1:]


def answersocrates_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows in AnswerSocrates' order: Google groups first, then YouTube, then People Also Ask."""
    rows: list[dict[str, Any]] = []
    paa: list[dict[str, Any]] = []
    for result in sorted(results, key=lambda r: r.get("source") == "youtube"):
        youtube = result.get("source") == "youtube"
        for k in result.get("keywords", []):
            group = k.get("group", "")
            rows.append({"type": "youtube" if youtube else _AS_TYPES.get(group, group),
                         "modifier": _as_modifier(group, k.get("modifier", "")), "question": k.get("keyword"),
                         "label": k.get("label") or "TOFU", "volume": None, "cpc": None})
        paa += [{"type": "paa", "modifier": "paa", "question": q.get("question"), "label": "TOFU",
                 "volume": None, "cpc": None} for q in result.get("people_also_ask", [])]
        # AI-assistant questions are estimated by Claude, never measured: the Type says so.
        paa += [{"type": "ai assistants (estimated)", "modifier": q.get("stage", ""), "question": q.get("question"),
                 "label": q.get("label") or "TOFU", "volume": None, "cpc": None}
                for q in (result.get("ai_questions") or {}).get("questions", [])]
    return rows + paa


def keywords_to_answersocrates_xlsx(results: list[dict[str, Any]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    _sheet(wb, "Keywords", AS_COLUMNS, answersocrates_rows(results))
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def _safe(value: Any) -> Any:
    """Plain text only: a keyword like '=cmd|...' must never become a live formula."""
    if isinstance(value, list):
        value = " ".join(map(str, value))
    return value


def _sheet(wb: Workbook, title: str, columns: list[tuple[str, str, int]], rows: list[dict[str, Any]],
           fill_by_channel: bool = False):
    ws = wb.create_sheet(title[:31])
    ws.append([header for _, header, _ in columns])
    for i, (_, _, width) in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
        cell = ws.cell(row=1, column=i)
        cell.fill, cell.font = _HEADER_FILL, _HEADER_FONT
    for row in rows:
        ws.append([None] * len(columns))
        r = ws.max_row
        for c, (key, _, _) in enumerate(columns, start=1):
            cell = ws.cell(row=r, column=c)
            cell.value = _safe(row.get(key))
            if isinstance(cell.value, str):
                cell.data_type = "s"  # force text even if it starts with '='
            cell.alignment = Alignment(vertical="top", wrap_text=key in {"title", "hashtags"})
        if fill_by_channel and row.get("channel") in CHANNEL_FILLS:
            for c in range(1, len(columns) + 1):
                ws.cell(row=r, column=c).fill = PatternFill("solid", fgColor=CHANNEL_FILLS[row["channel"]])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def plan_to_xlsx(plan: dict[str, Any], keywords: list[dict[str, Any]], clusters: list[dict[str, Any]]) -> bytes:
    from datetime import date

    items = []
    for item in plan.get("items", []):
        row = dict(item)
        try:
            row["weekday"] = date.fromisoformat(str(item.get("date"))).strftime("%a")
        except ValueError:
            row["weekday"] = ""
        row.setdefault("status", "Planned")
        items.append(row)

    wb = Workbook()
    wb.remove(wb.active)
    _sheet(wb, "Calendar", PLAN_COLUMNS, items, fill_by_channel=True)
    for channel, label in (plan.get("channels") or {}).items():
        rows = [i for i in items if i.get("channel") == channel]
        if rows:
            _sheet(wb, label, PLAN_COLUMNS, rows)
    _sheet(wb, "Keywords", KEYWORD_COLUMNS, keywords)
    _sheet(wb, "Topic clusters", [("topic", "Topic", 22), ("size", "Keywords", 10), ("keywords", "All keywords", 120)],
           [{**c, "keywords": ", ".join(c.get("keywords", []))} for c in clusters])

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
