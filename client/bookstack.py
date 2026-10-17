#!/usr/bin/env python3
"""Command-line client for a BookStack agent user.

Reads BOOKSTACK_URL and BOOKSTACK_TOKEN (id:secret) from the environment
or from --env-file. Prints API JSON to stdout. Errors go to stderr.

Examples:
  client/bookstack.sh books list
  client/bookstack.sh pages get 3 --field markdown
  client/bookstack.sh pages create --book 1 --name "Note" --markdown-file note.md
  client/bookstack.sh attachments upload --page 3 --file diagram.png
  client/bookstack.sh attachments get 1 --output diagram.png
  client/bookstack.sh search "capital region" --book 1
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"BookStack returned HTTP {status}: {message}")
        self.status = status


def die(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def unquote_env(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if value.startswith("$'") and value.endswith("'"):
        return bytes(value[2:-1], "utf-8").decode("unicode_escape")
    return value


def load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = unquote_env(value.strip())
    return data


def normalize_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def normalize_token(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("token "):
        value = value[6:].strip()
    if ":" not in value:
        die("BOOKSTACK_TOKEN must look like token_id:token_secret")
    return value


def resolve_config(args: argparse.Namespace) -> tuple[str, str]:
    file_values: dict[str, str] = {}
    if args.env_file:
        path = Path(args.env_file)
        if not path.is_file():
            die(f"Env file not found: {path}")
        file_values = load_env_file(path)

    def pick(*keys: str) -> str:
        for key in keys:
            if os.environ.get(key):
                return os.environ[key]
            if file_values.get(key):
                return file_values[key]
        return ""

    url = args.url or pick("BOOKSTACK_URL", "HOST")
    token = args.token or pick("BOOKSTACK_TOKEN", "AUTHORIZATION")
    if not token:
        token_id = pick("TOKEN_ID")
        token_secret = pick("TOKEN_SECRET")
        if token_id and token_secret:
            token = f"{token_id}:{token_secret}"
    if not url:
        die("Set BOOKSTACK_URL, or pass --url. Example: https://192.0.2.10.sslip.io")
    if not token:
        die("Set BOOKSTACK_TOKEN (id:secret), or pass --token.")
    return normalize_url(url), normalize_token(token)


def encode_multipart(fields: list[tuple[str, str]], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----BookStack" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(value.encode())
        chunks.append(b"\r\n")
    for name, filename, content_type, data in files:
        safe_name = filename.replace('"', "")
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{safe_name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
        )
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class Client:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        json_body: dict | None = None,
        fields: list[tuple[str, str]] | None = None,
        files: list[tuple[str, str, str, bytes]] | None = None,
        raw: bool = False,
    ):
        url = self.base_url + "/api/" + path.lstrip("/")
        if query:
            cleaned = {key: value for key, value in query.items() if value is not None}
            if cleaned:
                url += "?" + urllib.parse.urlencode(cleaned)
        headers = {"Authorization": f"Token {self.token}", "Accept": "application/json"}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif fields is not None or files is not None:
            data, content_type = encode_multipart(fields or [], files or [])
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "")
                disposition = response.headers.get("Content-Disposition", "")
                status = response.status
        except urllib.error.HTTPError as error:
            body = error.read()
            message = body.decode("utf-8", "replace")
            try:
                parsed = json.loads(body.decode("utf-8"))
                if isinstance(parsed, dict):
                    error_body = parsed.get("error")
                    if isinstance(error_body, dict) and error_body.get("message"):
                        message = str(error_body["message"])
                    elif parsed.get("message"):
                        message = str(parsed["message"])
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            raise ApiError(error.code, message) from error
        if status == 204 or not body:
            return None
        if raw or "application/json" not in content_type.split(";", 1)[0]:
            return {"raw": body, "content_type": content_type, "disposition": disposition}
        return json.loads(body.decode("utf-8"))


def compact(values: dict) -> dict:
    return {key: value for key, value in values.items() if value is not None}


def parse_tags(items: list[str] | None) -> list[dict[str, str]] | None:
    if not items:
        return None
    tags = []
    for item in items:
        if "=" in item:
            name, value = item.split("=", 1)
        else:
            name, value = item, ""
        tags.append({"name": name, "value": value})
    return tags


def read_text(value: str | None, file_value: str | None, label: str) -> str | None:
    if value is not None and file_value is not None:
        die(f"Pass --{label} or --{label}-file, not both.")
    if file_value is None:
        return value
    if file_value == "-":
        return sys.stdin.read()
    return Path(file_value).read_text(encoding="utf-8")


def listing_params(args: argparse.Namespace) -> dict[str, str]:
    params: dict[str, str] = {}
    if args.count is not None:
        params["count"] = str(args.count)
    if args.offset is not None:
        params["offset"] = str(args.offset)
    if args.sort:
        params["sort"] = args.sort
    for item in args.filter or []:
        if "=" not in item:
            die(f"--filter must look like field=value, got {item!r}")
        key, value = item.split("=", 1)
        params[f"filter[{key}]"] = value
    return params


def filename_from_disposition(header: str, fallback: str) -> str:
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)\"?", header or "")
    if not match:
        return fallback
    found = match.group(1) or match.group(2)
    return urllib.parse.unquote(found)


def write_bytes(data: bytes, output: str | None, fallback: str) -> None:
    if output == "-":
        sys.stdout.buffer.write(data)
        return
    path = Path(output or fallback)
    path.write_bytes(data)
    print(path)


def emit(data, args: argparse.Namespace) -> None:
    field = getattr(args, "field", None)
    if field:
        if not isinstance(data, dict) or field not in data:
            die(f"Response has no field {field!r}.")
        value = data[field]
        if isinstance(value, str):
            sys.stdout.write(value)
            if not value.endswith("\n"):
                sys.stdout.write("\n")
        else:
            json.dump(value, sys.stdout, indent=2, ensure_ascii=False)
            sys.stdout.write("\n")
        return
    if getattr(args, "format", "json") == "text" and isinstance(data, dict) and isinstance(data.get("data"), list):
        for item in data["data"]:
            if not isinstance(item, dict):
                print(item)
                continue
            name = item.get("name") or item.get("value") or ""
            kind = item.get("type")
            suffix = f"\t{kind}" if kind else ""
            print(f"{item.get('id', '')}\t{name}{suffix}")
        return
    if data is None:
        return
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def add_listing_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--count", type=int, help="page size, up to 500")
    parser.add_argument("--offset", type=int, help="items to skip")
    parser.add_argument("--sort", help="field name, prefix with - for descending")
    parser.add_argument(
        "--filter",
        action="append",
        help="field=value or field:op=value, op one of eq, ne, gt, lt, gte, lte, like",
    )


def add_tag_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tag", action="append", help="name=value; repeat to set more than one")


def file_part(field: str, path: Path) -> tuple[str, str, str, bytes]:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return field, path.name, content_type, path.read_bytes()


def cmd_system(client: Client, args: argparse.Namespace) -> None:
    emit(client.request("GET", "system"), args)


def cmd_search(client: Client, args: argparse.Namespace) -> None:
    params = {"query": args.query, "count": str(args.count), "page": str(args.page)}
    if args.book is not None:
        path = f"search/book/{args.book}"
    elif args.chapter is not None:
        path = f"search/chapter/{args.chapter}"
    else:
        path = "search"
    emit(client.request("GET", path, query=params), args)


def cmd_list(client: Client, resource: str, args: argparse.Namespace) -> None:
    emit(client.request("GET", resource, query=listing_params(args)), args)


def cmd_get(client: Client, resource: str, args: argparse.Namespace) -> None:
    emit(client.request("GET", f"{resource}/{args.id}"), args)


def cmd_delete(client: Client, resource: str, args: argparse.Namespace) -> None:
    client.request("DELETE", f"{resource}/{args.id}")
    emit({"deleted": True, "id": args.id}, args)


def body_from_content_args(args: argparse.Namespace, creating: bool) -> dict:
    markdown = read_text(args.markdown, args.markdown_file, "markdown")
    html = read_text(args.html, args.html_file, "html")
    if markdown is not None and html is not None:
        die("Pass markdown or HTML, not both.")
    if creating and not args.name:
        die("A new page needs --name.")
    if creating and markdown is None and html is None:
        die("A new page needs --markdown, --markdown-file, --html, or --html-file.")
    body = compact(
        {
            "name": args.name,
            "markdown": markdown,
            "html": html,
            "book_id": args.book,
            "chapter_id": args.chapter,
            "priority": args.priority,
            "tags": parse_tags(args.tag),
        }
    )
    return body


def cmd_page_create(client: Client, args: argparse.Namespace) -> None:
    if args.book is None and args.chapter is None:
        die("A new page needs --book or --chapter.")
    emit(client.request("POST", "pages", json_body=body_from_content_args(args, True)), args)


def cmd_page_update(client: Client, args: argparse.Namespace) -> None:
    body = body_from_content_args(args, False)
    if not body:
        die("Provide at least one field to update.")
    emit(client.request("PUT", f"pages/{args.id}", json_body=body), args)


def simple_create(client: Client, resource: str, body: dict) -> dict:
    return client.request("POST", resource, json_body=body)


def cmd_book_create(client: Client, args: argparse.Namespace) -> None:
    emit(
        simple_create(
            client,
            "books",
            compact({"name": args.name, "description": args.description, "tags": parse_tags(args.tag)}),
        ),
        args,
    )


def cmd_book_update(client: Client, args: argparse.Namespace) -> None:
    body = compact({"name": args.name, "description": args.description, "tags": parse_tags(args.tag)})
    if not body:
        die("Provide at least one field to update.")
    emit(client.request("PUT", f"books/{args.id}", json_body=body), args)


def cmd_chapter_create(client: Client, args: argparse.Namespace) -> None:
    emit(
        simple_create(
            client,
            "chapters",
            compact(
                {
                    "book_id": args.book,
                    "name": args.name,
                    "description": args.description,
                    "priority": args.priority,
                    "tags": parse_tags(args.tag),
                }
            ),
        ),
        args,
    )


def cmd_chapter_update(client: Client, args: argparse.Namespace) -> None:
    body = compact(
        {
            "book_id": args.book,
            "name": args.name,
            "description": args.description,
            "priority": args.priority,
            "tags": parse_tags(args.tag),
        }
    )
    if not body:
        die("Provide at least one field to update.")
    emit(client.request("PUT", f"chapters/{args.id}", json_body=body), args)


def cmd_shelf_create(client: Client, args: argparse.Namespace) -> None:
    emit(
        simple_create(
            client,
            "shelves",
            compact(
                {
                    "name": args.name,
                    "description": args.description,
                    "books": args.book,
                    "tags": parse_tags(args.tag),
                }
            ),
        ),
        args,
    )


def cmd_shelf_update(client: Client, args: argparse.Namespace) -> None:
    body = compact(
        {
            "name": args.name,
            "description": args.description,
            "books": args.book,
            "tags": parse_tags(args.tag),
        }
    )
    if not body:
        die("Provide at least one field to update.")
    emit(client.request("PUT", f"shelves/{args.id}", json_body=body), args)


def cmd_attachment_upload(client: Client, args: argparse.Namespace) -> None:
    path = Path(args.file)
    if not path.is_file():
        die(f"File not found: {path}")
    name = args.name or path.name
    result = client.request(
        "POST",
        "attachments",
        fields=[("name", name), ("uploaded_to", str(args.page))],
        files=[file_part("file", path)],
    )
    emit(result, args)


def cmd_attachment_link(client: Client, args: argparse.Namespace) -> None:
    emit(
        client.request(
            "POST",
            "attachments",
            json_body={"name": args.name, "uploaded_to": args.page, "link": args.url},
        ),
        args,
    )


def cmd_attachment_get(client: Client, args: argparse.Namespace) -> None:
    data = client.request("GET", f"attachments/{args.id}")
    if not isinstance(data, dict):
        die("Attachment response was empty.")
    if args.metadata:
        emit(data, args)
        return
    content = data.get("content")
    if data.get("external"):
        print(content or "")
        return
    if not isinstance(content, str):
        die("Attachment response did not include file content.")
    write_bytes(base64.b64decode(content), args.output, str(data.get("name") or f"attachment-{args.id}"))


def cmd_image_upload(client: Client, args: argparse.Namespace) -> None:
    path = Path(args.file)
    if not path.is_file():
        die(f"File not found: {path}")
    fields = [("type", "gallery"), ("uploaded_to", str(args.page))]
    if args.name:
        fields.append(("name", args.name))
    emit(client.request("POST", "image-gallery", fields=fields, files=[file_part("image", path)]), args)


def cmd_image_data(client: Client, args: argparse.Namespace) -> None:
    result = client.request("GET", f"image-gallery/{args.id}/data", raw=True)
    write_bytes(result["raw"], args.output, f"image-{args.id}")


def comment_html(args: argparse.Namespace) -> str:
    html = read_text(args.html, args.html_file, "html")
    text = read_text(args.text, args.text_file, "text")
    provided = [value for value in (html, text) if value is not None]
    if len(provided) != 1:
        die("Pass one of --text, --text-file, --html, or --html-file.")
    if html is not None:
        return html
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", escaped) if part.strip()]
    return "\n".join(f"<p>{part.replace(chr(10), '<br>')}</p>" for part in paragraphs)


def cmd_comment_create(client: Client, args: argparse.Namespace) -> None:
    body = {"page_id": args.page, "html": comment_html(args)}
    if args.reply_to is not None:
        body["reply_to"] = args.reply_to
    emit(client.request("POST", "comments", json_body=body), args)


def cmd_comment_update(client: Client, args: argparse.Namespace) -> None:
    body: dict = {}
    if any(value is not None for value in (args.text, args.text_file, args.html, args.html_file)):
        body["html"] = comment_html(args)
    if args.archive and args.unarchive:
        die("Pass --archive or --unarchive, not both.")
    if args.archive:
        body["archived"] = True
    if args.unarchive:
        body["archived"] = False
    if not body:
        die("Provide comment text or --archive / --unarchive.")
    emit(client.request("PUT", f"comments/{args.id}", json_body=body), args)


def cmd_comments_list(client: Client, args: argparse.Namespace) -> None:
    params = listing_params(args)
    if args.page is not None:
        params["filter[commentable_id]"] = str(args.page)
        params["filter[commentable_type]"] = "page"
    emit(client.request("GET", "comments", query=params), args)


def cmd_export(client: Client, args: argparse.Namespace) -> None:
    resource = {"page": "pages", "chapter": "chapters", "book": "books"}[args.kind]
    result = client.request("GET", f"{resource}/{args.id}/export/{args.export_format}", raw=True)
    text_formats = {"markdown", "html", "plaintext"}
    if args.output is None and args.export_format in text_formats:
        sys.stdout.buffer.write(result["raw"])
        if not result["raw"].endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")
        return
    extension = {"markdown": "md", "html": "html", "plaintext": "txt", "pdf": "pdf", "zip": "zip"}[args.export_format]
    fallback = filename_from_disposition(result["disposition"], f"{args.kind}-{args.id}.{extension}")
    if args.output is None:
        die(f"Pass --output for a {args.export_format} export.")
    write_bytes(result["raw"], args.output, fallback)


def cmd_tags(client: Client, args: argparse.Namespace) -> None:
    if args.tag_command == "names":
        emit(client.request("GET", "tags/names", query=listing_params(args)), args)
        return
    emit(client.request("GET", "tags/values-for-name", query={"name": args.name, **listing_params(args)}), args)


def add_content_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name")
    parser.add_argument("--markdown")
    parser.add_argument("--markdown-file", help="path, or - to read stdin")
    parser.add_argument("--html")
    parser.add_argument("--html-file", help="path, or - to read stdin")
    parser.add_argument("--book", type=int)
    parser.add_argument("--chapter", type=int)
    parser.add_argument("--priority", type=int)
    add_tag_arg(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bookstack",
        description="Call the BookStack API as an agent user.",
        allow_abbrev=False,
    )
    parser.add_argument("--url", help="BookStack base URL, overriding BOOKSTACK_URL")
    parser.add_argument("--token", help="token id and secret as id:secret, overriding BOOKSTACK_TOKEN")
    parser.add_argument("--env-file", help="file of KEY=value settings; environment variables win")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="text prints id and name for lists")
    parser.add_argument("--field", help="print one field from a JSON object instead of the whole response")
    sub = parser.add_subparsers(dest="command", required=True)

    system = sub.add_parser("system", help="show the instance version and base URL")
    system.set_defaults(handler=cmd_system)

    search = sub.add_parser("search", help="search shelves, books, chapters, and pages")
    search.add_argument("query")
    search.add_argument("--book", type=int, help="limit the search to one book")
    search.add_argument("--chapter", type=int, help="limit the search to one chapter")
    search.add_argument("--count", type=int, default=20)
    search.add_argument("--page", type=int, default=1, help="result page, starting at 1")
    search.set_defaults(handler=cmd_search)

    for resource, help_text in (
        ("shelves", "shelves group books"),
        ("books", "books hold chapters and pages"),
        ("chapters", "chapters belong to a book"),
        ("pages", "pages are the documents agents read and write"),
    ):
        command = sub.add_parser(resource, help=help_text)
        actions = command.add_subparsers(dest="action", required=True)
        listing = actions.add_parser("list", help=f"list visible {resource}")
        add_listing_args(listing)
        listing.set_defaults(handler=lambda client, args, resource=resource: cmd_list(client, resource, args))
        reading = actions.add_parser("get", help=f"read one {resource[:-1]}")
        reading.add_argument("id", type=int)
        reading.set_defaults(handler=lambda client, args, resource=resource: cmd_get(client, resource, args))
        deleting = actions.add_parser("delete", help=f"delete one {resource[:-1]}")
        deleting.add_argument("id", type=int)
        deleting.set_defaults(handler=lambda client, args, resource=resource: cmd_delete(client, resource, args))

    def actions_of(command_name: str) -> argparse._SubParsersAction:
        command = sub.choices[command_name]
        for action in command._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action
        raise RuntimeError(f"{command_name} has no subcommands")

    page_actions = actions_of("pages")
    creating = page_actions.add_parser("create", help="create a page in a book or chapter")
    add_content_flags(creating)
    creating.set_defaults(handler=cmd_page_create)
    updating = page_actions.add_parser("update", help="update a page; only the fields you pass are sent")
    updating.add_argument("id", type=int)
    add_content_flags(updating)
    updating.set_defaults(handler=cmd_page_update)

    def add_named_write(resource: str, create_handler, update_handler, extras) -> None:
        actions = actions_of(resource)
        creating = actions.add_parser("create", help=f"create a {resource[:-1]}")
        creating.add_argument("--name", required=True)
        creating.add_argument("--description")
        add_tag_arg(creating)
        extras(creating, True)
        creating.set_defaults(handler=create_handler)
        updating = actions.add_parser("update", help=f"update a {resource[:-1]}")
        updating.add_argument("id", type=int)
        updating.add_argument("--name")
        updating.add_argument("--description")
        add_tag_arg(updating)
        extras(updating, False)
        updating.set_defaults(handler=update_handler)

    def book_extras(parser, creating: bool) -> None:
        return None

    def chapter_extras(parser, creating: bool) -> None:
        parser.add_argument("--book", type=int, required=creating)
        parser.add_argument("--priority", type=int)

    def shelf_extras(parser, creating: bool) -> None:
        parser.add_argument("--book", type=int, action="append", help="book id to place on the shelf; repeat for more")

    add_named_write("books", cmd_book_create, cmd_book_update, book_extras)
    add_named_write("chapters", cmd_chapter_create, cmd_chapter_update, chapter_extras)
    add_named_write("shelves", cmd_shelf_create, cmd_shelf_update, shelf_extras)

    attachments = sub.add_parser("attachments", help="files stored on a page")
    attachment_actions = attachments.add_subparsers(dest="action", required=True)
    listing = attachment_actions.add_parser("list", help="list attachments; filter with uploaded_to=<page id>")
    add_listing_args(listing)
    listing.set_defaults(handler=lambda client, args: cmd_list(client, "attachments", args))
    reading = attachment_actions.add_parser("get", help="download an attachment")
    reading.add_argument("id", type=int)
    reading.add_argument("--output", help="file path, or - for stdout; defaults to the attachment name")
    reading.add_argument("--metadata", action="store_true", help="print the JSON record instead of the file")
    reading.set_defaults(handler=cmd_attachment_get)
    uploading = attachment_actions.add_parser("upload", help="upload a file onto a page")
    uploading.add_argument("--page", type=int, required=True)
    uploading.add_argument("--file", required=True)
    uploading.add_argument("--name", help="defaults to the file name")
    uploading.set_defaults(handler=cmd_attachment_upload)
    linking = attachment_actions.add_parser("link", help="attach an external URL to a page")
    linking.add_argument("--page", type=int, required=True)
    linking.add_argument("--name", required=True)
    linking.add_argument("--url", required=True)
    linking.set_defaults(handler=cmd_attachment_link)
    deleting = attachment_actions.add_parser("delete", help="delete an attachment")
    deleting.add_argument("id", type=int)
    deleting.set_defaults(handler=lambda client, args: cmd_delete(client, "attachments", args))

    images = sub.add_parser("images", help="gallery images embedded in page content")
    image_actions = images.add_subparsers(dest="action", required=True)
    listing = image_actions.add_parser("list", help="list gallery images")
    add_listing_args(listing)
    listing.set_defaults(handler=lambda client, args: cmd_list(client, "image-gallery", args))
    reading = image_actions.add_parser("get", help="read image metadata, including HTML and markdown snippets")
    reading.add_argument("id", type=int)
    reading.set_defaults(handler=lambda client, args: cmd_get(client, "image-gallery", args))
    data = image_actions.add_parser("data", help="download the image bytes")
    data.add_argument("id", type=int)
    data.add_argument("--output", help="file path, or - for stdout")
    data.set_defaults(handler=cmd_image_data)
    uploading = image_actions.add_parser("upload", help="upload an image onto a page")
    uploading.add_argument("--page", type=int, required=True)
    uploading.add_argument("--file", required=True)
    uploading.add_argument("--name")
    uploading.set_defaults(handler=cmd_image_upload)
    deleting = image_actions.add_parser("delete", help="delete an image")
    deleting.add_argument("id", type=int)
    deleting.set_defaults(handler=lambda client, args: cmd_delete(client, "image-gallery", args))

    comments = sub.add_parser("comments", help="comments on pages")
    comment_actions = comments.add_subparsers(dest="action", required=True)
    listing = comment_actions.add_parser("list", help="list comments")
    listing.add_argument("--page", type=int, help="only comments on this page")
    add_listing_args(listing)
    listing.set_defaults(handler=cmd_comments_list)
    reading = comment_actions.add_parser("get", help="read one comment and its replies")
    reading.add_argument("id", type=int)
    reading.set_defaults(handler=lambda client, args: cmd_get(client, "comments", args))
    creating = comment_actions.add_parser("create", help="comment on a page")
    creating.add_argument("--page", type=int, required=True)
    creating.add_argument("--reply-to", type=int, help="local_id of the comment being replied to")
    creating.add_argument("--text")
    creating.add_argument("--text-file")
    creating.add_argument("--html")
    creating.add_argument("--html-file")
    creating.set_defaults(handler=cmd_comment_create)
    updating = comment_actions.add_parser("update", help="edit or archive a comment")
    updating.add_argument("id", type=int)
    updating.add_argument("--text")
    updating.add_argument("--text-file")
    updating.add_argument("--html")
    updating.add_argument("--html-file")
    updating.add_argument("--archive", action="store_true")
    updating.add_argument("--unarchive", action="store_true")
    updating.set_defaults(handler=cmd_comment_update)
    deleting = comment_actions.add_parser("delete", help="delete a comment")
    deleting.add_argument("id", type=int)
    deleting.set_defaults(handler=lambda client, args: cmd_delete(client, "comments", args))

    export = sub.add_parser("export", help="download a page, chapter, or book")
    export.add_argument("kind", choices=["page", "chapter", "book"])
    export.add_argument("id", type=int)
    export.add_argument("--format", dest="export_format", choices=["markdown", "html", "plaintext", "pdf", "zip"], default="markdown")
    export.add_argument("--output", help="file path; text formats print to stdout when omitted")
    export.set_defaults(handler=cmd_export)

    tags = sub.add_parser("tags", help="list tag names and values already used on visible content")
    tag_actions = tags.add_subparsers(dest="tag_command", required=True)
    names = tag_actions.add_parser("names", help="list tag names")
    add_listing_args(names)
    names.set_defaults(handler=cmd_tags)
    values = tag_actions.add_parser("values", help="list values used for one tag name")
    values.add_argument("--name", required=True)
    add_listing_args(values)
    values.set_defaults(handler=cmd_tags)

    return parser


def split_global_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Accept --url, --token, --env-file, --format, and --field before or after the subcommand."""
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--url", default=None)
    pre.add_argument("--token", default=None)
    pre.add_argument("--env-file", default=None)
    pre.add_argument("--format", default=None)
    pre.add_argument("--field", default=None)
    return pre.parse_known_args(argv)


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    global_args, rest = split_global_args(argv)
    # export uses --format for markdown/html/pdf/zip. List output uses json or text.
    if global_args.format not in (None, "json", "text"):
        rest = [*rest, "--format", global_args.format]
        global_args.format = None
    parser = build_parser()
    args = parser.parse_args(rest)
    if global_args.url:
        args.url = global_args.url
    if global_args.token:
        args.token = global_args.token
    if global_args.env_file:
        args.env_file = global_args.env_file
    if global_args.format:
        args.format = global_args.format
    if global_args.field:
        args.field = global_args.field
    if args.command == "search" and args.book is not None and args.chapter is not None:
        die("Pass --book or --chapter, not both.")
    try:
        url, token = resolve_config(args)
        args.handler(Client(url, token), args)
    except ApiError as error:
        die(str(error))
    except OSError as error:
        die(str(error))


if __name__ == "__main__":
    main()
