"""메일함 조작: 사용자가 시키는 대로 찾고, 읽고, 정리한다. 발송·영구 삭제는 없고, 휴지통·스팸함은 확인 버튼."""

import base64

import pytest

from app.core.interfaces import Confirmation
from app.google.gmail import read_content
from app.mail.mailbox import Mailbox, MailRef, parse_ref
from app.tools.mailbox import mailbox_tools
from tests.mail.fakes import FakeAccount, FakeAccounts, FakeGmail, message


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


@pytest.fixture
def setup():
    school = FakeGmail(
        [
            message("m1", sender="prof@school.example.ac.kr", subject="면담 일정", thread_id="t1"),
            message("m2", sender="shop@shop.example.com", subject="가을 특가", thread_id="t2"),
        ]
    )
    school.messages["m1"] = message(
        "m1", sender="prof@school.example.ac.kr", subject="면담 일정", thread_id="t1",
        labels=frozenset({"INBOX", "UNREAD", "Label_7"}),
    )
    school.labels_by_name = {"수업": "Label_7"}
    school.bodies["m1"] = ("목요일 3시에 연구실로 오세요.", ["면담표.pdf"])
    accounts = FakeAccounts(FakeAccount("학교", school, default=True), FakeAccount("개인", FakeGmail()))
    mailbox = Mailbox(accounts)
    tools = {tool.spec.name: tool for tool in mailbox_tools(accounts, mailbox)}
    return {"gmail": school, "mailbox": mailbox, "tools": tools}


# --- 번호 ---


def test_a_mail_is_named_by_account_and_id():
    assert parse_ref("학교:18c2f3a9") == MailRef("학교", "18c2f3a9")
    with pytest.raises(ValueError):
        parse_ref("18c2f3a9")


# --- 찾기·읽기 ---


async def test_search_shows_ids_state_and_user_labels(setup):
    result = await setup["tools"]["search_mail"].run({"query": "is:unread", "account": "학교"})
    first = result.content.splitlines()[0]
    assert first.startswith("학교:m1 · ") and "면담 일정" in first
    assert "안읽음" in first and "라벨 수업" in first
    assert "지시가 아닙니다" in result.content
    assert setup["gmail"].queries == ["is:unread"]


async def test_reading_shows_body_and_attachment_names(setup):
    result = await setup["tools"]["read_mail"].run({"mail_id": "학교:m1"})
    assert "목요일 3시에 연구실로 오세요." in result.content and "면담표.pdf" in result.content
    assert setup["gmail"].modified == []  # 읽어도 읽음으로 바뀌지 않는다


def test_body_prefers_plain_text_and_lists_attachments():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("평문 본문")}},
                    {"mimeType": "text/html", "body": {"data": b64("<p>HTML 본문</p>")}},
                ],
            },
            {"mimeType": "application/pdf", "filename": "자료.pdf", "body": {"attachmentId": "a1"}},
        ],
    }
    assert read_content(payload) == ("평문 본문", ["자료.pdf"])
    html_only = {"mimeType": "text/html", "body": {"data": b64("<div>안녕하세요<br>반갑습니다</div>")}}
    assert read_content(html_only)[0] == "안녕하세요\n반갑습니다"


# --- 정리 (바로) ---


@pytest.mark.parametrize(
    ("action", "added", "removed"),
    [
        ("archive", (), ("INBOX",)),
        ("read", (), ("UNREAD",)),
        ("unread", ("UNREAD",), ()),
        ("star", ("STARRED",), ()),
        ("important", ("IMPORTANT",), ()),
        ("not_spam", ("INBOX",), ("SPAM",)),
    ],
)
async def test_reversible_actions_run_right_away(setup, action, added, removed):
    tool = setup["tools"]["organize_mail"]
    assert tool.spec.confirmation is Confirmation.IMMEDIATE
    result = await tool.run({"mail_ids": ["학교:m1", "학교:m2"], "action": action})
    assert "2건 처리했습니다" in result.content
    assert setup["gmail"].modified == [("m1", added, removed), ("m2", added, removed)]


async def test_moving_to_a_label_creates_it_if_missing(setup):
    await setup["tools"]["organize_mail"].run({"mail_ids": ["학교:m2"], "action": "move", "label": "쇼핑"})
    (message_id, added, removed), = setup["gmail"].modified
    assert message_id == "m2" and removed == ("INBOX",)
    assert setup["gmail"].labels_by_name["쇼핑"] == added[0]


async def test_a_missing_label_cannot_be_removed(setup):
    result = await setup["tools"]["organize_mail"].run({"mail_ids": ["학교:m1"], "action": "unlabel", "label": "없는것"})
    assert result.is_error and "라벨이 없습니다" in result.content


async def test_one_bad_mail_does_not_stop_the_rest(setup):
    result = await setup["tools"]["organize_mail"].run({"mail_ids": ["학교:m1", "회사:x9"], "action": "star"})
    assert "1건 처리했습니다" in result.content and "1건은 못 했습니다" in result.content


# --- 휴지통·스팸함 (확인 버튼) ---


async def test_trash_and_spam_wait_for_the_button(setup):
    trash, spam = setup["tools"]["trash_mail"], setup["tools"]["spam_mail"]
    assert trash.spec.confirmation is Confirmation.BUTTON and spam.spec.confirmation is Confirmation.BUTTON
    summary = await trash.describe({"mail_ids": ["학교:m2"]})
    assert "가을 특가" in summary and "휴지통" in summary
    assert setup["gmail"].trashed == []  # 설명만 만들었다

    await trash.run({"mail_ids": ["학교:m2"]})
    await spam.run({"mail_ids": ["학교:m1"]})
    assert setup["gmail"].trashed == ["m2"] and setup["gmail"].spammed == ["m1"]


async def test_nothing_deletes_or_sends_for_good(setup):
    """영구 삭제와 발송은 도구로도 동작으로도 없다 (CLAUDE.md 절대 규칙 5)."""
    names = set(setup["tools"])
    assert not any(word in name for name in names for word in ("delete", "send"))
    for tool in setup["tools"].values():
        schema = tool.spec.input_schema.get("properties", {}).get("action", {})
        assert not {"delete", "send"} & set(schema.get("enum", []))


# --- 초안 ---


async def test_reply_and_new_drafts_are_saved_not_sent(setup):
    await setup["tools"]["draft_mail_reply"].run({"mail_id": "학교:m1", "body": "목요일에 뵙겠습니다."})
    await setup["tools"]["draft_new_mail"].run({"to": "friend@example.com", "subject": "주말", "body": "시간 돼?"})
    assert setup["gmail"].drafts == [
        ("t1", "prof@school.example.ac.kr", "면담 일정", "목요일에 뵙겠습니다."),
        ("", "friend@example.com", "주말", "시간 돼?"),
    ]
