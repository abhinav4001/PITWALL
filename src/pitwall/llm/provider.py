"""LLM gateway provider.

The gateway (OmniRoute) exposes an OpenAI-compatible API.  The provider is
used for the slow path only: the pit wall always has a deterministic
fast path built from :class:`~pitwall.data.models.StrategyResult`, so the
app works when no credential is configured.
"""

from openai import OpenAI

from pitwall.config import get_settings
from pitwall.llm.schemas import Insight


class LLMGatewayError(RuntimeError):
    """Raised when the gateway cannot produce a usable structured answer."""


class LLMGatewayProvider:
    def __init__(self) -> None:
        settings = get_settings()
        self.client = OpenAI(
            base_url=str(settings.llm_base_url),
            api_key=settings.llm_api_key.get_secret_value() or "not-needed",
            timeout=30.0,
            max_retries=1,
        )
        self.insight_model = settings.insight_model or "gpt-4o"

    def generate_insight(self, prompt: str) -> Insight:
        """Ask the gateway for a structured insight.

        Raises :class:`LLMGatewayError` when the model refuses or returns
        something that is not a valid :class:`Insight`; the caller then falls
        back to the deterministic template.
        """
        response = self.client.beta.chat.completions.parse(
            model=self.insight_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are PitWall, an expert F1 strategy AI. "
                        "Never perform arithmetic; only use provided numbers."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format=Insight,
        )
        insight = response.choices[0].message.parsed
        if insight is None:
            raise LLMGatewayError(
                f"model {self.insight_model} returned no parsable insight"
            )
        return insight
