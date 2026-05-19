from flask import redirect
from supabase_client import supabase
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():

    data = request.get_json()

    user_message = data["message"]

    return jsonify({
        "reply": f"You said: {user_message}"
    })
@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"]
        password = request.form["password"]

        response = supabase.table("employees").select("*").eq("email", email).eq("password", password).execute()

        if response.data:
             return redirect("/")

        else:
             return render_template("login.html", error="Invalid Credentials")
    return render_template("login.html")
if __name__ == "__main__":
    app.run(debug=True)