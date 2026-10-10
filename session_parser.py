"""Parse client session logs into turns, messages, and paired tool calls."""

import json


def extract_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    text_types = {"text", "input_text", "output_text"}
    return "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") in text_types
    )


class SessionParser:
    def __init__(self, path, metadata, sanitize):
        self.path = path
        self.sanitize = sanitize
        self.session = dict(metadata or {})
        self.session.setdefault("id", path.stem)
        default_client = "Codex App"
        self.session.setdefault("client", default_client)
        self.session.update(path=str(path), turns=[], available=True)
        self.current_turn = None
        self.pending_messages = []
        self.calls_by_id = {}

    def parse(self):
        try:
            with self.path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    try:
                        event = json.loads(line)
                    except ValueError:
                        # The client's final line may still be in progress.
                        continue
                    self.consume_event(event, line_number)
        except (OSError, UnicodeError) as error:
            self.session.update(available=False, error=str(error))

        if not self.session.get("title"):
            self.session["title"] = self.first_user_text() or "未命名会话"
        return self.session

    def first_user_text(self):
        for turn in self.session["turns"]:
            for message in turn["messages"]:
                if message["role"] == "user":
                    return message["text"][:100]
        return None

    def begin_turn(self, identifier, timestamp, model=None, effort=None):
        self.current_turn = {
            "id": identifier,
            "time": timestamp,
            "model": model,
            "client_effort": effort,
            "messages": [],
            "tools": [],
            "evidence": [],
        }
        self.session["turns"].append(self.current_turn)

    def consume_event(self, event, line_number):
        event_type = event.get("type")
        timestamp = event.get("timestamp")
        payload = event.get("payload", {})

        if event_type == "session_meta":
            self.consume_metadata(payload)
            return
        if event_type == "turn_context":
            self.consume_turn_context(payload, timestamp, line_number)
            return
        if event_type == "event_msg":
            self.consume_lifecycle(payload, timestamp)
            return

        item = self.extract_item(event, line_number)
        if item is None:
            return
        self.consume_message(item, event, line_number)
        if self.current_turn is None:
            return

        if item.get("model"):
            self.current_turn["model"] = item["model"]
        if item.get("id"):
            self.current_turn.setdefault("output_ids", []).append(item["id"])
        if item.get("usage"):
            self.current_turn["usage"] = item["usage"]
        if item.get("stop_reason") == "end_turn":
            self.current_turn["end_time"] = timestamp

        self.consume_tools(item, timestamp, line_number)

    def consume_metadata(self, payload):
        for key in ("id", "cwd", "cli_version"):
            if payload.get(key):
                self.session[key] = payload[key]
        if payload.get("originator") == "Codex Desktop":
            self.session["client"] = "Codex App"
        else:
            self.session["client"] = "Codex CLI"

    def consume_turn_context(self, payload, timestamp, line_number):
        turn_id = payload.get("turn_id") or f"turn-{line_number}"
        if self.current_turn and self.current_turn["id"] == turn_id:
            self.current_turn.update(
                model=payload.get("model"), client_effort=payload.get("effort")
            )
            return

        self.begin_turn(turn_id, timestamp, payload.get("model"), payload.get("effort"))
        self.current_turn["messages"].extend(self.pending_messages)
        self.pending_messages.clear()

    def consume_lifecycle(self, payload, timestamp):
        completion_types = {"task_complete", "turn_complete", "turn_aborted"}
        if self.current_turn and payload.get("type") in completion_types:
            self.current_turn.update(end_time=timestamp, end_state=payload.get("type"))

    def extract_item(self, event, line_number):
        if event.get("type") == "response_item":
            return event.get("payload", {})
        return None

    def consume_message(self, item, event, line_number):
        role = item.get("role")
        text = extract_text(item.get("content"))
        if role not in ("user", "assistant") or not text:
            return

        context_prefixes = (
            "# AGENTS.md",
            "<environment_context>",
            "<permissions instructions>",
        )
        if text.lstrip().startswith(context_prefixes):
            return

        message = {
            "id": item.get("id") or event.get("uuid"),
            "role": role,
            "text": self.sanitize(text[:16000]),
            "time": event.get("timestamp"),
            "line": line_number,
        }
        if self.current_turn:
            self.current_turn["messages"].append(message)
        else:
            self.pending_messages.append(message)

    def consume_tools(self, item, timestamp, line_number):
        content = item.get("content")
        blocks = content if isinstance(content, list) else []
        parts = [item] + [block for block in blocks if isinstance(block, dict)]

        for part in parts:
            part_type = part.get("type")
            if part_type in ("function_call", "custom_tool_call"):
                self.record_tool_call(part, timestamp, line_number)
            elif part_type in (
                "function_call_output",
                "custom_tool_call_output",
            ):
                self.record_tool_result(part, timestamp)

    def record_tool_call(self, part, timestamp, line_number):
        call_id = part.get("call_id") or part.get("id") or f"call-{line_number}"
        arguments = part.get("arguments", part.get("input", ""))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass

        call = {
            "id": call_id,
            "name": part.get("name", "tool"),
            "start_time": timestamp,
            "line": line_number,
            "arguments": self.sanitize(arguments),
            "status": "pending",
        }
        self.current_turn["tools"].append(call)
        self.calls_by_id[call_id] = call

    def record_tool_result(self, part, timestamp):
        call_id = part.get("call_id") or part.get("tool_use_id")
        call = self.calls_by_id.get(call_id)
        if call is None:
            return

        result = part.get("output", part.get("content", ""))
        if isinstance(result, str):
            result = result[:16000]
        call.update(
            end_time=timestamp,
            result=self.sanitize(result),
            status="error" if part.get("is_error") else "returned",
        )
