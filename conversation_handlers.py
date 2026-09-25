"""Small import-friendly wrapper for the optional multi-turn interface."""

from bot import respond


def handle(state: dict, merchant_message: str) -> dict:
    """Continue a conversation using the state shape used by ``bot.py``."""
    conversation_id = str(state.get("conversation_id") or "conversation")
    merchant = state.get("merchant")
    customer = state.get("customer")
    turn_number = int(state.get("turn_number") or 1)
    return respond(conversation_id, merchant, customer, merchant_message, turn_number)
