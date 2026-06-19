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

    class Config:
        env_file = ".env"

settings = Settings()
