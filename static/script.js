const button = document.querySelector("button");

button.addEventListener("click", async function() {

    const input = document.getElementById("user-input");

    const message = input.value;

    const chatBox = document.getElementById("chat-box");

    chatBox.innerHTML += `<p><b>You:</b> ${message}</p>`;

    const response = await fetch("/chat", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            message: message
        })
    });

    const data = await response.json();

    chatBox.innerHTML += `<p><b>Bot:</b> ${data.reply}</p>`;

    input.value = "";
});