import json
import anthropic
from config import settings
import database as db

client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def answer_query(group_id: str, question: str) -> str:
    """Search the database and answer a natural language question."""

    all_logs = db.get_all_logs(group_id)
    all_panels = db.get_all_panels(group_id)

    context = f"""You are a construction site assistant. Answer the question using only the data below.
Be concise and direct. If the answer is not in the data, say so clearly.

DAILY SITE LOGS:
{json.dumps(all_logs, indent=2, default=str)}

D-WALL / BARRETTE PANEL RECORDS:
{json.dumps(all_panels, indent=2, default=str)}

Question: {question}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": context}],
    )

    return response.content[0].text.strip()
