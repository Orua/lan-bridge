"""Small, testable protocol adapters used by compatibility routes."""

from .chat_to_responses import (
    ChatToResponsesConversionError,
    convert_chat_request_to_responses,
)
from .responses_to_chat import (
    ResponsesToChatConversionError,
    convert_responses_response_to_chat,
)
from .responses_stream_to_chat import ResponsesStreamToChat

__all__ = [
    "ChatToResponsesConversionError",
    "ResponsesToChatConversionError",
    "ResponsesStreamToChat",
    "convert_chat_request_to_responses",
    "convert_responses_response_to_chat",
]
