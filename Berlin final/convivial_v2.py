from flask import Flask, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv
import os
import requests

load_dotenv()  # loads variables from .env
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# URL of the dramatization server
DRAMA_SERVER_URL = "http://localhost:8001"

debug = False

# Replace these with your assistant IDs
PARTICIPANTS = {
    "rain": "asst_V1sykfKJ8hCAfK8q7BQRT2Ji",
    "fungi": "asst_ScfvnTn1EquEItZJBfyHNRfj",
    "bee": "asst_RZOOiEK55firsvvKJ1qtb6nt",
    "fox": "asst_GxLawJ8rWxqvYQtoYBGb06Ep",
    "tree": "asst_nSHz3lLBZQIwm8DT0lnbEI38",
    "human": "",  # human can be proposer (manual text), not a participant in discussion or voting
}

ASSISTANTS = PARTICIPANTS

clerk = "asst_DkQkw8cR4RxcpXHOFvApxnuL"

app = Flask(__name__)

debug = False;
# Global parliament state
STATE = {
    "proposal": "",
    "proposer": "",
    "conversation": "",
    "discussion": {"responses": {}, "done": False},
    "voting": {"votes": {}, "done": False},
    "phase": "discussion",
    "actors_order": [],  # the order of AI characters (non-human) if provided
}

# --- Core helpers ---
def ask_assistant(assistant_id: str, conversation_history: str, name: str) -> str:
    if debug:
        return assistant_id + "test message"
    thread = client.beta.threads.create()
    client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=conversation_history
    )
    client.beta.threads.runs.create_and_poll(
        thread_id=thread.id,
        assistant_id=assistant_id,
    )
    messages = client.beta.threads.messages.list(thread_id=thread.id)
    return messages.data[0].content[0].text.value


def send_to_clerk(conversation: str, votes: dict) -> str:
    """Send full voting table to clerk; return summary."""
    message = "Here is how everyone voted.\n\n=== Votes ===\n"
    for participant, vote in votes.items():
        message += f"{participant}: {vote}\n"

    return ask_assistant(clerk, message, "clerk")


def get_proposal_from_assistant(proposer: str) -> str:
    """Generate proposal if proposer is non-human."""
    asst_id = ASSISTANTS[proposer]
    prompt = (
        'Make a proposal and present it in the Convivial Commons Congress. '
        'Aim for ≤150 words. Write as a continuous short speech, following this order: '
        'CALL-IN (“This is [actor] representing [...]”). PROPOSAL (location + description). '
        'WHY. Steps required. BENEFITS. RISKS / TRADE-OFFS. COST ESTIMATE in ₹ or ecosystem values. '
        'Rules: One concrete idea, no abbreviations, buildable actions, from non-human goals.'
    )
    return ask_assistant(asst_id, prompt, proposer)


# --- Discussion round ---
def run_discussion(participants: list):
    """Run a full discussion round (AI only)."""
    for name in participants:
        if name == "human":
            continue  # human never participates in discussion

        asst_id = ASSISTANTS[name]
        reply = ask_assistant(asst_id, STATE["conversation"], name)
        STATE["discussion"]["responses"][name] = reply
        STATE["conversation"] += f"\n{name} said:\n{reply}\n"

        # Tell the dramatization server as soon as we have this actor's text
        try:
            payload = {"actor": name, "text": reply}
            requests.post(f"{DRAMA_SERVER_URL}/actor_text", json=payload, timeout=5)
        except Exception as e:
            # Don't crash parliament if dramatization server is down
            print(f"[WARN] Failed to send actor_text to dramatization server for {name}: {e}")

    STATE["discussion"]["done"] = True
    STATE["phase"] = "voting"

    return {
        "status": "discussion_complete",
        "phase": "voting",
        "responses": STATE["discussion"]["responses"],
        "votes": STATE["voting"]["votes"],
        "conversation": STATE["conversation"],
    }


# --- Voting round ---
def run_voting(participants: list):
    """Run a full voting round (AI only)."""
    votes = STATE["voting"]["votes"]

    for name in participants:
        if name == "human":
            continue  # human never votes

        asst_id = ASSISTANTS[name]
        vote_prompt = (
            STATE["conversation"]
            + "\nNow cast your vote on the proposal: reply with YES or NO in one short sentence strictly no more than 50 words."
        )
        vote_reply = ask_assistant(asst_id, vote_prompt, name)
        votes[name] = vote_reply
        STATE["conversation"] += f"\n{name} voted:\n{vote_reply}\n"

    STATE["voting"]["done"] = True
    STATE["phase"] = "voting"

    # Send all votes to dramatization server in one go
    try:
        payload = {"votes": votes}
        requests.post(f"{DRAMA_SERVER_URL}/actor_vote", json=payload, timeout=5)
    except Exception as e:
        print(f"[WARN] Failed to send combined votes to dramatization server: {e}")

    # Ask clerk for summary
    clerk_summary = send_to_clerk(STATE["conversation"], votes)

    return {
        "status": "complete",
        "phase": "voting",
        "responses": STATE["discussion"]["responses"],
        "votes": votes,
        "conversation": STATE["conversation"],
        "clerk_summary": clerk_summary,
    }


# --- Orchestrator ---
def start_full_parliament(participants: list):
    """Run discussion → voting with no human input."""
    _ = run_discussion(participants)

    # Voting order = all AIs except proposer + proposer last (if not human)
    voting_participants = [
        n for n in ASSISTANTS
        if n != STATE["proposer"] and n != "human"
    ]
    if STATE["proposer"] != "human":
        voting_participants.append(STATE["proposer"])

    return run_voting(voting_participants)


# --- Routes ---
@app.route("/parliament", methods=["POST"])
def parliament_start():
    """
    Start a parliament run.

    Body:
    {
      "proposal": "...",               # required if proposer is human
      "proposer": "human" | "lake" | ...,
      "order": ["lake", "fungi", ...]  # optional order of AI characters for discussion
    }
    """
    data = request.get_json(force=True)
    proposal = data.get("proposal", "")
    proposer = data.get("proposer", "human")
    order = data.get("order")  # optional list of actor names

    # Human proposer MUST provide proposal
    if proposer == "human" and not proposal:
        return jsonify({"error": "Proposal required when proposer is human"}), 400

    # Non-human proposers must exist in ASSISTANTS
    if proposer != "human" and proposer not in ASSISTANTS:
        return jsonify({"error": f"Invalid proposer '{proposer}'"}), 400

    # Auto-generate proposal for non-human proposers
    if proposer != "human" and not proposal:
        proposal = get_proposal_from_assistant(proposer)

    # Determine actors_order (non-human characters) if provided
    actors_order = []
    if isinstance(order, list) and order:
        # Clean + validate provided order
        for name in order:
            if name in ASSISTANTS and name != "human":
                actors_order.append(name)
    if not actors_order:
        # Fallback default order = all non-human participants in dict order
        actors_order = [n for n in ASSISTANTS.keys() if n != "human"]

    # Reset state
    STATE["proposal"] = proposal
    STATE["proposer"] = proposer
    STATE["conversation"] = f"The proposal is from {proposer}:\n{proposal}\n\n"
    STATE["discussion"] = {"responses": {}, "done": False}
    STATE["voting"] = {"votes": {}, "done": False}
    STATE["phase"] = "discussion"
    STATE["actors_order"] = actors_order  # store for reference / debugging

    # Discussion participants = exactly the actor order provided / derived
    participants = actors_order[:]

    result = start_full_parliament(participants)
    return jsonify({
        "proposal": proposal,
        "proposer": proposer,
        "order": actors_order,  # echo back the order being used
        **result
    })


@app.route("/reset", methods=["POST"])
def reset_state():
    STATE.update({
        "proposal": "",
        "proposer": "",
        "conversation": "",
        "discussion": {"responses": {}, "done": False},
        "voting": {"votes": {}, "done": False},
        "phase": "discussion",
        "actors_order": [],
    })
    return jsonify({"status": "reset_ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
