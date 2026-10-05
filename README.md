# Gmail job email classifier

The project downloads new messages from Gmail, saves them in `emails.json`, and classifies each message with a local Ollama model. It keeps messages about active applications, moves unrelated messages to Gmail Trash, and permanently deletes rejection messages.

## Setup

1. Install Python dependencies: `py -m pip install -r requirements.txt`.
2. Create a `.env` file in the project folder:

   ```env
   GMAIL_USER=your-address@gmail.com
   APP_PASSWORD=your-gmail-app-password
   ```

3. Install and start [Ollama](https://ollama.com/), then download the configured model:

   ```sh
   ollama pull qwen3:1.7b
   ```

## Run

From the project folder, run `py app.py`. The app reads the last 100 messages on its first run; later runs fetch only messages it has not cached yet. Classification results and processing state are stored in `emails.json`.

## Ollama interface

The classifier sends the message subject and cleaned body to Ollama's local `POST http://localhost:11434/api/chat` endpoint. It requests a JSON response constrained to one of `rejection`, `waiting`, `invitation`, or `not_job`. The model and request settings are in `utils/llm_classifier.py`; Ollama must be running locally.

Gmail access uses IMAP with a Gmail app password. Rejections are moved to Trash and then expunged there; unrelated messages are moved to Trash and can be restored from Gmail's Trash until it is emptied.
