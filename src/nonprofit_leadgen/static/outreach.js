const form = document.getElementById("generate-form");
if (form) {
  form.addEventListener("submit", () => {
    document.getElementById("generate-button").disabled = true;
    document.getElementById("loading-status").hidden = false;
  });
}
const copyButton = document.getElementById("copy-draft");
if (copyButton) {
  copyButton.addEventListener("click", async () => {
    const subject = document.getElementById("draft-subject").value;
    const body = document.getElementById("draft-body").value;
    const status = document.getElementById("copy-status");
    try {
      await navigator.clipboard.writeText(`Subject: ${subject}\n\n${body}`);
      status.textContent = "Copied current text to clipboard.";
    } catch {
      status.textContent = "Clipboard unavailable. Select and copy the text manually.";
    }
  });
}
