from datetime import datetime, timezone, timedelta

from llm.prompt import build_system_prompt

MSK = timezone(timedelta(hours=3))  # Europe/Minsk без DST
NOW = datetime(2026, 5, 21, 14, 30, tzinfo=MSK)


def test_system_prompt_contains_role():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "маршрут" in prompt.lower()
    assert "только по теме" in prompt.lower()


def test_system_prompt_contains_today_date_and_time():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "2026-05-21" in prompt
    assert "14:30" in prompt


def test_system_prompt_includes_timezone_label():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "Minsk" in prompt or "Минск" in prompt


def test_system_prompt_with_user_name():
    prompt = build_system_prompt(now=NOW, user_name="Маша")
    assert "Маша" in prompt
    assert "имя" in prompt.lower() or "собеседник" in prompt.lower()


def test_system_prompt_without_user_name_does_not_mention_unknown():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "Имя собеседника" not in prompt


def test_system_prompt_lists_providers():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "mogilevminsk" in prompt
    assert "avto_slava" in prompt
    assert "buspro" in prompt
    assert "magnitplus" in prompt
    assert "atlasbus" in prompt


def test_system_prompt_lists_directions():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "mg_mnsk" in prompt
    assert "mnsk_mg" in prompt
    assert "mg_bobr" in prompt
    assert "bobr_mnsk" in prompt
    assert "Могилёв" in prompt
    assert "Минск" in prompt
    assert "Бобруйск" in prompt


def test_system_prompt_instructs_to_use_ask_user():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "ask_user" in prompt


def test_system_prompt_includes_telegram_markdown_rules():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "parse_mode=Markdown" in prompt
    assert "`avto_slava`" in prompt
    assert "без разметки" in prompt


def test_system_prompt_includes_status_proxy_and_callback_rules():
    prompt = build_system_prompt(now=NOW, user_name=None)
    assert "execution" in prompt
    assert "get_atlas_proxy_status" in prompt
    assert "set_atlas_proxy_target" in prompt
    assert "schedule_self_callback" in prompt
