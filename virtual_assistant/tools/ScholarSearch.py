from typing import Optional
from agency_swarm.tools import BaseTool
from pydantic import Field
import json

import os
from dotenv import load_dotenv


class ScholarSearch(BaseTool):
    """
    Searches for scholarly literature on Google Scholar.
    
    Returns academic papers, articles, theses, books, and conference papers.
    Includes links to PDFs and full-text resources when available.
    
    RATE LIMIT: This tool can only be called ONCE per each user request (message) to save API costs.
    Make sure to request enough results in a single call.
    """

    query: str = Field(
        ...,
        description="Search query for scholarly articles (e.g., 'machine learning', 'climate change effects', 'quantum computing')"
    )
    
    year_from: Optional[int] = Field(
        default=None,
        description="Filter results from this year onwards (e.g., 2020)"
    )
    
    year_to: Optional[int] = Field(
        default=None,
        description="Filter results up to this year (e.g., 2024)"
    )
    
    num_results: int = Field(
        default=10,
        ge=1,
        le=20,
        description="Number of results to return (1-20)"
    )
    
    page: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination"
    )
    
    def run(self):
        load_dotenv(override=True)
        try:
            import requests
            
            # Rate limiting: Check if already called in this session
            if self.context and self.context.get("scholar_search_called", False):
                return "Error: ScholarSearch can only be called once per user request to save API costs. Use the results from the previous search or web search tool."
            
            api_key = os.getenv("SERPER_API_KEY")
            if not api_key:
                raise ValueError("SERPER_API_KEY is not set. Add it to your .env to use ScholarSearch.")
            
            # Build request body (Serper uses POST + JSON)
            payload: dict = {
                "q": self.query,
                "num": self.num_results,
                "page": self.page,
            }
            
            # Year range via Google's tbs parameter
            if self.year_from or self.year_to:
                year_min = self.year_from or 1900
                year_max = self.year_to or 2100
                payload["tbs"] = f"cdr:1,cd_min:01/01/{year_min},cd_max:12/31/{year_max}"
            
            # Make API request
            response = requests.post(
                "https://google.serper.dev/scholar",
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=30
            )
            
            if response.status_code != 200:
                return f"Error: API returned status {response.status_code}: {response.text}"
            
            data = response.json()
            
            # Check for API errors
            if "error" in data:
                return f"Error from API: {data['error']}"
            
            # Extract results — Serper returns 'organic' list
            organic_results = data.get("organic", [])
            search_info = data.get("searchParameters", {})
            
            # Format articles
            articles = []
            for result in organic_results:
                # Authors come as a plain string in Serper
                raw_authors = result.get("authors", "")
                authors = [a.strip() for a in raw_authors.split(",")] if raw_authors else []
                
                article = {
                    "title": result.get("title"),
                    "link": result.get("link"),
                    "publication": result.get("publication"),
                    "snippet": result.get("snippet"),
                    "year": result.get("year"),
                    "authors": authors,
                    "citations": result.get("cited_by"),
                }
                
                # PDF link if available
                if result.get("pdf"):
                    article["resource"] = {"format": "PDF", "link": result["pdf"]}
                
                articles.append(article)
            
            # Serper scholar does not return author profile cards
            author_profiles = []
            
            # Mark as called in shared state (rate limiting)
            if self.context:
                self.context.set("scholar_search_called", True)

            result = {
                "query": self.query,
                "filters": {
                    "year_from": self.year_from,
                    "year_to": self.year_to
                },
                "total_results": data.get("credits"),
                "page": self.page,
                "articles_count": len(articles),
                "articles": articles
            }
            
            if author_profiles:
                result["author_profiles"] = author_profiles
            
            return json.dumps(result, indent=2)
            
        except Exception as e:
            return f"Error searching scholar: {str(e)}"



if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
    
    print("=" * 60)
    print("ScholarSearch Test Suite")
    print("=" * 60)
    print()
    
    # Test 1: Basic search
    print("Test 1: Basic scholarly search")
    print("-" * 60)
    tool = ScholarSearch(
        query="transformer architecture deep learning",
        num_results=5
    )
    result = tool.run()
    
    try:
        data = json.loads(result)
        print(f"Query: {data['query']}")
        print(f"Total results: {data.get('total_results', 'N/A')}")
        print(f"Articles returned: {data['articles_count']}")
        print()
        
        for i, article in enumerate(data['articles'][:3], 1):
            print(f"{i}. {article['title']}")
            print(f"   Authors: {', '.join(article['authors'][:3])}...")
            print(f"   Citations: {article.get('citations', 'N/A')}")
            if article.get('resource'):
                print(f"   PDF: {article['resource'].get('link', 'N/A')}")
            print()
    except json.JSONDecodeError:
        print(result)
    
    print("=" * 60)
    print("Tests completed!")
    print("=" * 60)

