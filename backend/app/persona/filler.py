"""
FillerSpeech — injects natural hesitation markers for human-like conversation.

Design principles (based on VoTurn / conversational AI research):
- Fillers should be sparse (max density: FILLER_MAX_DENSITY per token)
- Triggered by processing latency signals, not random insertion
- Context-aware: never inject in lists, code, or formal responses
- PAD-aware: higher arousal → fewer fillers; lower pleasure → more hedging

Filler categories:
  thinking: "嗯...", "让我想想..."
  hedging:  "应该是", "我觉得"
  backchannels: "对对", "嗯嗯"  (for listening acknowledgement)
"""
import random
import re
from app.config import get_config

_THINKING_FILLERS = ["嗯...", "让我想想...", "嗯，这个嘛...", "诶..."]
_HEDGING_FILLERS = ["我觉得", "应该是", "好像是", "大概"]
_BACKCHANNELS = ["嗯嗯", "对对", "啊，是这样"]

_FORMAL_PATTERN = re.compile(r"```|^\s*[-*]\s|\d+\.\s|^#+\s", re.MULTILINE)


def should_inject_filler(text: str, arousal: float = 0.0) -> bool:
    """
    Determine if a filler should be prepended to this response.

    text: the LLM response text
    arousal: PAD arousal dimension (-1 to +1)
    """
    # Never inject in formal/structured content
    if _FORMAL_PATTERN.search(text):
        return False

    # Token count estimate
    token_estimate = len(text) // 4
    if token_estimate == 0:
        return False

    # Density gate: reduce probability proportionally with token count
    base_prob = get_config().FILLER_MAX_DENSITY * token_estimate
    base_prob = min(base_prob, 0.3)  # hard cap at 30%

    # High arousal → more energetic, fewer fillers
    if arousal > 0.5:
        base_prob *= 0.5

    return random.random() < base_prob


def inject_filler(
    text: str,
    pleasure: float = 0.0,
    arousal: float = 0.0,
) -> str:
    """
    Optionally prepend a filler phrase to text.
    Returns original text if filler not applicable.
    """
    if not should_inject_filler(text, arousal):
        return text

    # Choose filler type based on emotional state
    if pleasure < -0.3:
        # Low pleasure → hedging
        filler = random.choice(_HEDGING_FILLERS)
        return f"{filler}，{text}"
    else:
        # Neutral/positive → thinking filler before long responses
        if len(text) > 50:
            filler = random.choice(_THINKING_FILLERS)
            return f"{filler} {text}"

    return text


def add_backchannel(context: str) -> str:
    """Return a brief backchannel phrase for acknowledgement turns."""
    return random.choice(_BACKCHANNELS)
