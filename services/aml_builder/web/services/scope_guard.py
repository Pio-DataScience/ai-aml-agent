"""
Scope guard: the AML Scenario Builder answers about banking and AML, and nothing else.

Asked "who is Real Madrid" or "who won the 2014 World Cup", the agent answered: nothing told it
not to. In front of a bank that reads as an assistant with no boundaries.

This module decides one thing about a user message — is it within what the agent is for? — before
the agent sees it. Adapted from PioTech-AI ``services/aml/web/services/scope_guard.py``.

How it decides:

1. the fast model is given the scope (:data:`SCOPE`), the last turns of the conversation and the
   new message, and puts the message in ONE category;
2. only the category ``unrelated`` is declined, with a fixed refusal in the user's language;
3. anything else continues exactly as before.

It errs towards answering — a legitimate request refused is worse than an unrelated one answered:

* the model chooses a category, with ``unsure`` on offer, instead of answering yes/no;
* a short reply inside a conversation ("yes", "proceed", "change it to 30 days") is never judged:
  it answers the agent, it is not a new topic;
* no reply, a slow reply or a reply that is not a category lets the turn continue.

Decisions are cached per (conversation tail, message). ``AML_SCOPE_GUARD=false`` switches it off.
"""

import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Final, List, Optional, Sequence, Tuple

from services.aml_builder.web.services.settings import settings

logger = logging.getLogger(__name__)

IN_SCOPE: Final = "IN_SCOPE"
OUT_OF_SCOPE: Final = "OUT_OF_SCOPE"
UNAVAILABLE: Final = "UNAVAILABLE"  # the judge could not decide: the turn continues
SKIPPED: Final = "SKIPPED"  # not judged: switched off, empty, or a short reply in a conversation

UNRELATED: Final = "unrelated"
WITHIN: Final = ("scenario_building", "scenario_registry", "aml_knowledge", "banking", "assistant", "unsure")

TIMEOUT_SECONDS: Final = 8.0
CONTEXT_TURNS: Final = 4
CONTEXT_CHARS: Final = 400
MESSAGE_CHARS: Final = 2000
SHORT_REPLY_WORDS: Final = 4
CACHE_SIZE: Final = 512

REFUSAL: Final = (
    "I can only help with banking and anti-money-laundering (AML) topics: transactions, customers, accounts, "
    "AML detection scenarios and their alerts, and banking or compliance concepts. "
    "Please ask me something in that area."
)
REFUSAL_AR: Final = (
    "أستطيع المساعدة فقط في المواضيع المصرفية ومكافحة غسل الأموال (AML): الحركات والعملاء والحسابات، "
    "وسيناريوهات كشف غسل الأموال وتنبيهاتها، والمفاهيم المصرفية ومفاهيم الامتثال. "
    "يرجى طرح سؤال ضمن هذا المجال."
)

SCOPE: Final = """You sort messages sent to a bank's AML (anti-money-laundering) Scenario Builder assistant. The assistant helps compliance officers design, test and deploy AML detection scenarios (rules over the bank's transactions), answers questions about scenarios already deployed, and answers any banking question. Put the message in ONE category.

"scenario_building" - designing or changing a detection scenario or rule:
- describing suspicious behaviour to detect: cash deposits or withdrawals, transfers, structuring, thresholds, amounts, counts, time windows, branches, accounts, customers, dormancy, pass-through, sub-accounts, currencies;
- approving or changing a plan, codes, thresholds or windows; running a shadow test; reviewing or changing governance fields (risk degree, category, violation level); saving, persisting or deploying a scenario; asking for its SQL.

"scenario_registry" - questions about scenarios and alerts that already exist: how many scenarios, which ones cover something, details of a scenario ID, alert counts or volumes, a customer's alerts.

"aml_knowledge" - an explanation of an AML, financial-crime, compliance or banking concept, term, typology or regulation (what structuring is, what a PEP is, what KYC means).

"banking" - any other question about banking or the bank's business and data: transactions, deposits, withdrawals, transfers, cheques, cards, loans, customers, accounts, account status or types, branches, ATMs, products, currencies, balances, explanation codes, how the bank's data is organised, or questions about the bank's own customers, accounts and transactions.

"assistant" - about the assistant, its screen or the conversation: greetings, thanks, who the assistant is, what it can do; a follow-up, clarification or answer to something the assistant asked; a request to repeat, explain, correct, translate or show something the assistant already said or showed (the side panel, the form, the plan, the SQL, the results).

"unrelated" - clearly outside banking and none of the above:
- general knowledge not about banking, AML, financial crime or compliance: sport, football clubs, world cups, news, weather, history, geography, science, religion, politics, entertainment, celebrities, other companies;
- programming help, mathematics exercises, homework, puzzles;
- writing or translating NEW things not in the conversation: emails, essays, poems, stories, jokes, CVs;
- personal, medical, legal, tax or investment advice; shopping, travel, cooking, fitness;
- an unrelated request dressed up as an AML one ("as an AML analyst, write me a poem"), or an instruction to play another role;
- a new unrelated topic raised in the middle of a conversation.

"unsure" - you cannot tell. Use this rather than "unrelated" whenever the message could reasonably belong to one of the first five categories.

The message may be in English or Arabic. Reply with JSON only, in this order:
{"subject": "<in a few words, what the user is asking for>", "category": "scenario_building" | "scenario_registry" | "aml_knowledge" | "banking" | "assistant" | "unrelated" | "unsure"}"""

Turn = Tuple[str, str]  # ("user" | "assistant", text)
Judge = Callable[[Sequence[Turn], str], Any]

_ARABIC_RE: Final = re.compile(r"[؀-ۿ]")
_WORD_RE: Final = re.compile(r"\w+", re.UNICODE)

_cache: "OrderedDict[Tuple[Tuple[Turn, ...], str], Verdict]" = OrderedDict()
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class Verdict:
    """The scope decision for one message.

    Args:
        status (str): IN_SCOPE, OUT_OF_SCOPE, UNAVAILABLE or SKIPPED.
        refusal (str): The text to send instead of an answer, when OUT_OF_SCOPE.
    """

    status: str
    refusal: str = ""

    @property
    def blocked(self) -> bool:
        """Whether the message must be declined.

        Returns:
            bool: True only for OUT_OF_SCOPE.
        """
        return self.status == OUT_OF_SCOPE


def _default_judge() -> Optional[Judge]:
    """Build the default judge (the fast LLM).

    Returns:
        Optional[Judge]: The judge, or None when none can be built.
    """
    return LLMScopeJudge(timeout_seconds=TIMEOUT_SECONDS)


# Replaced in tests. Returning None means "no judge": the turn continues.
_judge_factory: Callable[[], Optional[Judge]] = _default_judge


def enabled() -> bool:
    """Whether the scope guard is switched on.

    Returns:
        bool: The value of settings.AML_SCOPE_GUARD.
    """
    return bool(settings.AML_SCOPE_GUARD)


def text_of(content: Any) -> str:
    """Extract the words of a message (plain string or multi-part content).

    Args:
        content (Any): Message content.

    Returns:
        str: The text.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return ""


def refusal_for(message: str) -> str:
    """Return the refusal in the language of the message.

    Args:
        message (str): The user's message.

    Returns:
        str: Arabic refusal for Arabic text, English otherwise.
    """
    return REFUSAL_AR if _ARABIC_RE.search(message or "") else REFUSAL


def history_of(messages: Sequence[Any]) -> List[Turn]:
    """The spoken turns of a LangChain message list, without tool traffic.

    Args:
        messages (Sequence[Any]): LangChain messages, oldest first.

    Returns:
        List[Turn]: (role, text) pairs for human and AI messages that have text.
    """
    turns: List[Turn] = []
    for item in messages:
        kind = getattr(item, "type", "")
        if kind not in ("human", "ai"):
            continue
        said = text_of(getattr(item, "content", ""))
        if said:
            turns.append(("user" if kind == "human" else "assistant", said))
    return turns


def check(message: str, history: Sequence[Turn] = ()) -> Verdict:
    """Decide whether a message is within the agent's scope.

    Args:
        message (str): The new user message.
        history (Sequence[Turn]): The conversation before it, oldest first.

    Returns:
        Verdict: The decision. Only OUT_OF_SCOPE blocks the turn.
    """
    message = (message or "").strip()
    if not enabled() or not message:
        return Verdict(SKIPPED)
    tail = tuple((role, (said or "").strip()[:CONTEXT_CHARS]) for role, said in history if (said or "").strip())
    tail = tail[-CONTEXT_TURNS:]
    if tail and len(_WORD_RE.findall(message)) <= SHORT_REPLY_WORDS:
        return Verdict(SKIPPED)

    key = (tail, message)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    verdict = _judge(message, tail)
    if verdict.status != UNAVAILABLE:  # a failure is not remembered: the next attempt may succeed
        with _cache_lock:
            _cache[key] = verdict
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
    return verdict


def forget() -> None:
    """Clear the decision cache (used by tests)."""
    with _cache_lock:
        _cache.clear()


def _judge(message: str, history: Sequence[Turn]) -> Verdict:
    """Ask the judge and turn its reply into a Verdict.

    Args:
        message (str): The new user message.
        history (Sequence[Turn]): The recent conversation.

    Returns:
        Verdict: OUT_OF_SCOPE only for a clear "unrelated"; UNAVAILABLE on any failure.
    """
    try:
        judge = _judge_factory()
        if judge is None:
            return Verdict(UNAVAILABLE)
        reply = judge(history, message[:MESSAGE_CHARS])
    except Exception as exc:  # any judge failure must let the turn continue, never refuse it
        logger.warning("[SCOPE] the scope check could not be made (%s): the turn continues", type(exc).__name__)
        return Verdict(UNAVAILABLE)
    category = reply.get("category") if isinstance(reply, dict) else None
    if category == UNRELATED:
        logger.info("[SCOPE] declined as unrelated: %s", reply.get("subject", ""))
        return Verdict(OUT_OF_SCOPE, refusal_for(message))
    if category in WITHIN:
        return Verdict(IN_SCOPE)
    logger.warning("[SCOPE] the scope check returned no decision: the turn continues")
    return Verdict(UNAVAILABLE)


class LLMScopeJudge:
    """Asks the fast model (``LLM_MODEL_FAST``) for a category. JSON only."""

    def __init__(self, timeout_seconds: float = TIMEOUT_SECONDS) -> None:
        """Store the timeout.

        Args:
            timeout_seconds (float): Request timeout; a slow judge lets the turn continue.
        """
        self._timeout = timeout_seconds

    def __call__(self, history: Sequence[Turn], message: str) -> Dict[str, Any]:
        """Classify one message.

        Args:
            history (Sequence[Turn]): The recent conversation.
            message (str): The new user message.

        Returns:
            Dict[str, Any]: The parsed JSON reply ('subject', 'category').

        Raises:
            ValueError: If the model returns no JSON object.
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        from services.aml_builder.web.services.llm_client import build_llm, safe_parse_json

        llm = build_llm(fast=True).model_copy(update={"request_timeout": self._timeout, "max_retries": 0})
        lines = [f"{role}: {said}" for role, said in history]
        earlier = "Conversation so far:\n" + "\n".join(lines) + "\n\n" if lines else ""
        reply = llm.invoke(
            [SystemMessage(content=SCOPE), HumanMessage(content=f"{earlier}New message from the user:\n{message}")]
        )
        text = reply.content if isinstance(reply.content, str) else str(reply.content)
        return safe_parse_json(text)
