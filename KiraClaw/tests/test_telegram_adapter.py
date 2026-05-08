import asyncio
from kiraclaw_agentd.observer_service import InflightMessageContext
from kiraclaw_agentd.engine import RunResult
from kiraclaw_agentd.session_manager import RunRecord
from kiraclaw_agentd.settings import KiraClawSettings
from kiraclaw_agentd.telegram_adapter import (
    TelegramGateway,
    _build_delivery_context_prefix,
    _build_message_prompt,
    _clean_prompt_text,
    _display_name,
    _is_authorized_user_name,
    _is_human_message,
    _matchable_name,
    _reply_to_message_id,
    _session_id_from_message,
)


def test_display_name_prefers_username() -> None:
    assert _display_name({"username": "jiho"}) == "@jiho"
    assert _display_name({"first_name": "Jiho", "last_name": "Jeon"}) == "Jiho Jeon"


def test_matchable_name_includes_username_and_full_name() -> None:
    assert _matchable_name({"username": "jiho", "first_name": "Jiho", "last_name": "Jeon"}) == "@jiho Jiho Jeon"
    assert _matchable_name({"first_name": "전", "last_name": "지호"}) == "전 지호"


def test_telegram_prompt_cleanup_strips_bot_mention() -> None:
    assert _clean_prompt_text("  @kira_bot   check   this  ", "kira_bot", mention=True, agent_name="세나") == "세나 check this"
    assert _clean_prompt_text("  hello   there ", "kira_bot", mention=False) == "hello there"


def test_telegram_build_message_prompt_includes_replied_message_context() -> None:
    prompt = _build_message_prompt(
        {
            "chat": {"id": -1, "type": "group"},
            "from": {"id": 10, "is_bot": False},
            "text": "@kira_bot 합산해줘",
            "reply_to_message": {
                "chat": {"id": -1, "type": "group"},
                "from": {"id": 11, "is_bot": False},
                "text": "이전 금액은 1000원",
            },
        },
        "kira_bot",
        mention=True,
        agent_name="세나",
    )

    assert "Replied-to Telegram message:" in prompt
    assert "이전 금액은 1000원" in prompt
    assert "세나 합산해줘" in prompt


def test_is_human_message_filters_bot_messages() -> None:
    private = {
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 10, "is_bot": False},
        "text": "hello",
    }
    group = {
        "chat": {"id": -1, "type": "group"},
        "from": {"id": 10, "is_bot": False},
        "text": "@kira_bot hello",
    }
    bot_message = {
        "chat": {"id": -1, "type": "group"},
        "from": {"id": 10, "is_bot": True},
        "text": "hello",
    }

    assert _is_human_message(private) is True
    assert _is_human_message(group) is True
    assert _is_human_message(
        {
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 10, "is_bot": False},
            "document": {"file_id": "doc-1", "file_name": "report.pdf", "mime_type": "application/pdf"},
        }
    ) is True
    assert _is_human_message(bot_message) is False


def test_telegram_message_sessions_and_reply_targets() -> None:
    private = {"chat": {"id": 1, "type": "private"}, "message_id": 77}
    group = {
        "chat": {"id": -2, "type": "group"},
        "message_id": 88,
        "reply_to_message": {"message_id": 10},
    }

    assert _session_id_from_message(private) == "telegram:dm:1"
    assert _reply_to_message_id(private) is None
    assert _session_id_from_message(group) == "telegram:-2:10"
    assert _reply_to_message_id(group) == 88


def test_telegram_authorized_names_use_substring_match() -> None:
    assert _is_authorized_user_name("@jiho", "jiho, kris") is True
    assert _is_authorized_user_name("전지호", "jiho, 전지호") is True
    assert _is_authorized_user_name("전 지호", "jiho, 전지호") is True
    assert _is_authorized_user_name("someone else", "jiho, 전지호") is False


def test_telegram_delivery_context_prefix_includes_chat_and_reply() -> None:
    context = _build_delivery_context_prefix(12345, 99)
    assert "chat_id: 12345" in context
    assert "reply_to_message_id: 99" in context


class _FakeSessionManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, **kwargs) -> RunRecord:
        self.calls.append(kwargs)
        return RunRecord(
            run_id="run-1",
            session_id=kwargs["session_id"],
            state="completed",
            prompt=kwargs["prompt"],
            created_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            result=RunResult(final_response="internal telegram ok", streamed_text="telegram ok", spoken_messages=["telegram ok"]),
            metadata=kwargs.get("metadata", {}),
        )


class _FakeObserverService:
    def __init__(self) -> None:
        self.last_inbound = None

    def classify_inflight(self, prompt: str, snapshot: dict, inbound=None) -> object:
        from kiraclaw_agentd.observer_service import ObserverDecision

        self.last_inbound = inbound
        if "어디까지" in prompt:
            return ObserverDecision("status_query", "지금 상태를 확인 중입니다.")
        return ObserverDecision("queue_next", "끝난 뒤 이어서 처리할게요.")

    def summarize_heartbeat(self, snapshot: dict) -> str:
        return "아직 작업 중입니다."


def test_telegram_run_for_message_uses_session_manager_and_publish(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
        )
        session_manager = _FakeSessionManager()
        gateway = TelegramGateway(session_manager, settings)
        sent: list[dict] = []

        async def fake_send(chat_id, text, reply_to_message_id=None):
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                }
            )

        gateway.send_message = fake_send  # type: ignore[method-assign]

        message = {
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 10, "username": "jiho"},
            "message_id": 50,
            "text": "hello",
        }

        await gateway._run_for_message(
            message=message,
            session_id="telegram:dm:123",
            chat_id=123,
            reply_to_message_id=None,
            prompt="hello",
            inbound=InflightMessageContext(source="telegram-dm", mention=False, is_private=True, user_name="@jiho"),
        )

        assert session_manager.calls[0]["metadata"]["source"] == "telegram-dm"
        assert session_manager.calls[0]["metadata"]["mention"] is False
        assert session_manager.calls[0]["metadata"]["is_private"] is True
        assert "chat_id: 123" in session_manager.calls[0]["context_prefix"]
        assert sent == [{"chat_id": 123, "text": "telegram ok", "reply_to_message_id": None}]

    asyncio.run(scenario())


def test_telegram_inflight_status_query_is_answered_without_queueing(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
        )
        session_manager = _FakeSessionManager()
        session_manager.has_active_run = lambda session_id: True  # type: ignore[attr-defined]
        session_manager.build_observer_snapshot = lambda session_id: {  # type: ignore[attr-defined]
            "session_id": session_id,
            "source": "telegram-group",
            "state": "running",
            "prompt": "작업 중",
            "elapsed_seconds": 5,
            "recent_tool_events": [],
            "active_processes": [],
            "run_mention": True,
            "run_is_private": False,
        }
        observer = _FakeObserverService()
        gateway = TelegramGateway(session_manager, settings, observer_service=observer)
        sent: list[dict] = []

        async def fake_send(chat_id, text, reply_to_message_id=None):
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                }
            )

        gateway.send_message = fake_send  # type: ignore[method-assign]

        handled = await gateway._maybe_handle_inflight_message(
            session_id="telegram:-1:main",
            prompt="지금 어디까지 했어?",
            chat_id=-1,
            reply_to_message_id=44,
            inbound=InflightMessageContext(source="telegram-group", mention=True, is_private=False, user_name="@jiho"),
        )

        assert handled is True
        assert session_manager.calls == []
        assert observer.last_inbound is not None
        assert observer.last_inbound.mention is True
        assert observer.last_inbound.is_private is False
        assert sent == [{"chat_id": -1, "text": "지금 상태를 확인 중입니다.", "reply_to_message_id": 44}]

    asyncio.run(scenario())


def test_telegram_inflight_group_queue_next_is_silent_and_still_queues(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
        )
        session_manager = _FakeSessionManager()
        session_manager.has_active_run = lambda session_id: True  # type: ignore[attr-defined]
        session_manager.build_observer_snapshot = lambda session_id: {  # type: ignore[attr-defined]
            "session_id": session_id,
            "source": "telegram-group",
            "state": "running",
            "prompt": "작업 중",
            "elapsed_seconds": 5,
            "recent_tool_events": [],
            "active_processes": [],
            "run_mention": False,
            "run_is_private": False,
        }

        class _QueueOnlyObserver:
            def classify_inflight(self, prompt: str, snapshot: dict, inbound=None) -> object:
                from kiraclaw_agentd.observer_service import ObserverDecision

                return ObserverDecision("queue_next", "")

        gateway = TelegramGateway(session_manager, settings, observer_service=_QueueOnlyObserver())
        sent: list[dict] = []

        async def fake_send(chat_id, text, reply_to_message_id=None):
            sent.append({"chat_id": chat_id, "text": text, "reply_to_message_id": reply_to_message_id})

        gateway.send_message = fake_send  # type: ignore[method-assign]

        handled = await gateway._maybe_handle_inflight_message(
            session_id="telegram:-1:main",
            prompt="그리고 이것도 봐줘",
            chat_id=-1,
            reply_to_message_id=77,
            inbound=InflightMessageContext(
                source="telegram-group",
                mention=False,
                is_private=False,
                user_name="@jiho",
            ),
        )

        assert handled is False
        assert sent == []

    asyncio.run(scenario())


def test_telegram_group_debounce_is_longer_than_private(tmp_path) -> None:
    settings = KiraClawSettings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        home_mode="modern",
        slack_enabled=False,
        telegram_enabled=False,
    )
    gateway = TelegramGateway(_FakeSessionManager(), settings)

    assert gateway._debounce_seconds_for_message({"chat": {"type": "private"}}) == 5.0
    assert gateway._debounce_seconds_for_message({"chat": {"type": "group"}}) == 8.0


def test_telegram_poll_loop_recovers_after_transient_error(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
        )
        gateway = TelegramGateway(_FakeSessionManager(), settings)
        calls = {"count": 0}

        async def fake_api(method, payload=None):
            await asyncio.sleep(0)
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary polling error")
            return {"ok": True, "result": []}

        gateway._api = fake_api  # type: ignore[method-assign]

        task = asyncio.create_task(gateway._poll_loop())
        await asyncio.sleep(2.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert calls["count"] >= 2
        assert gateway.state == "running"
        assert gateway.last_error is None

    asyncio.run(scenario())


def test_telegram_messages_from_same_user_are_debounced_and_merged(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
        )
        session_manager = _FakeSessionManager()
        gateway = TelegramGateway(session_manager, settings, debounce_seconds=0.05)
        sent: list[dict] = []

        async def fake_send(chat_id, text, reply_to_message_id=None):
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                }
            )

        gateway.send_message = fake_send  # type: ignore[method-assign]
        gateway.identity = {"id": 999, "username": "jiho_kira_bot", "first_name": "지호봇"}

        message1 = {
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 10, "username": "batteryho", "first_name": "지호", "last_name": "전", "is_bot": False},
            "message_id": 50,
            "text": "first",
        }
        message2 = {
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 10, "username": "batteryho", "first_name": "지호", "last_name": "전", "is_bot": False},
            "message_id": 51,
            "text": "second",
        }

        await gateway._handle_message(message1)
        await gateway._handle_message(message2)
        await asyncio.sleep(0.12)

        assert len(session_manager.calls) == 1
        assert session_manager.calls[0]["prompt"] == "first\nsecond"
        assert sent == [{"chat_id": 123, "text": "telegram ok", "reply_to_message_id": None}]

    asyncio.run(scenario())


def test_telegram_document_message_without_text_is_processed(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
        )
        session_manager = _FakeSessionManager()
        gateway = TelegramGateway(session_manager, settings, debounce_seconds=0.05)
        sent: list[dict] = []

        async def fake_send(chat_id, text, reply_to_message_id=None):
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                }
            )

        gateway.send_message = fake_send  # type: ignore[method-assign]
        gateway.identity = {"id": 999, "username": "jiho_kira_bot", "first_name": "지호봇"}

        message = {
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 10, "username": "batteryho", "first_name": "지호", "last_name": "전", "is_bot": False},
            "message_id": 50,
            "document": {
                "file_id": "doc-1",
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
                "file_size": 2048,
            },
        }

        await gateway._handle_message(message)
        await asyncio.sleep(0.12)

        assert len(session_manager.calls) == 1
        prompt = session_manager.calls[0]["prompt"]
        assert "Attached Telegram files:" in prompt
        assert "report.pdf (document, application/pdf, size_bytes=2048)" in prompt
        assert "Use telegram_download_file" in prompt
        assert "file_id: doc-1" in prompt
        assert sent == [{"chat_id": 123, "text": "telegram ok", "reply_to_message_id": None}]

    asyncio.run(scenario())


def test_telegram_group_messages_are_handled_as_room_transcript(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
            agent_name="세나",
        )

        class _SilentGroupSessionManager(_FakeSessionManager):
            async def run(self, **kwargs) -> RunRecord:
                self.calls.append(kwargs)
                return RunRecord(
                    run_id="run-1",
                    session_id=kwargs["session_id"],
                    state="completed",
                    prompt=kwargs["prompt"],
                    created_at="2026-01-01T00:00:00Z",
                    finished_at="2026-01-01T00:00:01Z",
                    result=RunResult(final_response="internal only", streamed_text=""),
                    metadata=kwargs.get("metadata", {}),
                )

        session_manager = _SilentGroupSessionManager()
        gateway = TelegramGateway(session_manager, settings, debounce_seconds=0.05)
        sent: list[dict] = []

        async def fake_send(chat_id, text, reply_to_message_id=None):
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                }
            )

        gateway.send_message = fake_send  # type: ignore[method-assign]
        gateway.identity = {"id": 999, "username": "jiho_kira_bot", "first_name": "지호봇"}

        message = {
            "chat": {"id": -100, "type": "group"},
            "from": {"id": 10, "username": "batteryho", "first_name": "지호", "last_name": "전", "is_bot": False},
            "message_id": 52,
            "text": "상태 알려줘",
        }

        await gateway._handle_message(message)
        await asyncio.sleep(0.12)

        assert len(session_manager.calls) == 1
        assert session_manager.calls[0]["prompt"] == "Recent room messages:\n- @batteryho: 상태 알려줘"
        assert sent == []

    asyncio.run(scenario())


def test_telegram_group_messages_share_one_room_debounce_window(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
            agent_name="세나",
        )
        class _SilentGroupSessionManager(_FakeSessionManager):
            async def run(self, **kwargs) -> RunRecord:
                self.calls.append(kwargs)
                return RunRecord(
                    run_id="run-1",
                    session_id=kwargs["session_id"],
                    state="completed",
                    prompt=kwargs["prompt"],
                    created_at="2026-01-01T00:00:00Z",
                    finished_at="2026-01-01T00:00:01Z",
                    result=RunResult(final_response="internal only", streamed_text=""),
                    metadata=kwargs.get("metadata", {}),
                )

        session_manager = _SilentGroupSessionManager()
        gateway = TelegramGateway(session_manager, settings, debounce_seconds=0.05)
        sent: list[dict] = []

        async def fake_send(chat_id, text, reply_to_message_id=None):
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                }
            )

        gateway.send_message = fake_send  # type: ignore[method-assign]
        gateway.identity = {"id": 999, "username": "jiho_kira_bot", "first_name": "지호봇"}

        message1 = {
            "chat": {"id": -100, "type": "group"},
            "from": {"id": 10, "username": "batteryho", "first_name": "지호", "last_name": "전", "is_bot": False},
            "message_id": 52,
            "text": "첫번째",
        }
        message2 = {
            "chat": {"id": -100, "type": "group"},
            "from": {"id": 11, "username": "alice", "first_name": "Alice", "is_bot": False},
            "message_id": 53,
            "text": "두번째",
        }

        await gateway._handle_message(message1)
        await gateway._handle_message(message2)
        await asyncio.sleep(0.12)

        assert len(session_manager.calls) == 1
        assert session_manager.calls[0]["prompt"] == (
            "Recent room messages:\n"
            "- @batteryho: 첫번째\n"
            "- @alice: 두번째"
        )
        assert sent == []

    asyncio.run(scenario())


def test_telegram_publish_result_prefers_spoken_messages(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
        )
        session_manager = _FakeSessionManager()
        gateway = TelegramGateway(session_manager, settings)
        sent: list[dict] = []

        async def fake_send(chat_id, text, reply_to_message_id=None):
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                }
            )

        gateway.send_message = fake_send  # type: ignore[method-assign]
        record = RunRecord(
            run_id="run-1",
            session_id="telegram:-1:main",
            state="completed",
            prompt="hello",
            created_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            result=RunResult(
                final_response="internal summary",
                streamed_text="",
                spoken_messages=["첫번째 말", "두번째 말"],
            ),
        )

        await gateway._publish_result(-1, 77, record)

        assert sent == [
            {"chat_id": -1, "text": "첫번째 말", "reply_to_message_id": 77},
            {"chat_id": -1, "text": "두번째 말", "reply_to_message_id": 77},
        ]

    asyncio.run(scenario())


def test_telegram_publish_result_appends_tool_summary_to_last_spoken_message(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
            response_trace_enabled=True,
        )
        session_manager = _FakeSessionManager()
        gateway = TelegramGateway(session_manager, settings)
        sent: list[dict] = []

        async def fake_send(chat_id, text, reply_to_message_id=None):
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                }
            )

        gateway.send_message = fake_send  # type: ignore[method-assign]
        record = RunRecord(
            run_id="run-1",
            session_id="telegram:-1:main",
            state="completed",
            prompt="hello",
            created_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            result=RunResult(
                final_response="internal summary",
                streamed_text="",
                tool_events=[
                    {"phase": "start", "name": "read", "args": {}},
                    {"phase": "end", "name": "read", "result": "ok"},
                    {"phase": "start", "name": "bash", "args": {}},
                    {"phase": "end", "name": "bash", "result": "ok"},
                ],
                spoken_messages=["첫번째 말", "두번째 말"],
            ),
        )

        await gateway._publish_result(-1, 77, record)

        assert sent == [
            {"chat_id": -1, "text": "첫번째 말", "reply_to_message_id": 77},
            {"chat_id": -1, "text": "두번째 말\n\nUsed: read, bash\nElapsed: 1.0s", "reply_to_message_id": 77},
        ]

    asyncio.run(scenario())


def test_telegram_group_publish_is_silent_without_speak(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
        )
        session_manager = _FakeSessionManager()
        gateway = TelegramGateway(session_manager, settings)
        sent: list[dict] = []

        async def fake_send(chat_id, text, reply_to_message_id=None):
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                }
            )

        gateway.send_message = fake_send  # type: ignore[method-assign]
        record = RunRecord(
            run_id="run-1",
            session_id="telegram:-1:main",
            state="completed",
            prompt="hello",
            created_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            result=RunResult(final_response="internal summary", streamed_text=""),
            metadata={"source": "telegram-group"},
        )

        await gateway._publish_result(-1, 77, record)

        assert sent == []

    asyncio.run(scenario())


def test_telegram_dm_publish_is_silent_without_speak(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
        )
        session_manager = _FakeSessionManager()
        gateway = TelegramGateway(session_manager, settings)
        sent: list[dict] = []

        async def fake_send(chat_id, text, reply_to_message_id=None):
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                }
            )

        gateway.send_message = fake_send  # type: ignore[method-assign]
        record = RunRecord(
            run_id="run-1",
            session_id="telegram:dm:123",
            state="completed",
            prompt="hello",
            created_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            result=RunResult(final_response="internal summary", streamed_text=""),
            metadata={"source": "telegram-dm"},
        )

        await gateway._publish_result(123, None, record)

        assert sent == []

    asyncio.run(scenario())


def test_telegram_dm_publish_uses_terminal_fallback_after_max_turns(tmp_path) -> None:
    async def scenario() -> None:
        settings = KiraClawSettings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            home_mode="modern",
            slack_enabled=False,
            telegram_enabled=False,
        )
        session_manager = _FakeSessionManager()
        gateway = TelegramGateway(session_manager, settings)
        sent: list[dict] = []

        async def fake_send(chat_id, text, reply_to_message_id=None):
            sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                }
            )

        gateway.send_message = fake_send  # type: ignore[method-assign]
        record = RunRecord(
            run_id="run-1",
            session_id="telegram:dm:123",
            state="completed",
            prompt="hello",
            created_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
            result=RunResult(final_response="최종 정리입니다.", streamed_text="", max_turns_reached=True),
            metadata={"source": "telegram-dm", "is_private": True, "mention": False},
        )

        await gateway._publish_result(123, None, record)

        assert sent == [{"chat_id": 123, "text": "최종 정리입니다.", "reply_to_message_id": None}]

    asyncio.run(scenario())
