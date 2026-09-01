from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    WHATSAPP_TOKEN: str
    WHATSAPP_PHONE_NUMBER_ID: str
    WHATSAPP_BUSINESS_ACCOUNT_ID: str
    WEBHOOK_VERIFY_TOKEN: str
    APP_SECRET: str

    # ── Cloud SQL for PostgreSQL ──────────────────────────────────────────────
    # Connection goes through the Cloud SQL Python Connector, so there is no
    # host or port: the instance is addressed by its connection name and the
    # connector handles TLS and IP allowlisting itself.
    INSTANCE_CONNECTION_NAME: str          # project:region:instance
    DB_NAME: str = "sitebot"
    DB_USER: str = ""                      # IAM principal, or a built-in user
    DB_PASSWORD: str = ""                  # blank when DB_IAM_AUTH is true
    DB_IAM_AUTH: bool = True               # IAM database authentication
    DB_PRIVATE_IP: bool = False            # true if the VM reaches Cloud SQL over VPC

    # ── Claude via Vertex AI ──────────────────────────────────────────────────
    # Credentials come from Application Default Credentials (the VM's service
    # account), so there is no API key to store or rotate.
    VERTEX_PROJECT_ID: str
    # "global" avoids per-region model availability gaps. asia-southeast1 does
    # not serve every Claude model; check the Model Garden before pinning it.
    VERTEX_REGION: str = "global"
    PARSER_MODEL: str = "claude-haiku-4-5@20251001"
    ANSWER_MODEL: str = "claude-sonnet-4-6"

    # ── Baileys bridge ────────────────────────────────────────────────────────
    BAILEYS_BRIDGE_URL: str = ""
    BRIDGE_SHARED_SECRET: str = ""

    # Comma-separated group JIDs the bot will accept logs from. Enforced here
    # as well as in the bridge so a misconfigured or compromised bridge cannot
    # inject data from groups this deployment was never meant to see.
    ALLOWED_GROUP_IDS: str = ""

    @property
    def allowed_group_ids(self) -> set[str]:
        return {g.strip() for g in self.ALLOWED_GROUP_IDS.split(",") if g.strip()}

    class Config:
        env_file = ".env"


settings = Settings()
