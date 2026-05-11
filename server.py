#!/usr/bin/env python3
import argparse
import asyncio
import base64
import copy
import hashlib
import hmac
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml  # type: ignore
except Exception:
    yaml = None


def _parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""

    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]

    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None

    try:
        return int(value)
    except ValueError:
        pass

    return value


def parse_simple_yaml(raw: str) -> Dict[str, Any]:
    """Parse a small YAML subset used by this project.

    Supported:
    - comments and blank lines
    - key: value pairs
    - nested maps via two-space indentation
    """
    root: Dict[str, Any] = {}
    stack: List[tuple[int, Dict[str, Any]]] = [(-1, root)]

    for line_no, original in enumerate(raw.splitlines(), start=1):
        line = original.rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue

        indent = len(line) - len(stripped)
        if indent % 2 != 0:
            raise ValueError(f"Unsupported YAML indentation at line {line_no}: use multiples of 2 spaces")

        content = stripped
        if ":" not in content:
            raise ValueError(f"Invalid YAML line {line_no}: missing ':'")

        key, _, remainder = content.partition(":")
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid YAML line {line_no}: empty key")

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        current = stack[-1][1]
        remainder_value = remainder.strip()

        if not remainder_value:
            nested: Dict[str, Any] = {}
            current[key] = nested
            stack.append((indent, nested))
        else:
            current[key] = _parse_scalar(remainder_value)

    return root


DEFAULT_CONFIG: Dict[str, Any] = {
    "smtp_host": "127.0.0.1",
    "smtp_port": 2525,
    "smtp_username": "dev",
    "smtp_password": "dev",
    "smtp_auth_methods": ["PLAIN", "LOGIN", "CRAM-MD5"],
    "smtp_tls": False,
    "http_host": "127.0.0.1",
    "http_port": 8025,
    "max_messages": 500,
    "refresh_ms": 3000,
}


@dataclass
class CapturedEmail:
    id: int
    received_at: str
    peer: str
    mail_from: str
    rcpt_to: List[str]
    header_from: str
    header_to: str
    subject: str
    text_body: str
    html_body: str
    raw: str
    is_read: bool = False


class EmailStore:
    def __init__(self, max_messages: int = 500) -> None:
        self._messages: List[CapturedEmail] = []
        self._next_id = 1
        self._max_messages = max_messages
        self._lock = threading.Lock()

    def add(self, msg: CapturedEmail) -> None:
        with self._lock:
            self._messages.insert(0, msg)
            if len(self._messages) > self._max_messages:
                self._messages = self._messages[: self._max_messages]

    def all(self) -> List[CapturedEmail]:
        with self._lock:
            return list(self._messages)

    def get(self, message_id: int) -> Optional[CapturedEmail]:
        with self._lock:
            for message in self._messages:
                if message.id == message_id:
                    return message
        return None

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()

    def delete(self, message_id: int) -> bool:
        with self._lock:
            before = len(self._messages)
            self._messages = [m for m in self._messages if m.id != message_id]
            return len(self._messages) < before

    def mark_read(self, message_id: int) -> bool:
        with self._lock:
            for message in self._messages:
                if message.id == message_id:
                    message.is_read = True
                    return True
        return False

    def next_id(self) -> int:
        with self._lock:
            current = self._next_id
            self._next_id += 1
            return current


class SMTPConnection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        store: EmailStore,
        smtp_auth_methods: List[str],
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.store = store
        self.peer = str(writer.get_extra_info("peername"))
        self.smtp_auth_methods = smtp_auth_methods
        self.reset_transaction()

    def reset_transaction(self) -> None:
        self.mail_from = ""
        self.rcpt_to: List[str] = []

    async def send_line(self, line: str) -> None:
        self.writer.write((line + "\r\n").encode("utf-8"))
        await self.writer.drain()

    async def handle(self) -> None:
        await self.send_line("220 local-smtp ready")

        while not self.reader.at_eof():
            data = await self.reader.readline()
            if not data:
                break

            line = data.decode("utf-8", errors="replace").strip("\r\n")
            if not line:
                await self.send_line("500 Empty command")
                continue

            upper = line.upper()

            if upper.startswith("EHLO") or upper.startswith("HELO"):
                await self.send_line("250-local-smtp")
                if self.smtp_auth_methods:
                    await self.send_line("250-AUTH " + " ".join(self.smtp_auth_methods))
                await self.send_line("250 OK")
            elif upper.startswith("AUTH"):
                await self._handle_auth(line)
            elif upper.startswith("STARTTLS"):
                await self.send_line("454 TLS not available")
            elif upper.startswith("MAIL FROM:"):
                self.mail_from = line[10:].strip()
                self.rcpt_to = []
                await self.send_line("250 OK")
            elif upper.startswith("RCPT TO:"):
                recipient = line[8:].strip()
                self.rcpt_to.append(recipient)
                await self.send_line("250 OK")
            elif upper == "RSET":
                self.reset_transaction()
                await self.send_line("250 OK")
            elif upper == "NOOP":
                await self.send_line("250 OK")
            elif upper == "DATA":
                if not self.mail_from or not self.rcpt_to:
                    await self.send_line("503 Bad sequence of commands")
                    continue
                await self.send_line("354 End data with <CR><LF>.<CR><LF>")
                raw_message = await self._read_data_block()
                self._save_message(raw_message)
                self.reset_transaction()
                await self.send_line("250 Message accepted")
            elif upper == "QUIT":
                await self.send_line("221 Bye")
                break
            else:
                await self.send_line("502 Command not implemented")

        self.writer.close()
        await self.writer.wait_closed()

    async def _handle_auth(self, line: str) -> None:
        parts = line.split()
        mechanism = parts[1].upper() if len(parts) > 1 else ""
        if not mechanism:
            await self.send_line("501 Syntax: AUTH <mechanism>")
            return

        if mechanism not in self.smtp_auth_methods:
            await self.send_line("504 Unrecognized authentication type")
            return

        if mechanism == "CRAM-MD5":
            challenge = "local-smtp-test-challenge"
            await self.send_line("334 " + base64.b64encode(challenge.encode("utf-8")).decode("ascii"))
            response = await self.reader.readline()
            if not response:
                await self.send_line("535 Authentication failed")
                return

            try:
                decoded = base64.b64decode(response.strip() or b"", validate=False)
            except Exception:
                await self.send_line("535 Authentication failed")
                return
            # Accept any response in dev mode, but compute expected digest for parity/debugging.
            _ = hmac.new(b"dev", challenge.encode("utf-8"), hashlib.md5).hexdigest()
            if decoded:
                await self.send_line("235 Authentication successful")
                return

            await self.send_line("535 Authentication failed")
            return

        await self.send_line("235 Authentication successful")

    async def _read_data_block(self) -> str:
        lines: List[str] = []
        while True:
            data = await self.reader.readline()
            if not data:
                break
            line = data.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == ".":
                break
            if line.startswith(".."):
                line = line[1:]
            lines.append(line)
        return "\r\n".join(lines)

    def _save_message(self, raw_message: str) -> None:
        parsed = BytesParser(policy=policy.default).parsebytes(raw_message.encode("utf-8", errors="replace"))
        text_body, html_body = extract_bodies(parsed)

        captured = CapturedEmail(
            id=self.store.next_id(),
            received_at=datetime.now(timezone.utc).isoformat(),
            peer=self.peer,
            mail_from=self.mail_from,
            rcpt_to=list(self.rcpt_to),
            header_from=str(parsed.get("From", "")),
            header_to=str(parsed.get("To", "")),
            subject=str(parsed.get("Subject", "")),
            text_body=text_body,
            html_body=html_body,
            raw=raw_message,
        )
        self.store.add(captured)


class InboxHandler(BaseHTTPRequestHandler):
    ui_path: Path
    store: EmailStore
    config: Dict[str, Any]

    def do_GET(self) -> None:
        if self.path == "/":
            self._serve_ui()
            return

        if self.path == "/api/emails":
            all_messages = [asdict(m) for m in self.store.all()]
            self._send_json({"emails": all_messages})
            return

        if self.path == "/api/config":
            self._send_json({"config": self.config})
            return

        if self.path.startswith("/api/emails/"):
            maybe_id = self.path.replace("/api/emails/", "", 1)
            try:
                message_id = int(maybe_id)
            except ValueError:
                self._send_json({"error": "invalid id"}, status=HTTPStatus.BAD_REQUEST)
                return

            message = self.store.get(message_id)
            if message is None:
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return

            self._send_json(asdict(message))
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_DELETE(self) -> None:
        if self.path == "/api/emails":
            self.store.clear()
            self._send_json({"status": "ok"})
            return

        if self.path.startswith("/api/emails/"):
            maybe_id = self.path.replace("/api/emails/", "", 1)
            try:
                message_id = int(maybe_id)
            except ValueError:
                self._send_json({"error": "invalid id"}, status=HTTPStatus.BAD_REQUEST)
                return

            if not self.store.delete(message_id):
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return

            self._send_json({"status": "ok"})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_PATCH(self) -> None:
        if not self.path.startswith("/api/emails/") or not self.path.endswith("/read"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        maybe_id = self.path.replace("/api/emails/", "", 1).replace("/read", "", 1)
        maybe_id = maybe_id.strip("/")
        try:
            message_id = int(maybe_id)
        except ValueError:
            self._send_json({"error": "invalid id"}, status=HTTPStatus.BAD_REQUEST)
            return

        if not self.store.mark_read(message_id):
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return

        self._send_json({"status": "ok"})
        return

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_ui(self) -> None:
        content = self.ui_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def extract_bodies(message: Any) -> tuple[str, str]:
    text_parts: List[str] = []
    html_parts: List[str] = []

    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            try:
                payload = part.get_content()
            except Exception:
                payload = ""
            if not isinstance(payload, str):
                continue
            if content_type == "text/plain":
                text_parts.append(payload)
            elif content_type == "text/html":
                html_parts.append(payload)
    else:
        content_type = message.get_content_type()
        payload = message.get_content()
        if isinstance(payload, str):
            if content_type == "text/html":
                html_parts.append(payload)
            else:
                text_parts.append(payload)

    return "\n".join(text_parts).strip(), "\n".join(html_parts).strip()


def make_handler(store: EmailStore, ui_path: Path, config: Dict[str, Any]):
    class _Handler(InboxHandler):
        pass

    _Handler.store = store
    _Handler.ui_path = ui_path
    _Handler.config = config
    return _Handler


def run_http_server(host: str, port: int, store: EmailStore, ui_path: Path, config: Dict[str, Any]) -> ThreadingHTTPServer:
    handler_cls = make_handler(store, ui_path, config)
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


async def run_smtp_server(host: str, port: int, store: EmailStore, smtp_auth_methods: List[str]) -> None:
    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = SMTPConnection(reader, writer, store, smtp_auth_methods=smtp_auth_methods)
        try:
            await session.handle()
        except Exception:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(on_client, host=host, port=port)
    async with server:
        await server.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local SMTP catcher with inbox UI")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--smtp-host")
    parser.add_argument("--smtp-port", type=int)
    parser.add_argument("--smtp-username")
    parser.add_argument("--smtp-password")
    parser.add_argument("--smtp-auth-methods")
    parser.add_argument("--smtp-tls")
    parser.add_argument("--http-host")
    parser.add_argument("--http-port", type=int)
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--refresh-ms", type=int)
    return parser.parse_args()


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def parse_auth_methods(value: Any) -> List[str]:
    if isinstance(value, list):
        methods = [str(v).strip().upper() for v in value if str(v).strip()]
    else:
        methods = [m.strip().upper() for m in str(value).split(",") if m.strip()]
    return methods or ["PLAIN", "LOGIN", "CRAM-MD5"]


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config_file(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        return {}

    raw = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() in {".yaml", ".yml"}:
        if yaml is not None:
            loaded = yaml.safe_load(raw)
        else:
            loaded = parse_simple_yaml(raw)
    else:
        loaded = json.loads(raw)

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("Config root must be an object/map")
    return loaded


def build_config(args: argparse.Namespace) -> Dict[str, Any]:
    config_path = Path(args.config)
    file_config = load_config_file(config_path)

    config = deep_merge(DEFAULT_CONFIG, file_config)

    cli_overrides = {
        "smtp_host": args.smtp_host,
        "smtp_port": args.smtp_port,
        "smtp_username": args.smtp_username,
        "smtp_password": args.smtp_password,
        "smtp_auth_methods": args.smtp_auth_methods,
        "smtp_tls": args.smtp_tls,
        "http_host": args.http_host,
        "http_port": args.http_port,
        "max_messages": args.max_messages,
        "refresh_ms": args.refresh_ms,
    }
    for key, value in cli_overrides.items():
        if value is not None:
            config[key] = value

    config["smtp_auth_methods"] = parse_auth_methods(config.get("smtp_auth_methods", ""))
    config["smtp_tls"] = to_bool(config.get("smtp_tls", False))

    config["smtp_endpoint"] = f"{config['smtp_host']}:{config['smtp_port']}"
    config["ui_url"] = f"http://{config['http_host']}:{config['http_port']}"

    return config


def main() -> None:
    args = parse_args()
    here = Path(__file__).resolve().parent
    ui_path = here / "ui" / "index.html"

    if not ui_path.exists():
        raise FileNotFoundError(f"UI file not found: {ui_path}")

    config = build_config(args)
    store = EmailStore(max_messages=int(config["max_messages"]))
    httpd = run_http_server(str(config["http_host"]), int(config["http_port"]), store, ui_path, config)

    print(f"SMTP listening on {config['smtp_endpoint']}")
    print(f"Inbox UI: {config['ui_url']}")

    try:
        asyncio.run(
            run_smtp_server(
                str(config["smtp_host"]),
                int(config["smtp_port"]),
                store,
                smtp_auth_methods=list(config["smtp_auth_methods"]),
            )
        )
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
