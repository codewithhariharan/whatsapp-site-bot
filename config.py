from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    WHATSAPP_TOKEN: str
    WHATSAPP_PHONE_NUMBER_ID: str
    WHATSAPP_BUSINESS_ACCOUNT_ID: str
    WEBHOOK_VERIFY_TOKEN: str
    APP_SECRET: str
    SUPABASE_URL: str
    SUPABASE_KEY: str
    ANTHROPIC_API_KEY: str

    # Baileys bridge (unofficial WhatsApp Web service for group messaging).
    # Leave blank to run Cloud-API-only with no group support.
    BAILEYS_BRIDGE_URL: str = ""      # e.g. https://your-bridge.up.railway.app
    BRIDGE_SHARED_SECRET: str = ""    # must match the bridge's BRIDGE_SHARED_SECRET

    # The only group the bot serves. The bridge already filters on this, so this
    # is the second half of the same gate: it keeps a misconfigured or older
    # bridge from writing another group's messages into the database.
    # Blank = accept every group the bridge forwards.
    ALLOWED_GROUP_NAME: str = "CR106 LTA PJT (Site Work)"

    class Config:
        env_file = ".env"

settings = Settings()
