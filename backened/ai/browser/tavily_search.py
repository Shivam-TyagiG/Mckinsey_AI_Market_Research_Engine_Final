import os
import logging
import time

from dotenv import load_dotenv
from tavily import TavilyClient

from ai.browser.search import SearchEngine
from ai.reliability import is_retryable_error, retry_delay


load_dotenv()

logger = logging.getLogger(__name__)


class TavilySearchEngine(SearchEngine):

    def __init__(self, client=None, sleep=time.sleep):
        self.api_key = os.getenv("TAVILY_API_KEY")
        self.client = client
        self.sleep = sleep

    def _get_client(self):
        if self.client is None:
            if not self.api_key:
                raise RuntimeError("TAVILY_API_KEY is not configured.")
            self.client = TavilyClient(api_key=self.api_key)
        return self.client

    def _request(self, operation, operation_name: str):
        for attempt in range(1, 3):
            try:
                return operation(self._get_client())
            except Exception as error:
                if not is_retryable_error(error) or attempt == 2:
                    logger.warning(
                        "Tavily %s failed on attempt %d: %s",
                        operation_name,
                        attempt,
                        error,
                    )
                    raise

                delay = retry_delay(error, attempt, base=0.25)
                logger.warning(
                    "Tavily %s had a temporary failure; retrying in %.2fs.",
                    operation_name,
                    delay,
                )
                self.sleep(delay)

    def search(self, query: str) -> list[dict]:
        response = self._request( 
            # Search the web using this query and give me a maximum of 2 results. 
            # so here we are using the TavilyClient to perform a search operation with the given query.
            # and we are limiting the search results to a maximum of 2. The results will be returned as a list of dictionaries.
            # result contains properties like url, title, publisher, published_date, and content.
            # tavily is used for web search and extraction of content from the web pages.
            # here in _request method we are handling the retry logic for the search operation. If the search operation fails due to a temporary error, it will retry the operation after a delay.
            # first argument is a lambda function that takes a TavilyClient instance and calls its search method with the provided query, search_depth set to "basic", and max_results set to 2.
            # second argument is a string "search" that represents the name of the operation being performed. This is used for logging purposes in case of errors or retries.
            lambda client: client.search(
                query=query,
                search_depth="basic",
                max_results=2,
            ),
            "search",
        )

        return response.get("results", []) if isinstance(response, dict) else []

    def extract(self, urls: list[str]) -> list[dict]:
        response = self._request(
            lambda client: client.extract(urls),
            "extract",
        )

        return response.get("results", []) if isinstance(response, dict) else []