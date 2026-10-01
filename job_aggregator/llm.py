"""
Single shared Gemini client, lazily initialized so importing this module
(or anything that imports it) doesn't require GEMINI_API_KEY to be set
until an LLM call is actually made.
"""

import os
from google import genai

_client = None


def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client
