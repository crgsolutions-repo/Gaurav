const form = document.getElementById("chat-form");
const input = document.getElementById("user-input");
const dateInput = document.getElementById("date-input");
const receiptInput = document.getElementById("receipt-input");
const chatBox = document.getElementById("chat-box");

function appendMessage(sender, text, variant = "") {
    const wrapper = document.createElement("div");
    wrapper.className = `message ${sender.toLowerCase()} ${variant}`.trim();

    const label = document.createElement("span");
    label.textContent = sender;

    const body = document.createElement("p");
    renderMessageText(body, text);

    wrapper.append(label, body);
    chatBox.appendChild(wrapper);
    chatBox.scrollTop = chatBox.scrollHeight;

    return wrapper;
}

function renderMessageText(element, text) {
    const messageText = String(text || "");
    const linkPattern = /(\/[A-Za-z0-9/_-]+(?:\?[A-Za-z0-9._~%=&-]+)?)/g;
    let cursor = 0;
    messageText.replace(linkPattern, function(match, _unused, offset) {
        if (offset > cursor) {
            element.appendChild(document.createTextNode(messageText.slice(cursor, offset)));
        }

        const link = document.createElement("a");
        link.href = match;
        link.textContent = match;
        link.className = "chat-link";
        element.appendChild(link);
        cursor = offset + match.length;
        return match;
    });

    if (cursor < messageText.length) {
        element.appendChild(document.createTextNode(messageText.slice(cursor)));
    }
}

form.addEventListener("submit", async function(event) {
    event.preventDefault();

    const typedMessage = input.value.trim();
    const selectedDate = dateInput ? dateInput.value : "";
    const receipt = receiptInput && receiptInput.files.length ? receiptInput.files[0] : null;
    const message = [typedMessage, selectedDate].filter(Boolean).join(" ");
    if (!message && !receipt) {
        input.focus();
        return;
    }

    appendMessage("You", receipt ? `${message || "Receipt attached"}\nReceipt: ${receipt.name}` : message);
    input.value = "";
    if (dateInput) {
        dateInput.value = "";
    }
    if (receiptInput) {
        receiptInput.value = "";
    }
    input.disabled = true;
    if (dateInput) {
        dateInput.disabled = true;
    }
    if (receiptInput) {
        receiptInput.disabled = true;
    }

    const loading = appendMessage("Bot", "Thinking...", "loading");

    try {
        const fetchOptions = {
            method: "POST"
        };

        if (receipt) {
            const formData = new FormData();
            formData.append("message", message);
            formData.append("receipt", receipt);
            fetchOptions.body = formData;
        } else {
            fetchOptions.headers = {
                "Content-Type": "application/json"
            };
            fetchOptions.body = JSON.stringify({ message });
        }

        const response = await fetch("/chat", {
            ...fetchOptions
        });

        const data = await response.json();
        loading.remove();
        appendMessage("Bot", data.reply || "I could not process that request.", response.ok ? "" : "error");
    } catch (error) {
        loading.remove();
        appendMessage("Bot", "Unable to reach the HR assistant. Please try again.", "error");
    } finally {
        input.disabled = false;
        if (dateInput) {
            dateInput.disabled = false;
        }
        if (receiptInput) {
            receiptInput.disabled = false;
        }
        input.focus();
    }
});
