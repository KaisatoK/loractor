from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langgraph.prebuilt import create_react_agent

llm = ChatOllama(model="qwen3.5:4b")

@tool
def count_by_category_tool():
    """Count log entries grouped by category within a time window."""
    

retrieval_agent = create_react_agent(
    llm,
    tools=[count_by_category_tool, get_trend_tool, compare_periods_tool],
    prompt="You retrieve structured aggregate data (counts, trends, comparisons) from logs. "
           "Pick the right tool and correct parameters. Do not interpret results — just return data."
)

search_agent = create_react_agent(
    llm,
    tools=[search_logs_tool, top_locations_tool],
    prompt="You find relevant raw log evidence using free-text search or location breakdowns. "
           "Return the raw matches, do not summarize."
)

