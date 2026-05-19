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

    class Config:
        env_file = ".env"

settings = Settings()
