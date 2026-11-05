"""Model imports must skip models that cannot chat (video, image, speech, embeddings)."""
import pytest

from axon.providers.catalog import is_chat_model_id


@pytest.mark.parametrize("model", ["veo-3.1-generate-001", "imagen-4", "sora-2", "whisper-large", "text-embedding-3"])
def test_non_chat_models_are_skipped(model):
    assert not is_chat_model_id(model)


@pytest.mark.parametrize("model", ["gpt-oss-120b", "claude-haiku-4-5", "deepseek-v4-pro", "gemini-3.8-flash"])
def test_chat_models_are_kept(model):
    assert is_chat_model_id(model)
