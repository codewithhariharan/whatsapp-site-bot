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

    class Config:
        env_file = ".env"
        # The .env still carries the retired Cloud API keys (WHATSAPP_TOKEN,
        # APP_SECRET, WEBHOOK_VERIFY_TOKEN and friends). pydantic-settings
        # treats unknown dotenv keys as an error by default, so ignore them
        # rather than requiring the file to be rewritten in lockstep.
        extra = "ignore"

settings = Settings()
