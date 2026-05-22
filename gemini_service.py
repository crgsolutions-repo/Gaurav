# =========================
# gemini_service.py
# =========================

import google.generativeai as genai

import json

genai.configure(api_key="AIzaSyDUvBeJWtMKAaMMyyzRfc7h8iXvE-2srZU")

model = genai.GenerativeModel("gemini-3.1-flash-lite")


def process_hr_request(user_message):

    prompt = f"""
    You are an AI HR Management System.

    Analyze employee message and return ONLY valid JSON.

    Rules:
    1. Do NOT add markdown.
    2. Do NOT add explanations.
    3. Do NOT invent missing fields.
    4. Missing fields = UNKNOWN.
    5. reply should sound professional and conversational.
    6. If employee says:
       - confirm
       - yes confirm
       - yes submit
       - proceed
       - submit it
       intent MUST be CONFIRM_LEAVE

    Possible intents:
    - PUNCH_IN
    - PUNCH_OUT
    - CHECK_LEAVE_BALANCE
    - APPLY_LEAVE
    - CONFIRM_LEAVE
    - GENERAL_CHAT

    Return ONLY this exact JSON format:

    {{
        "intent": "",
        "leave_type": "",
        "from_date": "",
        "to_date": "",
        "duration": "",
        "reply": ""
    }}

    User message:
    {user_message}
    """

    response = model.generate_content(prompt)

    cleaned_response = response.text.strip()

    cleaned_response = cleaned_response.replace("```json", "")

    cleaned_response = cleaned_response.replace("```", "")

    return json.loads(cleaned_response)