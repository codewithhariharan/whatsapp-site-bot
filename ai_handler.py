import json
from ai_client import get_client, model_id
from config import settings
import database as db

# /ask stuffs the whole history into one prompt. That was fine at a few hundred
# rows and is not fine at ~38k: Vertex rejects request payloads over 30 MB, and
# well before that the cost and latency per question become absurd. Cap it at
# the most recent slice until this is replaced with a real retrieval step.
MAX_LOGS_IN_PROMPT = 1500


def answer_query(group_id: str, question: str) -> str:
    """Search the database and answer a natural language question."""

    all_logs = db.get_all_logs(group_id)
    truncated = len(all_logs) > MAX_LOGS_IN_PROMPT
    if truncated:
        all_logs = all_logs[-MAX_LOGS_IN_PROMPT:]
    all_panels = db.get_all_panels(group_id)

    context = f"""You are a construction site assistant. Answer the question using only the data below.
Be concise and direct. If the answer is not in the data, say so clearly.

DAILY SITE LOGS:
{json.dumps(all_logs, indent=2, default=str)}

D-WALL / BARRETTE PANEL RECORDS:
{json.dumps(all_panels, indent=2, default=str)}

Question: {question}"""

    response = get_client().messages.create(
        model=model_id(settings.ANSWER_MODEL),
        max_tokens=500,
        messages=[{"role": "user", "content": context}],
    )

    answer = response.content[0].text.strip()
    if truncated:
        answer += (
            f"\n\n_(Answered from the most recent {MAX_LOGS_IN_PROMPT} entries only.)_"
        )
    return answer
