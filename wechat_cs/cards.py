"""Build non-leaky decision cards from normalized private-chat messages.

This module deliberately keeps the information available at the decision
boundary separate from the action observed afterwards.  Only
``to_blind_payload`` is intended to feed a model.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union
from zoneinfo import ZoneInfo

from .core import (
    DEFAULT_HMAC_SECRET,
    Message,
    hmac_id,
    parse_timestamp,
    redact_text,
    stable_split,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
CARD_RULE_VERSION = "decision-card-v1"
INBOUND = "inbound"
PROACTIVE_FOLLOWUP = "proactive_followup"
OBSERVED_ACTION_STATES = frozenset(
    {
        "immediate_reply",
        "delayed_reply",
        "no_reply",
        "proactive_followup",
        "unobserved",
    }
)

TimestampValue = Union[str, datetime]


@dataclass(frozen=True)
class CardSource:
    """Profile-scoped source facts required to judge action observability."""

    profile_id: str
    source_snapshot_id: str
    observation_until: Optional[TimestampValue]


@dataclass(frozen=True)
class DecisionTurn:
    """A same-role turn with exact source-order boundaries retained."""

    customer_key: str
    role: str
    started_at: str
    ended_at: str
    text: str
    message_keys: Tuple[str, ...]
    messages: Tuple[Message, ...]
    start_ordinal: int
    end_ordinal: int
    start_message_key: str
    end_message_key: str

    @property
    def start(self) -> datetime:
        return _moment(self.started_at)

    @property
    def end(self) -> datetime:
        return _moment(self.ended_at)

    @property
    def start_order(self) -> Tuple[datetime, int, str]:
        return (self.start, self.start_ordinal, self.start_message_key)

    @property
    def end_order(self) -> Tuple[datetime, int, str]:
        return (self.end, self.end_ordinal, self.end_message_key)


@dataclass(frozen=True)
class DecisionCard:
    card_id: str
    customer_key: str
    episode_id: str
    card_type: str
    as_of_at: str
    boundary_ordinal: int
    boundary_message_key: str
    source_snapshot_id: str
    action_window_end: str
    observation_until: Optional[str]
    blind_context: List[Dict[str, object]]
    observed_action: Dict[str, object]
    context_message_keys: Tuple[str, ...]
    action_message_keys: Tuple[str, ...]
    split: str
    rule_version: str


def _moment(value: TimestampValue) -> datetime:
    parsed = value if isinstance(value, datetime) else parse_timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(timezone.utc)


def _shanghai_iso(value: TimestampValue) -> str:
    return _moment(value).astimezone(SHANGHAI).isoformat(timespec="seconds")


def _message_order(message: Message) -> Tuple[datetime, int, str]:
    return (_moment(message.timestamp), int(message.source_ordinal), message.message_key)


def _turn_from_messages(messages: Sequence[Message]) -> DecisionTurn:
    ordered = tuple(sorted(messages, key=_message_order))
    first = ordered[0]
    last = ordered[-1]
    return DecisionTurn(
        customer_key=first.customer_key,
        role=first.role,
        started_at=first.timestamp,
        ended_at=last.timestamp,
        text="\n".join(item.text for item in ordered),
        message_keys=tuple(item.message_key for item in ordered),
        messages=ordered,
        start_ordinal=int(first.source_ordinal),
        end_ordinal=int(last.source_ordinal),
        start_message_key=first.message_key,
        end_message_key=last.message_key,
    )


def build_decision_turns(
    messages: Sequence[Message], *, window_minutes: int = 15
) -> List[DecisionTurn]:
    """Merge consecutive, same-role messages using an inclusive 15-minute window.

    Sorting always uses ``(timestamp, source_ordinal, message_key)``.  The final
    key matters when two source files assign the same ordinal in the same
    second.
    """

    if window_minutes < 0:
        raise ValueError("turn window must be non-negative")
    by_customer: Dict[str, List[Message]] = defaultdict(list)
    for item in messages:
        if item.role not in {"customer", "studio"}:
            raise ValueError("message role must be customer or studio")
        if not item.customer_key or not item.message_key:
            raise ValueError("message and customer keys are required")
        by_customer[item.customer_key].append(item)

    turns: List[DecisionTurn] = []
    window_seconds = window_minutes * 60
    for customer_key in sorted(by_customer):
        ordered = sorted(by_customer[customer_key], key=_message_order)
        current: List[Message] = []
        for item in ordered:
            if current:
                gap = (_moment(item.timestamp) - _moment(current[-1].timestamp)).total_seconds()
                if item.role != current[-1].role or gap < 0 or gap > window_seconds:
                    turns.append(_turn_from_messages(current))
                    current = []
            current.append(item)
        if current:
            turns.append(_turn_from_messages(current))
    return sorted(turns, key=lambda item: (item.start_order, item.customer_key))


def segment_episodes(
    turns: Sequence[DecisionTurn], *, gap_hours: int = 24
) -> List[List[DecisionTurn]]:
    """Split turns when the gap from the prior turn is strictly over 24 hours."""

    if gap_hours <= 0:
        raise ValueError("episode gap must be positive")
    if not turns:
        return []
    ordered = sorted(turns, key=lambda item: (item.customer_key, item.start_order))
    episodes: List[List[DecisionTurn]] = []
    gap_seconds = gap_hours * 3600
    for turn in ordered:
        if not episodes:
            episodes.append([turn])
            continue
        previous = episodes[-1][-1]
        gap = (turn.start - previous.end).total_seconds()
        if turn.customer_key != previous.customer_key or gap > gap_seconds:
            episodes.append([turn])
        else:
            episodes[-1].append(turn)
    return episodes


def _context(turns: Sequence[DecisionTurn]) -> List[Dict[str, object]]:
    output: List[Dict[str, object]] = []
    for turn in turns:
        text, _ = redact_text(turn.text)
        output.append(
            {
                "role": turn.role,
                "text": text,
                "started_at": turn.started_at,
                "ended_at": turn.ended_at,
            }
        )
    return output


def _message_keys(turns: Sequence[DecisionTurn]) -> Tuple[str, ...]:
    return tuple(key for turn in turns for key in turn.message_keys)


def _episode_id(secret: str, episode: Sequence[DecisionTurn]) -> str:
    first = episode[0]
    return hmac_id(
        secret,
        "episode",
        CARD_RULE_VERSION,
        first.customer_key,
        first.message_keys[0],
    )


def _card_id(
    secret: str,
    customer_key: str,
    card_type: str,
    trigger_message_keys: Sequence[str],
) -> str:
    return hmac_id(
        secret,
        "card",
        CARD_RULE_VERSION,
        customer_key,
        card_type,
        *trigger_message_keys,
    )


def _empty_action(state: str) -> Dict[str, object]:
    return {
        "state": state,
        "reply_delay_seconds": None,
        "message_keys": [],
        "text": None,
    }


def _reply_action(
    turns: Sequence[DecisionTurn],
    *,
    boundary: Tuple[datetime, int, str],
    as_of: datetime,
    window_end: datetime,
    observation_until: Optional[datetime],
    immediate_reply_seconds: int,
) -> Tuple[Dict[str, object], Tuple[str, ...]]:
    if observation_until is None:
        return _empty_action("unobserved"), ()

    upper_bound = min(window_end, observation_until)
    for turn in turns:
        if turn.role != "studio" or turn.end_order <= boundary:
            continue
        visible = tuple(
            item
            for item in turn.messages
            if _message_order(item) > boundary and _moment(item.timestamp) <= upper_bound
        )
        if not visible:
            if turn.start > upper_bound:
                break
            continue
        first_reply = _moment(visible[0].timestamp)
        delay = int((first_reply - as_of).total_seconds())
        if delay < 0 or delay > int((window_end - as_of).total_seconds()):
            continue
        state = "immediate_reply" if delay <= immediate_reply_seconds else "delayed_reply"
        text, _ = redact_text("\n".join(item.text for item in visible))
        keys = tuple(item.message_key for item in visible)
        return (
            {
                "state": state,
                "reply_delay_seconds": delay,
                "message_keys": list(keys),
                "text": text,
            },
            keys,
        )

    state = "no_reply" if observation_until >= window_end else "unobserved"
    return _empty_action(state), ()


def _source_for(
    sources_by_customer: Mapping[str, CardSource], customer_key: str
) -> CardSource:
    try:
        source = sources_by_customer[customer_key]
    except KeyError as exc:
        raise ValueError("card source is missing for customer %s" % customer_key) from exc
    if not source.profile_id or not source.source_snapshot_id:
        raise ValueError("card source profile and snapshot IDs are required")
    return source


def build_decision_cards(
    messages: Sequence[Message],
    sources_by_customer: Mapping[str, CardSource],
    *,
    secret: str = DEFAULT_HMAC_SECRET,
    turn_window_minutes: int = 15,
    episode_gap_hours: int = 24,
    action_window_hours: int = 24,
    immediate_reply_minutes: int = 30,
    context_turn_limit: int = 8,
) -> List[DecisionCard]:
    """Return stable inbound and proactive-followup cards.

    Future messages may update ``observed_action`` but never participate in an
    inbound card ID or blind context.  A missing profile observation boundary
    always yields ``unobserved`` rather than falling back to another profile's
    latest timestamp.
    """

    if not secret:
        raise ValueError("HMAC secret must not be empty")
    if action_window_hours <= 0 or immediate_reply_minutes < 0 or context_turn_limit <= 0:
        raise ValueError("card windows and context limit are invalid")

    all_turns = build_decision_turns(messages, window_minutes=turn_window_minutes)
    turns_by_customer: Dict[str, List[DecisionTurn]] = defaultdict(list)
    for turn in all_turns:
        turns_by_customer[turn.customer_key].append(turn)

    cards: List[DecisionCard] = []
    action_delta = timedelta(hours=action_window_hours)
    immediate_seconds = immediate_reply_minutes * 60
    for customer_key in sorted(turns_by_customer):
        customer_turns = sorted(turns_by_customer[customer_key], key=lambda item: item.start_order)
        episodes = segment_episodes(customer_turns, gap_hours=episode_gap_hours)
        source = _source_for(sources_by_customer, customer_key)
        observation = (
            _moment(source.observation_until) if source.observation_until is not None else None
        )
        observation_iso = (
            _shanghai_iso(source.observation_until)
            if source.observation_until is not None
            else None
        )

        for episode_index, episode in enumerate(episodes):
            episode_id = _episode_id(secret, episode)
            for turn_index, turn in enumerate(episode):
                if turn.role != "customer":
                    continue
                as_of = turn.end
                window_end = as_of + action_delta
                boundary = turn.end_order
                context_turns = episode[: turn_index + 1][-context_turn_limit:]
                action, action_keys = _reply_action(
                    customer_turns,
                    boundary=boundary,
                    as_of=as_of,
                    window_end=window_end,
                    observation_until=observation,
                    immediate_reply_seconds=immediate_seconds,
                )
                cards.append(
                    DecisionCard(
                        card_id=_card_id(secret, customer_key, INBOUND, turn.message_keys),
                        customer_key=customer_key,
                        episode_id=episode_id,
                        card_type=INBOUND,
                        as_of_at=turn.ended_at,
                        boundary_ordinal=turn.end_ordinal,
                        boundary_message_key=turn.end_message_key,
                        source_snapshot_id=source.source_snapshot_id,
                        action_window_end=_shanghai_iso(window_end),
                        observation_until=observation_iso,
                        blind_context=_context(context_turns),
                        observed_action=action,
                        context_message_keys=_message_keys(context_turns),
                        action_message_keys=action_keys,
                        split=stable_split(secret, customer_key),
                        rule_version=CARD_RULE_VERSION,
                    )
                )

            if episode_index == 0 or episode[0].role != "studio":
                continue
            action_turn = episode[0]
            previous_episode = episodes[episode_index - 1]
            context_turns = previous_episode[-context_turn_limit:]
            as_of = action_turn.start
            gap_seconds = int((action_turn.start - previous_episode[-1].end).total_seconds())
            visible_action_messages = (
                tuple(
                    item
                    for item in action_turn.messages
                    if _moment(item.timestamp) <= observation
                )
                if observation is not None
                else ()
            )
            if visible_action_messages:
                action_keys = tuple(item.message_key for item in visible_action_messages)
                action_text, _ = redact_text(
                    "\n".join(item.text for item in visible_action_messages)
                )
                action = {
                    "state": PROACTIVE_FOLLOWUP,
                    "reply_delay_seconds": None,
                    "gap_seconds": gap_seconds,
                    "message_keys": list(action_keys),
                    "text": action_text,
                }
            else:
                action_keys = ()
                action = _empty_action("unobserved")
            cards.append(
                DecisionCard(
                    card_id=_card_id(
                        secret,
                        customer_key,
                        PROACTIVE_FOLLOWUP,
                        (action_turn.message_keys[0],),
                    ),
                    customer_key=customer_key,
                    episode_id=episode_id,
                    card_type=PROACTIVE_FOLLOWUP,
                    as_of_at=action_turn.started_at,
                    boundary_ordinal=action_turn.start_ordinal,
                    boundary_message_key=action_turn.start_message_key,
                    source_snapshot_id=source.source_snapshot_id,
                    action_window_end=_shanghai_iso(as_of + action_delta),
                    observation_until=observation_iso,
                    blind_context=_context(context_turns),
                    observed_action=action,
                    context_message_keys=_message_keys(context_turns),
                    action_message_keys=action_keys,
                    split=stable_split(secret, customer_key),
                    rule_version=CARD_RULE_VERSION,
                )
            )

    unique = {item.card_id: item for item in cards}
    return sorted(
        unique.values(),
        key=lambda item: (
            _moment(item.as_of_at),
            item.boundary_ordinal,
            item.boundary_message_key,
            item.customer_key,
            item.card_type,
        ),
    )


def to_blind_payload(card: DecisionCard) -> Dict[str, object]:
    """Return the only fields allowed in model-facing decision analysis."""

    return {
        "card_id": card.card_id,
        "card_type": card.card_type,
        "as_of_at": card.as_of_at,
        "context": [dict(item) for item in card.blind_context],
    }
