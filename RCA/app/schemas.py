from pydantic import BaseModel


class ClickStackAlertPayload(BaseModel):
    """Body ClickStack sends. Its webhook template only exposes {{title}}, {{body}}, {{link}} —
    no structured metric/segment fields — so that's all this model can carry for now."""

    title: str
    body: str
    link: str | None = None
