"""Validation and execution of the deliberately small, safe workflow node catalogue."""

from datetime import datetime

from sensor_network_pwa.mailer import send_email
from sensor_network_pwa.storage import load_station_rows, to_float
from sensor_network_pwa.timeutil import utc_iso, utc_now
from sensor_network_pwa.validation import is_valid_email

NODE_TYPES = frozenset({"station", "condition", "email"})
OPERATORS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def validate_workflow_graph(graph, station_allowed):
    if (
        not isinstance(graph, dict)
        or not isinstance(graph.get("nodes"), list)
        or not isinstance(graph.get("edges"), list)
    ):
        return False, "graph must contain nodes and edges arrays"
    if not 1 <= len(graph["nodes"]) <= 50 or len(graph["edges"]) > 100:
        return False, "a workflow needs 1-50 nodes and at most 100 edges"
    ids = set()
    types = {}
    for node in graph["nodes"]:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"].strip():
            return False, "every node needs an id"
        node_id, node_type = node["id"].strip(), node.get("type")
        if node_id in ids or node_type not in NODE_TYPES:
            return False, "node ids must be unique and node types must be station, condition or email"
        ids.add(node_id)
        types[node_id] = node_type
        data = node.get("data") or {}
        if node_type == "station" and (
            not isinstance(data.get("station"), str) or not station_allowed(data["station"])
        ):
            return False, "a station node is not readable by this user"
        if node_type == "condition":
            if (
                not isinstance(data.get("field"), str)
                or not data["field"].strip()
                or data.get("operator") not in OPERATORS
            ):
                return False, "a condition needs a field and valid operator"
            value = data.get("value")
            try:
                float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return False, "a condition value must be numeric"
        if node_type == "email" and not is_valid_email(str(data.get("recipient") or "")):
            return False, "an email node needs a valid recipient"
    for edge in graph["edges"]:
        if not isinstance(edge, dict) or edge.get("source") not in ids or edge.get("target") not in ids:
            return False, "every edge must connect existing nodes"
    # The first catalogue is intentionally strict: a station feeds a condition, which feeds email actions.
    incoming: dict[str, list[str]] = {node_id: [] for node_id in ids}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in graph["edges"]:
        incoming[edge["target"]].append(edge["source"])
        outgoing[edge["source"]].append(edge["target"])
    for node_id, node_type in types.items():
        if node_type == "station" and incoming[node_id]:
            return False, "station nodes cannot have inputs"
        if node_type == "condition" and (
            not incoming[node_id] or any(types[source] != "station" for source in incoming[node_id])
        ):
            return False, "each condition must have at least one station input"
        if node_type == "email" and (
            not incoming[node_id] or any(types[src] != "condition" for src in incoming[node_id])
        ):
            return False, "each email must have at least one condition input"
        if node_type == "email" and outgoing[node_id]:
            return False, "email nodes cannot have outputs"
    return True, "Workflow is valid"


def run_workflows(cfg: dict, store, storage_root: str, now: datetime | None = None):
    """Evaluate enabled workflows. An email fires only on a false-to-true condition transition."""
    now = now or utc_now()
    for workflow in store.list_workflows(enabled_only=True):
        try:
            owner = store.get_user(workflow["username"])
            if not owner or not owner.get("active") or not (owner.get("role") == "admin" or owner.get("can_api")):
                continue
            graph = workflow["graph"]
            nodes = {node["id"]: node for node in graph["nodes"]}
            incoming: dict[str, list[str]] = {node_id: [] for node_id in nodes}
            for edge in graph["edges"]:
                incoming[edge["target"]].append(edge["source"])
            station_rows = {}
            for node in nodes.values():
                if node["type"] == "station":
                    station = node["data"]["station"]
                    if not store.can_browse(owner, station):
                        raise PermissionError(f"station {station} is no longer readable")
                    rows = load_station_rows(storage_root, station, limit=1)
                    station_rows[node["id"]] = rows[-1] if rows else None
            fired_conditions = set()
            matched_stations: dict[str, list[str]] = {}
            for node in nodes.values():
                if node["type"] != "condition":
                    continue
                data = node["data"]
                matches = []
                for source in incoming[node["id"]]:
                    row = station_rows.get(source)
                    actual = to_float(row.get(data["field"])) if row else None
                    if actual is not None and OPERATORS[data["operator"]](actual, float(data["value"])):
                        matches.append(nodes[source]["data"]["station"])
                active = bool(matches)
                matched_stations[node["id"]] = matches
                state = store.get_workflow_state(workflow["id"], node["id"])
                if active and not state["active"]:
                    fired_conditions.add(node["id"])
                store.set_workflow_state(workflow["id"], node["id"], active)
            for node in nodes.values():
                if node["type"] != "email" or not any(src in fired_conditions for src in incoming[node["id"]]):
                    continue
                sources = [nodes[src] for src in incoming[node["id"]] if src in fired_conditions]
                details = ", ".join(
                    f"{src['data']['field']} {src['data']['operator']} {src['data']['value']} "
                    f"on {', '.join(matched_stations[src['id']])}"
                    for src in sources
                )
                sent = send_email(
                    cfg,
                    [node["data"]["recipient"]],
                    f"[Sensor Network] Workflow fired: {workflow['name']}",
                    f"Workflow: {workflow['name']}\nCondition: {details}\nFired at: {utc_iso(now)}\n",
                )
                if sent:
                    store.set_workflow_state(workflow["id"], node["id"], True, utc_iso(now))
            store.mark_workflow_run(workflow["id"])
        except Exception as exc:
            store.mark_workflow_run(workflow["id"], str(exc)[:500])
