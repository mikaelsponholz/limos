"""List available Gemini model IDs: python -m scripts.list_models."""

from google import genai

from gateway.settings import Settings


def main() -> int:
    settings = Settings()
    if not settings.gemini_api_key.strip():
        print("Set GEMINI_API_KEY in the environment before listing models.")
        return 1
    try:
        with genai.Client(api_key=settings.gemini_api_key) as client:
            models = sorted(
                (model for model in client.models.list() if model.name),
                key=lambda model: model.name or "",
            )
            for model in models:
                actions = ", ".join(sorted(model.supported_actions or []))
                print(f"{model.name} | {actions}" if actions else model.name)
    except Exception:
        # SDK exception text can include request details. Never print it.
        print("Unable to list Gemini models. Check the key and API connectivity.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
