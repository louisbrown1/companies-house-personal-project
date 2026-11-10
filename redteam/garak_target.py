"""garak function.Single adapter. Configured explicitly by the suite runner."""

_client = None


def configure(client):
    global _client
    _client = client


def generate(prompt: str, **kwargs) -> list[str]:
    if _client is None:
        raise RuntimeError("Configure the application client before running garak")
    return [_client.generate(prompt)]
