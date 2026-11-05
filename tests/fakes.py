from app.integrations.telegram_gateway import BotIdentity, MemberStatus


class FakeTelegram:
    def __init__(self, members=None, admins=None, fail_with=None):
        self.members: dict[int, MemberStatus] = members or {}
        self.admins: set[int] = admins or set()
        self.fail_with: Exception | None = fail_with
        self.sent: list[dict] = []

    async def get_me(self) -> BotIdentity:
        return BotIdentity(1, "test_bot")

    async def get_member_status(self, chat_id: int, user_id: int) -> MemberStatus:
        return self.members.get(user_id, MemberStatus.NOT_MEMBER)

    async def get_admin_ids(self, chat_id: int) -> set[int]:
        return set(self.admins)

    async def send_message(self, chat_id, text, thread_id=None, buttons=None) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(
            {"chat_id": chat_id, "text": text, "thread_id": thread_id, "buttons": buttons}
        )
