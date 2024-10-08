import os
from typing import Callable, Optional
from abc import ABC, abstractmethod
import time
from ratelimit import limits, sleep_and_retry
from datetime import datetime, timedelta
from token_estimation_utils import estimate_tokens
from .config import LLMConfig
# set up tracing, use relative import to avoid import errors since they are in the same path
#from .langfuse_setup import observe, langfuse_context




class LLMInterface(ABC):
    @abstractmethod
    def query(self, user_prompt: str) -> str:
        pass


class OpenAILLM(LLMInterface):
    def __init__(self, config: LLMConfig):
        from llm_client.llm_openai import OpenAIAssistant
        
        assistant_without_history = OpenAIAssistant(config, use_history=False)
        self.assistant = assistant_without_history

    #@observe(as_type="generation", capture_input=True, capture_output=True)
    def query(self, user_prompt: str) -> str:
        response = self.assistant.query(user_prompt)
        while callable(response):
            response = response()
        
        return response

    
class VertexAILLM(LLMInterface):
    def __init__(self, config: LLMConfig):
        from llm_client.llm_google_vertexai import VertexAssistant
        project_id = os.getenv("GCP_PROJECT_ID")
        location = os.getenv("GCP_LOCATION")
       
        self.assistant = VertexAssistant(
            project_id, 
            location, 
            config=config, 
            use_history=False
        )

    #@observe(as_type="generation", capture_input=True, capture_output=True)
    def query(self, user_prompt: str) -> str:
        return self.assistant.query(user_prompt)

class AnthropicLLM(LLMInterface):
    def __init__(self, config: LLMConfig):
        from llm_client.llm_anthropic import AnthropicAssistant
        
        self.assistant = AnthropicAssistant(config, use_history=False)

    #@observe(as_type="generation", capture_input=True, capture_output=True)
    def query(self, user_prompt: str) -> str:
        return self.assistant.query(user_prompt)

class LLMFactory: 
    
    @staticmethod
    def get_llm(use_llm: str = "openai", tier: str = "tier1", system_prompt: str = "You are a helpful assistant", cached_prompt: str = None) -> LLMInterface:
        if use_llm == "openai":
            config = LLMConfig(api_key=os.environ.get("OPENAI_API_KEY"), model_name=os.environ.get(f"OPENAI_MODEL_{tier.upper()}_NAME"),
                           system_prompt=system_prompt, cached_prompt=cached_prompt)
            return OpenAILLM(config=config)
        elif use_llm == "gemini":
            config = LLMConfig(model_name=os.environ.get(f"GCP_MODEL_{tier.upper()}_NAME"),
                           system_prompt=system_prompt, cached_prompt=cached_prompt)
            return VertexAILLM(config=config)
        elif use_llm == "anthropic":
            config = LLMConfig(api_key=os.environ.get("ANTHROPIC_API_KEY"), model_name=os.environ.get(f"ANTHROPIC_MODEL_{tier.upper()}_NAME"),
                           system_prompt=system_prompt, cached_prompt=cached_prompt)
            return AnthropicLLM(config=config)
        else:
            raise ValueError("Please set the environment variable USE_LLM to either openai, gemini, or anthropic")

class LLMQueryManager:
    def __init__(self, use_llm: str, tier: str = "tier1", system_prompt: str = "You are a helpful assistant", cached_prompt: str = None, 
                 max_calls: int = 10, period: int = 60, 
                 max_tokens_per_min: int = 80000, max_tokens_per_day: int = 2500000,
                 encoding_name: str = "cl100k_base"):
        if not use_llm:
            raise ValueError("Please set the environment variable USE_LLM to either openai, gemini, or anthropic")
        
        self.llm = LLMFactory.get_llm(use_llm=use_llm, tier=tier, system_prompt=system_prompt, cached_prompt=cached_prompt)
        self.max_calls = max_calls
        self.period = period
        self.max_tokens_per_min = max_tokens_per_min
        self.max_tokens_per_day = max_tokens_per_day
        self.encoding_name = encoding_name
        
        self.input_tokens_used_today = 0
        self.output_tokens_used_today = 0
        self.tokens_used_this_minute = 0
        self.last_token_reset = datetime.now()
        self.last_day_reset = datetime.now().date()

        # Pricing per million tokens (in USD)
        self.input_token_price = 3.00
        self.output_token_price = 15.00

    def _reset_token_counters(self):
        now = datetime.now()
        if now.date() > self.last_day_reset:
            self.input_tokens_used_today = 0
            self.output_tokens_used_today = 0
            self.last_day_reset = now.date()
        
        if (now - self.last_token_reset).total_seconds() >= 60:
            self.tokens_used_this_minute = 0
            self.last_token_reset = now

    def _update_token_usage(self, input_tokens: int, output_tokens: int):
        self.input_tokens_used_today += input_tokens
        self.output_tokens_used_today += output_tokens
        self.tokens_used_this_minute += input_tokens + output_tokens

    def _check_token_limits(self, estimated_tokens: int):
        self._reset_token_counters()
        if self.input_tokens_used_today + self.output_tokens_used_today + estimated_tokens > self.max_tokens_per_day:
            raise Exception("Daily token limit exceeded")
        if self.tokens_used_this_minute + estimated_tokens > self.max_tokens_per_min:
            sleep_time = 60 - (datetime.now() - self.last_token_reset).total_seconds()
            time.sleep(max(0, sleep_time))
            self._reset_token_counters()

    @sleep_and_retry
    @limits(calls=10, period=60)
    def rate_limited_query(self, user_prompt: str) -> str:
        input_tokens = estimate_tokens(user_prompt, self.encoding_name)
        self._check_token_limits(input_tokens)
        
        response = self.llm.query(user_prompt)
        
        output_tokens = estimate_tokens(response, self.encoding_name)
        self._update_token_usage(input_tokens, output_tokens)
        
        return response

    def query(self, user_prompt: str) -> str:
        return self.rate_limited_query(user_prompt)

    def get_total_tokens(self) -> tuple:
        return (self.input_tokens_used_today, self.output_tokens_used_today)

    def estimate_cost(self) -> float:
        input_cost = (self.input_tokens_used_today / 1_000_000) * self.input_token_price
        output_cost = (self.output_tokens_used_today / 1_000_000) * self.output_token_price
        return input_cost + output_cost

# Usage
if __name__ == "__main__":
    # Load configuration into environment variables
    from config_utils import load_config_to_env
    load_config_to_env()
    
    system_prompt = "You are a helpful assistant."
    user_prompt = "How are you?"
   
   # now we test each of the LLMs, including OpenAI, Google GenAI, Google VertexAI, and Anthropic
    try:
        assistant = LLMQueryManager(use_llm='openai', tier = "tier2", system_prompt = system_prompt)
        response = assistant.query(user_prompt)
        print(response[:100])
    except Exception as e:
        print(e)

    try:
        assistant = LLMQueryManager(use_llm='gemini', tier = "tier2", system_prompt = system_prompt)
        response = assistant.query(user_prompt)
        print(response[:100])
    except Exception as e:
        print(e)

    try:
        assistant = LLMQueryManager(use_llm='anthropic', tier = "tier2", system_prompt = system_prompt)
        response = assistant.query(user_prompt)
        print(response[:100])
    except Exception as e:
        print(e)

    #langfuse_context.flush()

