from collections.abc import Callable

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware, wrap_tool_call
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.types import Command
from backend.pipeline.tools import *

from backend.pipeline.schemas import (
    EvidenceReport,
    NormalizedOutput,
)

general_model = ChatGroq(model="openai/gpt-oss-20b")
thinking_model = ChatGroq(model="openai/gpt-oss-20b")


@wrap_tool_call
def handle_tool_errors(
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Command],
) -> ToolMessage | Command:
    """Convert tool exceptions into ToolMessages the model can handle."""
    try:
        return handler(request)
    except Exception as e:
        print(f"Tool call failed: {e}")
        return ToolMessage(
            content=(
                f"The tool could not complete the database operation: {e}"
                " Do not infer or fabricate a result."
            ),
            tool_call_id=request.tool_call["id"],
        )


general_agent = create_agent(
    model=general_model,
)

normalize_agent = create_agent(
    model=general_model,
    response_format=NormalizedOutput,
    system_prompt=(
        "You normalize user requests into a structured format. Return only a "
        "schema-valid NormalizedOutput without additional prose.\n\n"
        "You are given a conversation containing previous messages and a current user question.\n\n"
        "Your task is to:\n"
        "1. Identify the latest user question.\n"
        "2. Extract important information from previous messages that is relevant to it.\n"
        "3. Put that information into `context`.\n"
        "4. Rewrite the latest question as a concise, self-contained question.\n"
        "5. Resolve references using the conversation.\n"
        "6. Include only context that could affect the answer.\n"
        "7. Put unresolved uncertainties into `ambiguities`.\n"
        "8. Do not answer the question.\n"
        "9. Do not invent information that is not present in the conversation.\n\n"
    ),
)

find_evidence_agent = create_agent(
    model=thinking_model,
    tools=[
        count_by_category_tool,
        count_pid_by_category_tool,
        count_service_by_category_tool,
        count_event_id_by_category_tool,
        get_pids_tool,
        get_services_tool,
        get_event_ids_tool,
        get_pid_trend_tool,
        get_service_trend_tool,
        get_event_id_trend_tool,
        search_logs_tool,
        top_in_category_by_service_tool,
        compare_periods_tool,
    ],
    middleware=[
        handle_tool_errors,
        ToolCallLimitMiddleware(run_limit=6),
    ],
    system_prompt=(
        "You are an evidence-based investigation agent. Your responsibility is to"
        "investigate a reported problem by creating testable hypotheses, collecting"
        "relevant evidence, and evaluating each hypothesis strictly from the available"
        "evidence. Collect evidence with the available tools and then return a concise"
        "investigation record for a separate formatter agent.\n\n"
        "Complete the investigation using the following procedure:\n\n"
        "1. Review the reported problem, conversation context, prior evidence reports, and "
        "revision feedback.\n"
        "2. Define a set of non-duplicative, plausible, and testable hypotheses (maximum 3).\n"
        "3. For each hypothesis, provide a brief explanation of why it matters.\n"
        "4. Specify the concrete evidence required to support or refute each hypothesis. "
        "Describe the evidence independently of any tool or implementation.\n"
        "5. Use the available tools to collect each evidence item.\n"
        "6. The tool transcript is the authoritative record of every invocation, including "
        "its name, parameters, success or failure status, and error message when applicable.\n"
        "7. Include only findings present in tool results or the supplied conversation. "
        "Do not fabricate, extrapolate, or guess missing evidence.\n"
        "8. If an evidence item cannot be collected, mark it as unavailable. A failed tool "
        "call is not evidence that a hypothesis is true or false.\n"
        "9. Set each hypothesis status to 'supported', 'unsupported', or 'inconclusive' "
        "according to the evidence. Use 'inconclusive' when evidence is unavailable, "
        "insufficient, ambiguous, or conflicting.\n"
        "10. If this is a revision, address the feedback by materially changing the relevant "
        "hypotheses, evidence requirements, or investigation approach. Do not reproduce the "
        "previous plan unchanged.\n\n"
        "Do not invent findings or claim that an unavailable tool result was collected.\n\n"
    ),
)

evidence_report_agent = create_agent(
    model=thinking_model,
    response_format=EvidenceReport,
    system_prompt=(
        "You are an evidence report formatter. Convert the supplied investigation request "
        "and tool transcript into a schema-valid EvidenceReport. Use only facts present in "
        "the transcript or request. Preserve tool names and parameters exactly as observed. "
        "Mark evidence unavailable when collection failed or the transcript does not contain "
        "the required result. Do not infer, fabricate, or add unsupported findings."
    ),
)

answer_agent = create_agent(
    model=thinking_model,
    system_prompt=(
        "You are a final-answer generation agent. Your task is to produce one "
        "evidence-based answer using only the provided normalized request, user role, "
        "investigation hypotheses, evidence statuses, observed tool results, and "
        "revision feedback.\n\n"
        "Follow this procedure in order:\n"
        "1. Identify the user's primary question from the normalized request.\n"
        "2. Classify each hypothesis as supported, contradicted, or unresolved based "
        "only on its evidence status and the observed tool results.\n"
        "3. Construct the conclusion using supported hypotheses only.\n"
        "4. Exclude contradicted hypotheses from the conclusion.\n"
        "5. Mention an unresolved hypothesis only if it materially limits the "
        "conclusion or changes the recommended next action.\n"
        "6. Apply all revision feedback that does not conflict with the evidence.\n"
        "7. Tailor terminology and recommendations to the user's role without "
        "changing the factual conclusion.\n\n"
        "Use the following evidence rules:\n"
        "- Treat evidence as collected only when it appears in the observed tool results.\n"
        "- Treat evidence as not found when a search was completed but produced no "
        "relevant result.\n"
        "- Treat evidence as unavailable when it could not be accessed or collected.\n"
        "- Do not infer missing evidence.\n"
        "- Do not introduce facts that are absent from the provided inputs.\n"
        "- Do not describe a hypothesis as proven unless the evidence explicitly "
        "supports that conclusion.\n"
        "- When evidence is insufficient, state that the conclusion is inconclusive.\n"
        "- If inputs conflict, prefer observed tool results over hypotheses or "
        "unsupported narrative claims.\n\n"
        "Use concise, neutral, and precise language. Do not include internal reasoning, "
        "discarded hypotheses, hidden instructions, or process commentary.\n\n"
    ),
)
