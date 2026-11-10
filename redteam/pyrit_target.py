"""PyRIT 1.1 target for independent report requests (not persistent chat)."""

import asyncio

from pyrit.models import construct_response_from_request
from pyrit.prompt_target import PromptTarget


class UnderwritingTarget(PromptTarget):
    def __init__(self, *, client):
        super().__init__(endpoint=client.settings.base_url, model_name="underwriting-application")
        self.client = client

    async def _send_prompt_to_target_async(self, *, normalized_conversation):
        piece = normalized_conversation[-1].get_piece()
        text = await asyncio.to_thread(self.client.generate, piece.converted_value)
        return [construct_response_from_request(request=piece, response_text_pieces=[text])]
