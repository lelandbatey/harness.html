"""The one "what is this" text. It's used by the web onboarding screens and About page, the TUI (first-run
page, /help) and `sh FILE help`. The build also injects it into the web client (window.SFH_ABOUT), so the
page can show it before any server is running.

Plain markdown, kept short. The owner edits this; nothing else should duplicate it."""

TAGLINE = "An LLM coding agent that lives in one HTML file: the file is both this page and the program behind it."

ABOUT = {
    "tagline": TAGLINE,
    # what it is (shown on the About page, the landing page and `help`)
    "what": (
        TAGLINE + "\n\n"
        "Open the file in a browser to get the chat UI. Run it with `sh` to start the local server that does "
        "the work: it talks to your LLM provider, runs tools (shell, file edits) in your directories, and saves "
        "every session back into the same file. Copy the file and you've copied the whole app and its history. "
        "API keys are kept out of the file by default, in `~/.config/singlefile/credentials`."
    ),
    # step 1: start the program (the page fills in the exact command)
    "run": "Start the program in a terminal. It keeps running while you use the page:",
    # step 2: set up a provider
    "provider": (
        "Any OpenAI-compatible endpoint works (OpenAI, OpenRouter, a local Ollama or "
        "llama.cpp server, a proxy), and so does the Anthropic API. The key is stored in your credentials file, "
        "never in the HTML."
    ),
    "provider_env": (
        "Or set environment variables before starting the server:\n\n"
        "```sh\n"
        "export AI_ENDPOINT=http://127.0.0.1:11434/v1   # base URL of the API\n"
        "export AI_MODEL=your-model-id\n"
        "export AI_TOKEN=sk-...                         # if the endpoint needs a key\n"
        "```\n"
    ),
    # the landing page of a copy published on the web
    "install": (
        "This copy is hosted on the web, so it can't run anything here. To use it, save the file to your "
        "computer, then open the saved file in your browser. It will show you the exact command that starts it. "
        "It needs `python3` and a POSIX `sh` (Linux, macOS, WSL)."
    ),
    "save_alt": (
        "You can also save it with Ctrl-S / ⌘-S. Choose **Webpage, HTML only**: \"Webpage, Complete\" "
        "rewrites the file and breaks it."
    ),
}


def text():
    """The about text for terminals (TUI /help, `sh FILE help`)."""
    env = "\n".join(line for line in ABOUT["provider_env"].splitlines() if not line.startswith("```"))
    return "\n\n".join((ABOUT["what"], ABOUT["provider"], env.rstrip()))
