import json
import anthropic
from config import settings

client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def classify_and_parse(message: str) -> dict:
    """
    Classify a message and extract structured data.
    Returns a dict with:
      - type: "log" | "dwall" | "query" | "ignore"
      - data: extracted fields (if log or dwall)
      - query: the question string (if query)
    """
    prompt = f"""You are processing WhatsApp messages from construction site engineers.

Classify this message into one of these types and extract relevant data:

1. "log" - A daily site progress update. Contains location, sub-location, activity description, and manpower.
   Example: "Main Location: Zone 3, S2-2\\nSub Location: GL A-B/20\\nDescription: Honeycomb rectification works\\nManpower: Worker - 1"
   Extract: main_location, sub_location, description, manpower

2. "dwall" - A D-Wall or Barrette Pile panel detail entry.
   The first line is typically a date and engineer initials in the format: DD/MM/YY(INITIALS)
   The second line identifies the entry and panel: "Ent-N - Panel No. XXXXX (Group)"
   Then follow labeled fields for panel specs, stage start/end times, volumes, and downtime.
   Stage times are in the format: "HH:MMhrs (DD/MM/YY)"
   Downtime has multiple entries each with a date, start/end time, and reason.
   Use EXACTLY these field names (no variations):
     report_date, engineer_initials, entry_number, panel_number, panel_group,
     panel_size, guide_wall_level, cut_off_level, design_toe_level, design_depth, final_depth, rock_hit,
     excavation_start, excavation_end,
     koden_start, koden_end,
     desanding_start, desanding_end,
     water_stop_start, water_stop_end,
     rebar_cage_start, rebar_cage_end,
     tremie_pipe_start, tremie_pipe_end,
     casting_start, casting_end,
     theo_volume, actual_volume, overbreak_pct,
     downtime, notes

3. "query" - A question about historical data. Examples: "When was Panel 39 cast?", "What happened at CCW1 last Monday?"
   Extract: the full question as "query"

4. "ignore" - General chat, greetings, unrelated messages.

Message:
\"\"\"{message}\"\"\"

Respond ONLY with a JSON object. No explanation. Examples:

For a log:
{{"type": "log", "data": {{"main_location": "Zone 3, S2-2", "sub_location": "GL A-B/20, B2-23", "description": "Honeycomb rectification works in progress", "manpower": "Worker - 1"}}}}

For a dwall entry:
{{"type": "dwall", "data": {{"report_date": "22/02/26", "engineer_initials": "DS", "entry_number": "Ent-2", "panel_number": "CN284A", "panel_group": "G3", "panel_size": "3.0m x 1.2m", "guide_wall_level": "+11.735 mSHD", "cut_off_level": "+9.900 mSHD", "design_toe_level": "-16.000 mSHD", "design_depth": "27.735 m", "final_depth": "28.2m", "rock_hit": "NA", "excavation_start": "13:00hrs (20/02/26)", "excavation_end": "17:45hrs (21/02/26)", "koden_start": "18:00hrs (21/02/26)", "koden_end": "19:00hrs (21/02/26)", "desanding_start": "20:30hrs (21/02/26)", "desanding_end": "22:10hrs (21/02/26)", "water_stop_start": "00:05hrs (22/02/26)", "water_stop_end": "00:55hrs (22/02/26)", "rebar_cage_start": "01:50hrs (22/02/26)", "rebar_cage_end": "05:50hrs (22/02/26)", "tremie_pipe_start": "06:10hrs (22/02/26)", "tremie_pipe_end": "06:35hrs (22/02/26)", "casting_start": "09:30hrs (22/02/26)", "casting_end": "13:00hrs (22/02/26)", "theo_volume": "97.07m3", "actual_volume": "105.00m3", "overbreak_pct": "9.20%", "downtime": [{{"date": "21/02/26", "start": "14:00hrs", "end": "15:30hrs", "reason": "Equipment fault"}}], "notes": ""}}}}

For a query:
{{"type": "query", "query": "When was Panel 39 cast?"}}

For ignore:
{{"type": "ignore"}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)
