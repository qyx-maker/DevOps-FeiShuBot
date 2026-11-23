"""Per-message tracing and short-lived diagnostic context."""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic

LOGGER = logging.getLogger("dafeiyu")
INCIDENT_TTL_SECONDS = 30 * 60
INCIDENT_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,252}")


def _label(value):
    return value if isinstance(value, str) and INCIDENT_LABEL.fullmatch(value) else None


def tool_payload(result: str) -> dict:
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class Incident:
    captured_at: float
    observed_at_utc: str
    scope: dict
    observations: tuple[str, ...]
    missing: tuple[str, ...]
    window: tuple[str, str] | None = None

    def as_context(self) -> str:
        return json.dumps({
            "observed_at_utc": self.observed_at_utc, "scope": self.scope,
            "observations": self.observations, "missing": self.missing, "window_utc": self.window,
        }, ensure_ascii=False)


def _cluster_incident(payload: dict, previous: Incident | None) -> Incident | None:
    if payload.get("status") not in ("OK", "PARTIAL", "NO_DATA"):
        return previous
    scope = {
        field: label for field in ("context", "namespace", "service", "host", "pod")
        if (label := _label(payload.get(field)))
    }
    if not scope.get("context"):
        return previous
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    observations = []
    for resource, details in evidence.items():
        if not _label(resource) or not isinstance(details, dict):
            continue
        items = details.get("items", [])
        if isinstance(items, list):
            names = [_label(item.get("name")) for item in items[:3] if isinstance(item, dict)]
            names = [name for name in names if name]
            if names:
                observations.append(f"{resource} 已查到：{', '.join(names)}")
        if resource == "pods":
            lines = details.get("lines")
            if isinstance(lines, list):
                for line in lines[1:4]:
                    if isinstance(line, str):
                        observations.append(f"Pod 状态：{line[:150]}")
        if type(details.get("attention_count")) is int:
            observations.append(f"{resource} 异常候选数：{details['attention_count']}")
    if _label(payload.get("container")) and payload.get("pod"):
        observations.append(f"已查询容器 {payload['container']} 的日志，状态 {payload['status']}")
    if not observations and evidence:
        observations.append("已读取资源：" + ", ".join(key for key in evidence if _label(key))[:220])
    unavailable = payload.get("unavailable_resources")
    missing = tuple(key for key in unavailable if _label(key)) if isinstance(unavailable, list) else ()
    same_scope = previous and scope == previous.scope
    if not observations and not missing:
        return previous
    return Incident(
        monotonic(), datetime.now(UTC).isoformat(timespec="seconds"), scope,
        tuple(observations[:6]), missing[:8], previous.window if same_scope else None,
    )


@dataclass
class TurnTrace:
    message_id: str
    started_at: float = field(default_factory=monotonic)
    incident: Incident | None = None

    def event(self, event: str, **fields) -> None:
        LOGGER.info(json.dumps({"event": event, "message_id": self.message_id, **fields}, ensure_ascii=False))

    def tool_result(self, name: str, result: str, started_at: float) -> None:
        payload = tool_payload(result)
        status = payload.get("status")
        fields = {
            "tool": name,
            "status": status if isinstance(status, str) and re.fullmatch("[A-Za-z_]{1,32}", status) else "unclassified",
            "duration_ms": round((monotonic() - started_at) * 1000),
        }
        if type(payload.get("http_status")) is int:
            fields["http_status"] = payload["http_status"]
        if isinstance(payload.get("results"), list):
            fields["result_count"] = len(payload["results"])
        missing = payload.get("unavailable_resources")
        if isinstance(missing, list):
            fields["unavailable_resources"] = [item for item in missing if _label(item)][:8]
        self.event("tool_result", **fields)
        if name == "ask_holmes":
            self.incident = _cluster_incident(payload, self.incident)
        if name == "query_prometheus_metric" and self.incident and status == "OK":
            scope = self.incident.scope
            series = payload.get("series", [])
            if scope.get("namespace") and scope.get("service") and isinstance(series, list) and series and all(
                isinstance(row, dict) and isinstance(row.get("labels"), dict)
                and row["labels"].get("namespace") == scope["namespace"]
                and row["labels"].get("service") == scope["service"]
                for row in series
            ):
                window = (payload.get("start_utc"), payload.get("end_utc"))
                if all(isinstance(value, str) and 0 < len(value) <= 40 for value in window):
                    incident = self.incident
                    self.incident = Incident(
                        incident.captured_at, incident.observed_at_utc, incident.scope,
                        incident.observations, incident.missing, window,
                    )
