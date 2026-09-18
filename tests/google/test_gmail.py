import base64
import json
from email import message_from_bytes, policy
from email.header import decode_header, make_header

import httpx
import pytest

from app.google.auth import GoogleApiError, GoogleAuthError, TransientGoogleError
from app.google.gmail import GmailClient, parse_message, split_sender
from tests.conftest import kst
from tests.google.test_calendar import FakeAuth

# 2026-09-18 09:00 KST = 1789689600000 ms
INTERNAL_DATE = str(int(kst(9, 18, 9).timestamp() * 1000))

MESSAGE = {
    "id": "msg-1",
    "threadId": "thread-1",
    "snippet": "다음 주 면담 일정 안내드립니다.",
    "internalDate": INTERNAL_DATE,
    "labelIds": ["INBOX", "UNREAD"],
    "payload": {
        "headers": [
            {"name": "From", "value": "홍길동 <prof@example.ac.kr>"},
            {"name": "Subject", "value": "면담 일정"},
            {"name": "Date", "value": "Fri, 18 Sep 2026 09:00:00 +0900"},
        ]
    },
}


def client_with(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GmailClient(FakeAuth(), http), http


def test_split_sender_handles_both_forms():
    assert split_sender("홍길동 <prof@example.ac.kr>") == ("홍길동", "prof@example.ac.kr")
    assert split_sender('"학과 사무실" <office@example.ac.kr>') == ("학과 사무실", "office@example.ac.kr")
    assert split_sender("noreply@example.com") == ("", "noreply@example.com")


def test_parse_message_keeps_only_what_alerts_need():
    message = parse_message(MESSAGE)
    assert message.message_id == "msg-1" and message.thread_id == "thread-1"
    assert message.sender == "prof@example.ac.kr" and message.sender_name == "홍길동"
    assert message.sender_domain == "example.ac.kr"
    assert message.subject == "면담 일정" and "면담" in message.snippet
    assert message.received_at == kst(9, 18, 9)
    assert "INBOX" in message.labels


def test_parse_message_survives_missing_fields():
    message = parse_message({"id": "msg-2", "payload": {}})
    assert message.message_id == "msg-2" and message.sender == "" and message.subject == ""


async def test_new_message_ids_reads_history_in_order():
    def handler(request):
        assert "startHistoryId=100" in str(request.url)
        return httpx.Response(
            200,
            json={
                "historyId": "140",
                "history": [
                    {"messagesAdded": [{"message": {"id": "msg-2", "labelIds": ["INBOX"]}}]},
                    {"messagesAdded": [{"message": {"id": "msg-1", "labelIds": ["INBOX"]}}]},
                ],
            },
        )

    client, http = client_with(handler)
    async with http:
        ids, latest = await client.new_message_ids("100")
    assert ids == ["msg-1", "msg-2"] and latest == "140"


async def test_sent_and_draft_messages_are_skipped():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "historyId": "141",
                "history": [
                    {"messagesAdded": [{"message": {"id": "sent-1", "labelIds": ["SENT"]}}]},
                    {"messagesAdded": [{"message": {"id": "draft-1", "labelIds": ["DRAFT"]}}]},
                    {"messagesAdded": [{"message": {"id": "msg-3", "labelIds": ["INBOX"]}}]},
                ],
            },
        )

    client, http = client_with(handler)
    async with http:
        ids, _ = await client.new_message_ids("100")
    assert ids == ["msg-3"]


async def test_expired_cursor_falls_back_to_recent_mail():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "/history" in request.url.path:
            return httpx.Response(404, json={})
        if "/messages" in request.url.path:
            return httpx.Response(200, json={"messages": [{"id": "msg-9"}]})
        return httpx.Response(200, json={"historyId": "200"})

    client, http = client_with(handler)
    async with http:
        ids, latest = await client.new_message_ids("1")
    assert ids == ["msg-9"] and latest == "200"
    assert any("newer_than" in url for url in calls)


async def test_message_asks_only_for_metadata():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=MESSAGE)

    client, http = client_with(handler)
    async with http:
        message = await client.message("msg-1")
    assert "format=metadata" in seen["url"] and "metadataHeaders=From" in seen["url"]
    assert message.subject == "면담 일정"


@pytest.mark.parametrize(("method", "path"), [("trash", "/trash"), ("untrash", "/untrash")])
async def test_trash_and_untrash(method, path):
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        return httpx.Response(200, json={})

    client, http = client_with(handler)
    async with http:
        await getattr(client, method)("msg-1")
    assert seen["method"] == "POST" and seen["path"].endswith(path)


async def test_create_draft_saves_a_reply_without_sending():
    sent = {}

    def handler(request):
        sent["path"] = request.url.path
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "draft-1"})

    client, http = client_with(handler)
    async with http:
        draft_id = await client.create_draft("thread-1", "prof@example.ac.kr", "면담 일정", "네, 수요일에 뵙겠습니다.")

    assert draft_id == "draft-1"
    assert sent["path"].endswith("/drafts") and sent["body"]["message"]["threadId"] == "thread-1"
    raw = base64.urlsafe_b64decode(sent["body"]["message"]["raw"])
    mail = message_from_bytes(raw, policy=policy.default)
    assert mail["To"] == "prof@example.ac.kr"
    # 한글 제목은 RFC 2047로 인코딩되어 들어간다
    assert str(make_header(decode_header(mail["Subject"]))) == "Re: 면담 일정"
    assert "수요일에 뵙겠습니다" in mail.get_content()
    # 발송 주소(/send)는 어디에서도 부르지 않는다
    assert "/send" not in sent["path"]


async def test_reply_detection_looks_for_sent_messages():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "messages": [
                    {"id": "m1", "labelIds": ["INBOX"], "internalDate": INTERNAL_DATE},
                    {"id": "m2", "labelIds": ["SENT"], "internalDate": str(int(kst(9, 18, 15).timestamp() * 1000))},
                ]
            },
        )

    client, http = client_with(handler)
    async with http:
        assert await client.thread_has_reply("thread-1", kst(9, 18, 10)) is True
        assert await client.thread_has_reply("thread-1", kst(9, 19, 10)) is False


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(401, json={}), GoogleAuthError),
        (httpx.Response(429, json={}), TransientGoogleError),
        (httpx.Response(500, text="x"), TransientGoogleError),
        (httpx.Response(400, json={}), GoogleApiError),
    ],
)
async def test_error_mapping(response, expected):
    client, http = client_with(lambda request: response)
    async with http:
        with pytest.raises(expected):
            await client.message("msg-1")


async def test_connection_error_is_transient():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    client, http = client_with(handler)
    async with http:
        with pytest.raises(TransientGoogleError):
            await client.current_history_id()
