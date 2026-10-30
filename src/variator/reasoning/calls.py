"""The single place where the reasoning layer talks to Token Factory."""

from __future__ import annotations

from pydantic import BaseModel

from variator.llm import TokenFactoryClient, recommended_sampling, user_message

SEPARATOR = "\n\n---\n\n"

# Token Factory defaults max_tokens to 8192 when omitted
# (https://docs.tokenfactory.nebius.com/api-reference/inference/create-chat-completion.md).
# ASSUMPTION: reasoning tokens count against max_tokens, so a thinking trace plus a full
# DecisionModel JSON could be truncated at 8192; the higher cap is accepted by the gateway.
MAX_TOKENS_REASONING = 32_768
MAX_TOKENS_PLAIN = 8_192


def ask_structured[T: BaseModel](
    client: TokenFactoryClient,
    llm: str,
    instructions: str,
    request: str,
    schema: type[T],
    *,
    reasoning: bool,
    max_attempts: int,
) -> T:
    """One structured call: static instructions first, then the dynamic request.

    Sampling follows NVIDIA's Nemotron 3 recommendation (1.0 with reasoning,
    0 without); ``structured()`` exposes no top_p, so only temperature is set.
    """
    sampling = recommended_sampling(reasoning=reasoning)
    return client.structured(
        llm,
        [user_message(instructions + SEPARATOR + request)],
        schema,
        reasoning=reasoning,
        max_attempts=max_attempts,
        temperature=sampling["temperature"],
        max_tokens=MAX_TOKENS_REASONING if reasoning else MAX_TOKENS_PLAIN,
    )
