# =========================
# app.py
# =========================

from flask import Flask, render_template, request, jsonify, redirect, session

from supabase_client import supabase

from gemini_service import process_hr_request

from intent_handlers import (
    handle_punch_in,
    handle_punch_out,
    handle_leave_balance,
    handle_apply_leave,
    handle_confirm_leave
)

app = Flask(__name__)

app.secret_key = "your_secret_key"


@app.route("/")
def home():

    if "employee_id" not in session:

        return redirect("/login")

    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():

    if "employee_id" not in session:

        return jsonify({
            "reply": "Please login first."
        })

    data = request.get_json()

    user_message = data["message"]

    employee_id = session["employee_id"]

    employee_name = session["employee_name"]

    pending_leave = session.get("pending_leave")

    # =========================
    # CENTRALIZED AI PROCESSING
    # =========================

    ai_result = process_hr_request(user_message)

    print(ai_result)

    intent = ai_result["intent"]

    # =========================
    # PUNCH IN
    # =========================

    if intent == "PUNCH_IN":

        return handle_punch_in(
            employee_id,
            employee_name,
            ai_result
        )

    # =========================
    # PUNCH OUT
    # =========================

    if intent == "PUNCH_OUT":

        return handle_punch_out(
            employee_id,
            employee_name,
            ai_result
        )

    # =========================
    # LEAVE BALANCE
    # =========================

    if intent == "CHECK_LEAVE_BALANCE":

        return handle_leave_balance(
            employee_id,
            employee_name
        )

    # =========================
    # APPLY LEAVE
    # =========================

    if intent == "APPLY_LEAVE":

        return handle_apply_leave(ai_result)

    # =========================
    # CONFIRM LEAVE
    # =========================

    if intent == "CONFIRM_LEAVE" and pending_leave:

        return handle_confirm_leave(
            employee_id,
            employee_name
        )

    # =========================
    # GENERAL CHAT
    # =========================

    bot_reply = ai_result["reply"]

    supabase.table("conversations").insert({
        "employee_id": employee_id,
        "user_message": user_message,
        "bot_response": bot_reply
    }).execute()

    return jsonify({
        "reply": bot_reply
    })


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"]

        password = request.form["password"]

        response = supabase.table("employees") \
            .select("*") \
            .eq("email", email) \
            .eq("password", password) \
            .execute()

        if response.data:

            session["employee_id"] = response.data[0]["employee_id"]

            session["employee_name"] = response.data[0]["name"]

            session["role"] = response.data[0]["role"]

            return redirect("/")

        else:

            return render_template(
                "login.html",
                error="Invalid Credentials"
            )

    return render_template("login.html")


@app.route("/logout")
def logout():

    session.clear()

    return redirect("/login")


if __name__ == "__main__":

    app.run(debug=True)