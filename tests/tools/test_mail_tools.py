import pytest

from app.core.interfaces import Confirmation
from app.mail.service import MailService
from app.storage.mail import MailCleanupLog, MailRuleRepository, MailStateStore, WaitingReplyStore
from app.tools.common import ToolInputError
from app.tools.mail import NOT_CONNECTED, mail_tools, not_connected_mail_tools
from tests.conftest import kst
from tests.mail.fakes import FakeAccount, FakeAccounts, FakeGmail, message

NOW = kst(9, 18, 14)


@pytest.fixture
def setup(db):
    gmail = FakeGmail(
        [
            message(),
            message(message_id="msg-2", sender="friend@example.com", sender_name="", subject="주말 약속"),
        ]
    )
    accounts = FakeAccounts(FakeAccount("개인", gmail, default=True))
    rules = MailRuleRepository(db)
    service = MailService(accounts, MailCleanupLog(db), WaitingReplyStore(db), MailStateStore(db), lambda: NOW)
    tools = {tool.spec.name: tool for tool in mail_tools(accounts, rules, service, lambda: NOW)}
    return {
        "tools": tools,
        "rules": rules,
        "service": service,
        "gmail": gmail,
        "waiting": WaitingReplyStore(db),
        "cleanup": MailCleanupLog(db),
        "accounts": accounts,
    }


# --- 규칙 ---


async def test_empty_rule_list_explains_the_default(setup):
    result = await setup["tools"]["list_mail_rules"].run({})
    assert "등록된 메일 유형이 없습니다" in result.content and "professor" in result.content


async def test_add_rule_confirms_then_saves(setup):
    tool = setup["tools"]["add_mail_rule"]
    args = {"name": "교수님 메일", "kind": "professor", "domains": "example.ac.kr, cs.example.ac.kr"}

    summary = await tool.describe(args)
    assert summary.startswith("메일 규칙 추가 — 교수님 메일 [교수님·학과]")
    assert "도메인 example.ac.kr, cs.example.ac.kr" in summary
    assert await setup["rules"].list_all() == []

    result = await tool.run(args)
    saved = await setup["rules"].list_all()
    assert len(saved) == 1 and saved[0].domains == ("example.ac.kr", "cs.example.ac.kr")
    assert "등록했습니다" in result.content

    listed = await setup["tools"]["list_mail_rules"].run({})
    assert "#1 교수님 메일" in listed.content


@pytest.mark.parametrize(
    ("args", "message_text"),
    [
        ({"name": "x", "kind": "spam", "keywords": "a"}, "유형은"),
        ({"name": "x", "kind": "ad"}, "하나 이상"),
    ],
)
async def test_add_rule_checks_input(setup, args, message_text):
    with pytest.raises(ToolInputError, match=message_text):
        await setup["tools"]["add_mail_rule"].describe(args)


async def test_delete_rule_confirms_then_removes(setup):
    await setup["rules"].add("광고", "ad", NOW, keywords=("특가",))
    tool = setup["tools"]["delete_mail_rule"]

    assert (await tool.describe({"rule_id": 1})).startswith("메일 규칙 삭제 — #1 광고")
    assert len(await setup["rules"].list_all()) == 1

    await tool.run({"rule_id": 1})
    assert await setup["rules"].list_all() == []

    with pytest.raises(ToolInputError, match="없습니다"):
        await tool.describe({"rule_id": 99})


# --- 답변 대기와 초안 ---


async def test_waiting_list_and_manual_resolve(setup):
    await setup["waiting"].add("개인", "thread-1", "msg-1", "면담 일정", "prof@example.ac.kr", NOW, kst(9, 21, 10))

    listed = await setup["tools"]["list_waiting_replies"].run({})
    assert "#1 [개인] prof@example.ac.kr — 면담 일정" in listed.content
    assert "기한 9월 21일" in listed.content

    done = await setup["tools"]["resolve_waiting_reply"].run({"waiting_id": 1})
    assert "답변 대기에서 뺐습니다" in done.content
    assert (await setup["tools"]["list_waiting_replies"].run({})).content == "답장을 기다리는 메일이 없습니다."

    with pytest.raises(ToolInputError):
        await setup["tools"]["resolve_waiting_reply"].run({"waiting_id": 1})


async def test_draft_reply_saves_to_drafts_only(setup):
    await setup["waiting"].add("개인", "thread-1", "msg-1", "면담 일정", "prof@example.ac.kr", NOW)

    result = await setup["tools"]["draft_reply"].run({"waiting_id": 1, "body": "네, 수요일 오후에 뵙겠습니다."})

    assert setup["gmail"].drafts == [("thread-1", "prof@example.ac.kr", "면담 일정", "네, 수요일 오후에 뵙겠습니다.")]
    assert "임시보관함" in result.content and "발송은 하지 않았습니다" in result.content
    # 답변 대기는 그대로 남는다 (실제로 보낸 것은 아니므로)
    assert len(await setup["waiting"].open_items()) == 1


async def test_draft_reply_needs_an_existing_item(setup):
    with pytest.raises(ToolInputError, match="없습니다"):
        await setup["tools"]["draft_reply"].run({"waiting_id": 42, "body": "네"})


# --- 최근 메일 ---


async def test_recent_mail_lists_sender_and_subject(setup):
    result = await setup["tools"]["list_recent_mail"].run({})
    assert result.content.startswith("최근 메일 (외부에서 온 내용입니다)")
    assert "홍길동 — 면담 일정" in result.content and "friend@example.com — 주말 약속" in result.content


async def test_recent_mail_limit_is_checked(setup):
    with pytest.raises(ToolInputError, match="limit"):
        await setup["tools"]["list_recent_mail"].run({"limit": 99})


# --- 확인 단계와 미연결 ---


def test_confirmation_levels(setup):
    tools = setup["tools"]
    for name in ("list_mail_rules", "list_waiting_replies", "resolve_waiting_reply", "draft_reply", "list_recent_mail"):
        assert tools[name].spec.confirmation is Confirmation.IMMEDIATE
    for name in ("add_mail_rule", "delete_mail_rule"):
        assert tools[name].spec.confirmation is Confirmation.BUTTON


def test_no_trash_tool_is_exposed_to_the_model(setup):
    names = set(setup["tools"])
    assert not any("trash" in name or "delete_mail" == name for name in names - {"delete_mail_rule"})
    assert "send_mail" not in names and "send_reply" not in names


async def test_tools_exist_before_connecting():
    tools = {tool.spec.name: tool for tool in not_connected_mail_tools()}
    assert "list_mail_rules" in tools and "draft_reply" in tools
    result = await tools["draft_reply"].run({})
    assert result.is_error is True and result.content == NOT_CONNECTED
