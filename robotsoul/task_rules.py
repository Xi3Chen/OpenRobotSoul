"""Bounded task-local rules. No expression evaluation or executable model text."""

from __future__ import annotations

from math import isfinite
from typing import Any

from robotsoul.models import AgentIntent


class RuleError(ValueError):
    pass


def intent_from(value: Any) -> AgentIntent:
    if isinstance(value, AgentIntent):
        return value
    if not isinstance(value, dict) or not isinstance(value.get("skill"), str) or not isinstance(value.get("parameters"), dict):
        raise RuleError("Rule action must contain a skill and parameters")
    return AgentIntent(value["skill"], dict(value["parameters"]))


def _number(value: Any, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or not minimum <= value <= maximum:
        raise RuleError(f"{label} is outside its allowed range")
    return float(value)


class RuleEngine:
    """The host supplies checked action dispatch, facts, waiting and cancellation."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def validate(self, rule: Any, depth: int = 0) -> None:
        if not isinstance(rule, dict) or depth > 8:
            raise RuleError("Rule must be an object with at most 8 nesting levels")
        name = rule.get("template")
        if name == "sequence":
            steps = rule.get("steps")
            if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
                raise RuleError("Sequence must contain 1 to 32 steps")
            for step in steps:
                self._validate_node(step, depth)
        elif name == "if_result":
            field = rule.get("field")
            if not isinstance(field, str) or not field.startswith("evidence.") or len(field.split(".")) > 4:
                raise RuleError("Conditions may only inspect a bounded evidence field")
            if not all(part.isidentifier() and not part.startswith("_") for part in field.split(".")):
                raise RuleError("Invalid condition field")
            if not isinstance(rule.get("equals"), (str, bool, int, float, type(None))):
                raise RuleError("Condition equality requires a scalar")
            self._validate_node(rule.get("then"), depth)
            if rule.get("else") is not None:
                self._validate_node(rule["else"], depth)
        elif name == "wait_event":
            event_type = rule.get("event_type")
            if not isinstance(event_type, str) or not event_type or len(event_type) > 128:
                raise RuleError("Waiting requires an event type")
            _number(rule.get("timeout_seconds"), 0.001, 300, "Wait timeout")
            equals = rule.get("equals", {})
            if not isinstance(equals, dict) or len(equals) > 8 or any(not isinstance(v, (str, int, float, bool, type(None))) for v in equals.values()):
                raise RuleError("Event filter must contain at most 8 scalar fields")
        elif name == "retry_limited":
            attempts = rule.get("attempts", 1)
            if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 3:
                raise RuleError("Retry attempts must be between 1 and 3")
            intent = intent_from(rule.get("step"))
            self.host.validate_intent(intent)
            if attempts > 1 and not self.host.capabilities[intent.skill].retry_safe:
                raise RuleError("This skill is not registered as safe to retry")
        elif name == "timeline":
            events = rule.get("events")
            if not isinstance(events, list) or not 1 <= len(events) <= 256:
                raise RuleError("Timeline must contain 1 to 256 events")
            previous = -1.0
            for event in events:
                if not isinstance(event, dict):
                    raise RuleError("Timeline event must be an object")
                at = _number(event.get("at_ms"), 0, 300000, "Timeline timestamp")
                if at < previous:
                    raise RuleError("Timeline timestamps must be ordered")
                previous = at
                self.host.validate_intent(intent_from(event))
            cleanup = rule.get("cleanup", [])
            if not isinstance(cleanup, list) or len(cleanup) > 4:
                raise RuleError("Timeline cleanup must contain at most 4 actions")
            for action in cleanup:
                self.host.validate_intent(intent_from(action))
            _number(rule.get("late_tolerance_ms", 50), 0, 1000, "Late tolerance")
        else:
            raise RuleError("Unknown task rule template")

    def _validate_node(self, node: Any, depth: int) -> None:
        if isinstance(node, dict) and "template" in node:
            self.validate(node, depth + 1)
        else:
            self.host.validate_intent(intent_from(node))

    def execute(self, rule: dict[str, Any]) -> bool:
        # Validate the complete tree before the first physical action.
        self.validate(rule)
        reserve = getattr(self.host, "reserve_rule", None)
        if callable(reserve):
            reserve(*self._measure(rule))
        return self._execute(rule)

    def _measure(self, node: dict[str, Any]) -> tuple[int, int]:
        if not isinstance(node, dict) or "template" not in node:
            return 1, 0
        name = node["template"]
        if name == "sequence":
            amounts = [self._measure(step) for step in node["steps"]]
            return sum(item[0] for item in amounts), sum(item[1] for item in amounts)
        if name == "if_result":
            yes = self._measure(node["then"])
            no = self._measure(node["else"]) if node.get("else") is not None else (0, 0)
            return max(yes[0], no[0]), max(yes[1], no[1])
        if name == "retry_limited":
            return node.get("attempts", 1), 0
        if name == "timeline":
            return 0, len(node["events"]) + len(node.get("cleanup", []))
        return 0, 0

    def _node(self, node: dict[str, Any]) -> bool:
        self.host.check_running()
        return self._execute(node) if isinstance(node, dict) and "template" in node else self.host.execute_intent(intent_from(node))

    def _execute(self, rule: dict[str, Any]) -> bool:
        self.host.check_running()
        name = rule["template"]
        if name == "sequence":
            return all(self._node(step) for step in rule["steps"])
        if name == "if_result":
            value: Any = {"evidence": self.host.last_evidence()}
            for part in rule["field"].split("."):
                value = value.get(part) if isinstance(value, dict) else None
            branch = rule.get("then") if value == rule.get("equals") else rule.get("else")
            return True if branch is None else self._node(branch)
        if name == "wait_event":
            return self.host.wait_event(rule)
        if name == "retry_limited":
            failed_commands: list[str] = []
            for _ in range(rule.get("attempts", 1)):
                if self.host.execute_intent(intent_from(rule["step"])):
                    resolve = getattr(self.host, "resolve_retry_failures", None)
                    if callable(resolve):
                        resolve(tuple(failed_commands))
                    return True
                if not self.host.last_failure_retryable():
                    return False
                command_ids = getattr(self.host, "last_step_command_ids", None)
                if callable(command_ids):
                    failed_commands.extend(command_ids())
            return False
        if name == "timeline":
            return self._timeline(rule)
        raise RuleError("Unknown task rule template")

    def _timeline(self, rule: dict[str, Any]) -> bool:
        origin = self.host.now()
        offset = 0.0
        paused_at: float | None = None
        index = 0
        events = rule["events"]
        tolerance = rule.get("late_tolerance_ms", 50) / 1000
        try:
            while index < len(events):
                self.host.check_running()
                for control in self.host.timeline_controls():
                    action = control.get("action")
                    if action == "pause" and paused_at is None:
                        paused_at = self.host.now()
                    elif action == "resume" and paused_at is not None:
                        origin += self.host.now() - paused_at
                        paused_at = None
                    elif action == "seek":
                        position = _number(control.get("position_ms"), 0, 300000, "Seek position") / 1000
                        origin = self.host.now()
                        offset = position
                        paused_at = origin if paused_at is not None else None
                        # Never replay events already emitted, or burst older events.
                        while index < len(events) and events[index]["at_ms"] / 1000 < position:
                            index += 1
                    elif action == "end":
                        index = len(events)
                        self.host.record("timeline.ended", {})
                if index >= len(events):
                    break
                if paused_at is not None:
                    self.host.wait(0.02)
                    continue
                position = self.host.now() - origin + offset
                due = events[index]["at_ms"] / 1000
                if position < due:
                    self.host.wait(min(due - position, 0.02))
                    continue
                event = events[index]
                index += 1
                if position - due > tolerance:
                    self.host.record("timeline.event_skipped", {"at_ms": event["at_ms"], "lateness_ms": (position - due) * 1000})
                    continue
                if not self.host.execute_intent(intent_from(event), timeline=True):
                    return False
            return True
        finally:
            # Cancellation stops through the adapter; the model cannot nominate
            # extra physical actions to dispatch after a task is cancelled.
            for action in rule.get("cleanup", []):
                self.host.check_running()
                self.host.execute_intent(intent_from(action), timeline=True, cleanup=True)
