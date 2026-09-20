"""eClass 범위 도구 확인. "게시판은 알리지 마" 같은 말을 받아 수준을 바꾼다."""

import pytest

from app.collectors.eclass.scope import Decided, ScopeEntry, ScopeStore
from app.core.config import Level
from app.core.interfaces import Confirmation
from app.tools.common import ToolInputError
from app.tools.eclass import NO_SCOPE, eclass_tools


def entry(path: str, name: str, level: Level, **overrides) -> ScopeEntry:
    return ScopeEntry(path=path, name=name, level=level, **overrides)


@pytest.fixture
def store(tmp_path):
    store = ScopeStore(tmp_path / "eclass_scope.json")
    store.write(
        {
            "/ilos/st/course/notice_list.acl": entry("/ilos/st/course/notice_list.acl", "공지사항", Level.NOTIFY, per_course=True),
            "/ilos/st/course/material_list_form.acl": entry("/ilos/st/course/material_list_form.acl", "열린게시판", Level.STORE),
            "/ilos/guide/guide_main_form.acl": entry("/ilos/guide/guide_main_form.acl", "FAQ", Level.OFF),
        }
    )
    return store


@pytest.fixture
def tools(store):
    return {tool.spec.name: tool for tool in eclass_tools(store)}


async def test_the_list_shows_what_is_collected_and_how(tools):
    result = await tools["eclass_scope"].run({})
    assert "공지사항 [알림]" in result.content and "과목별" in result.content
    assert "열린게시판 [저장만]" in result.content
    # 꺼 둔 화면은 줄줄이 늘어놓지 않고 개수만
    assert "FAQ" not in result.content and "꺼 둔 화면 1개" in result.content


async def test_changing_a_level_needs_a_button(tools):
    assert tools["eclass_scope_set"].spec.confirmation is Confirmation.BUTTON


async def test_the_confirmation_says_what_changes(tools):
    text = await tools["eclass_scope_set"].describe({"screen": "공지사항", "level": "brief"})
    assert "공지사항" in text and "알림" in text and "브리핑" in text


async def test_a_screen_is_found_by_its_menu_name(tools, store):
    result = await tools["eclass_scope_set"].run({"screen": "열린게시판", "level": "off"})

    assert "열린게시판" in result.content
    saved = store.read()["/ilos/st/course/material_list_form.acl"]
    assert saved.level is Level.OFF and saved.by is Decided.USER


async def test_a_screen_is_found_by_its_path(tools, store):
    await tools["eclass_scope_set"].run({"screen": "/ilos/guide/guide_main_form.acl", "level": "store"})
    assert store.read()["/ilos/guide/guide_main_form.acl"].level is Level.STORE


async def test_an_unknown_screen_is_reported_not_guessed(tools):
    with pytest.raises(ToolInputError, match="찾지 못했습니다"):
        await tools["eclass_scope_set"].run({"screen": "없는화면", "level": "off"})


async def test_an_ambiguous_name_asks_again(tools):
    with pytest.raises(ToolInputError, match="여럿입니다"):
        await tools["eclass_scope_set"].run({"screen": "list", "level": "off"})


async def test_an_unknown_level_is_refused(tools):
    with pytest.raises(ToolInputError, match="수준은"):
        await tools["eclass_scope_set"].run({"screen": "공지사항", "level": "가끔"})


async def test_without_a_catalog_the_user_is_told_what_to_do(tmp_path):
    tools = {tool.spec.name: tool for tool in eclass_tools(ScopeStore(tmp_path / "없음.json"))}
    result = await tools["eclass_scope"].run({})
    assert result.content == NO_SCOPE

    with pytest.raises(ToolInputError, match="화면 목록"):
        await tools["eclass_scope_set"].run({"screen": "공지사항", "level": "off"})
