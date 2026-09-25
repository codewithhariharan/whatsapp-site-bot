from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    ANTHROPIC_API_KEY: str

    # Baileys bridge (WhatsApp Web transport). This is the only transport —
    # the bot serves groups, and the Cloud API cannot do groups — so both of
    # these are required. Without them there is no way to receive a message or
    # send a reply, and failing at startup beats discovering it at runtime.
    BAILEYS_BRIDGE_URL: str           # e.g. http://bridge:8088
    BRIDGE_SHARED_SECRET: str         # must match the bridge's BRIDGE_SHARED_SECRET

    # Groups whose messages are tunnel updates rather than site work, as a
    # comma-separated list of JIDs. These route to tunnel_updates and their
    # /ask sees only that table; every other group keeps the existing
    # daily_logs / dwall_panels behaviour.
    #
    # By JID, never by name: several groups on this account share a name
    # prefix, so a name match is genuinely ambiguous here. Empty means no
    # group is a tunnel group — this fails CLOSED, unlike the bridge
    # allowlist, because the wrong answer is to start writing site logs into
    # the tunnel table.
    TUNNEL_GROUP_IDS: str = ""

    @property
    def tunnel_group_ids(self) -> set[str]:
        return {g.strip() for g in self.TUNNEL_GROUP_IDS.split(",") if g.strip()}

    class Config:
        env_file = ".env"
        # The .env still carries the retired Cloud API keys (WHATSAPP_TOKEN,
        # APP_SECRET, WEBHOOK_VERIFY_TOKEN and friends). pydantic-settings
        # treats unknown dotenv keys as an error by default, so ignore them
        # rather than requiring the file to be rewritten in lockstep.
        extra = "ignore"

settings = Settings()
