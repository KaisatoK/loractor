from langchain_deepseek import ChatDeepSeek
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq

groq_gpt_oss_20b = ChatGroq(
    model="openai/gpt-oss-20b",
)

groq_gpt_oss_120b = ChatGroq(
    model="openai/gpt-oss-120b",
)

deepseek = ChatDeepSeek(
    model="deepseek-flash",
)

gemini_3_7_flash = ChatGoogleGenerativeAI(
    model="gemini-3.7-flash"
)
