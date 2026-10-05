import os
import re
import json
import imaplib
import email

from pathlib import Path
from email.header import decode_header
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

GMAIL_USER = os.environ["GMAIL_USER"]
APP_PASSWORD = os.environ["APP_PASSWORD"]

EMAILS_FILE = Path("emails.json")

MAILBOX = "INBOX"

# На самом первом запуске не тащим 10 000 старых писем.
INITIAL_LIMIT = 100


def decode_mime_header(value):
    if not value:
        return ""

    result = []

    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            result.append(
                part.decode(
                    encoding or "utf-8",
                    errors="replace",
                )
            )
        else:
            result.append(part)

    return "".join(result)


def load_database():
    if not EMAILS_FILE.exists():
        return {"emails": {}}

    with EMAILS_FILE.open(
        "r",
        encoding="utf-8",
    ) as f:
        return json.load(f)


def save_database(db):
    temp_file = EMAILS_FILE.with_suffix(".tmp")

    with temp_file.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            db,
            f,
            ensure_ascii=False,
            indent=2,
        )

    # atomic-ish replacement
    temp_file.replace(EMAILS_FILE)


def get_uidvalidity(mail):
    status, data = mail.status(
        MAILBOX,
        "(UIDVALIDITY)",
    )

    if status != "OK":
        raise RuntimeError("Could not get UIDVALIDITY")

    raw = data[0].decode()

    match = re.search(
        r"UIDVALIDITY\s+(\d+)",
        raw,
    )

    if not match:
        raise RuntimeError(f"Could not parse UIDVALIDITY: {raw}")

    return match.group(1)


def decode_part(part):
    payload = part.get_payload(decode=True)

    if payload is None:
        return ""

    charset = part.get_content_charset() or "utf-8"

    try:
        return payload.decode(
            charset,
            errors="replace",
        )

    except LookupError:
        return payload.decode(
            "utf-8",
            errors="replace",
        )


def extract_email_body(message):
    plain_parts = []
    html_parts = []

    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()

            disposition = part.get_content_disposition()

            # attachments не нужны классификатору
            if disposition == "attachment":
                continue

            if content_type == "text/plain":
                text = decode_part(part)

                if text:
                    plain_parts.append(text)

            elif content_type == "text/html":
                text = decode_part(part)

                if text:
                    html_parts.append(text)

    else:
        content_type = message.get_content_type()

        text = decode_part(message)

        if content_type == "text/plain":
            plain_parts.append(text)

        elif content_type == "text/html":
            html_parts.append(text)

    # plain text предпочтительнее
    if plain_parts:
        return "\n".join(plain_parts)

    if html_parts:
        html = "\n".join(html_parts)

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        # удаляем явно ненужные элементы
        for tag in soup(["script", "style", "head"]):
            tag.decompose()

        return soup.get_text(
            "\n",
            strip=True,
        )

    return ""


def remove_quoted_reply(text):
    """
    Удаляем наиболее распространённые quoted replies.

    Например:
    On 5 Oct 2026 ... wrote:
    > old message

    или:
    -----Original Message-----
    """

    patterns = [
        r"\nOn .+?wrote:\s*\n",
        r"\nAm .+?schrieb .+?:\s*\n",
        r"\nВ .+?писал\(а\)?:\s*\n",
        r"\n-{2,}\s*Original Message\s*-{2,}",
        r"\n-{2,}\s*Forwarded message\s*-{2,}",
        r"\nОт:\s*.+\nОтправлено:",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if match:
            text = text[: match.start()]

    # строки вида:
    # > previous message
    lines = []

    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            continue

        lines.append(line)

    return "\n".join(lines)


def remove_signature(text):
    """
    Пока очень консервативная очистка.

    Не пытаемся агрессивно удалять все подписи,
    чтобы случайно не потерять полезный текст.
    """

    separators = [
        "\n-- \n",
        "\nSent from my iPhone",
        "\nSent from my Android",
        "\nОтправлено с iPhone",
    ]

    for separator in separators:
        if separator.lower() in text.lower():
            index = text.lower().find(separator.lower())

            text = text[:index]

    return text


def normalize_whitespace(text):
    text = text.replace(
        "\r\n",
        "\n",
    )

    text = text.replace(
        "\r",
        "\n",
    )

    # trailing spaces
    text = "\n".join(line.strip() for line in text.splitlines())

    # максимум две пустые строки подряд
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text,
    )

    return text.strip()


def remove_urls(text):
    """
    Удаляем URL, но не весь текст строки.

    Например:
    "Выберите время: https://example.com/calendar"
    ->
    "Выберите время:"
    """

    # http://...
    # https://...
    # www....
    text = re.sub(
        r"https?://[^\s<>()]+",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\bwww\.[^\s<>()]+",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Иногда в plaintext встречаются конструкции:
    # <https://example.com>
    text = re.sub(
        r"<\s*https?://[^>]+>",
        "",
        text,
        flags=re.IGNORECASE,
    )

    return text


def clean_body(text):
    text = remove_quoted_reply(text)
    text = remove_signature(text)

    # Удаляем URL перед нормализацией пробелов
    text = remove_urls(text)

    text = normalize_whitespace(text)

    return text


def parse_message(raw_email, uid):
    message = email.message_from_bytes(raw_email)

    body_raw = extract_email_body(message)

    body = clean_body(body_raw)

    return {
        "uid": uid,
        "message_id": decode_mime_header(message.get("Message-ID")),
        "from": decode_mime_header(message.get("From")),
        "to": decode_mime_header(message.get("To")),
        "subject": decode_mime_header(message.get("Subject")),
        "date": decode_mime_header(message.get("Date")),
        "body": body,
    }


def get_all_uids(mail):
    status, data = mail.uid(
        "search",
        None,
        "ALL",
    )

    if status != "OK":
        raise RuntimeError("Could not search mailbox")

    return [uid.decode() for uid in data[0].split()]


def fetch_message(mail, uid):
    status, data = mail.uid(
        "fetch",
        uid,
        "(RFC822)",
    )

    if status != "OK":
        raise RuntimeError(f"Could not fetch UID {uid}")

    for item in data:
        if isinstance(item, tuple) and isinstance(
            item[1],
            bytes,
        ):
            return item[1]

    raise RuntimeError(f"No RFC822 body for UID {uid}")


def main():
    db = load_database()

    emails = db.setdefault(
        "emails",
        {},
    )

    mail = imaplib.IMAP4_SSL(
        "imap.gmail.com",
        993,
    )

    try:
        mail.login(
            GMAIL_USER,
            APP_PASSWORD,
        )

        status, _ = mail.select(
            MAILBOX,
            readonly=True,
        )

        if status != "OK":
            raise RuntimeError(f"Could not select {MAILBOX}")

        uidvalidity = get_uidvalidity(mail)

        uids = get_all_uids(mail)

        print(f"Messages in Gmail: {len(uids)}")

        first_run = len(emails) == 0

        if first_run:
            uids = uids[-INITIAL_LIMIT:]

            print("First run:" f" checking last {len(uids)} messages")

        new_count = 0
        skipped_count = 0

        for uid in uids:
            key = f"{uidvalidity}:{uid}"

            # Самая важная часть:
            # RFC822/body вообще НЕ запрашиваем,
            # если письмо уже локально есть.
            if key in emails:
                skipped_count += 1
                continue

            print(f"Fetching new message UID={uid}")

            raw_email = fetch_message(
                mail,
                uid,
            )

            parsed = parse_message(
                raw_email,
                uid,
            )

            parsed["uidvalidity"] = uidvalidity

            emails[key] = parsed

            new_count += 1

            # сохраняем после каждого письма,
            # чтобы при падении ничего не потерять
            save_database(db)

            print(
                "  FROM:",
                parsed["from"],
            )

            print(
                "  SUBJECT:",
                parsed["subject"],
            )

        print()
        print(f"New: {new_count}")
        print(f"Already cached: {skipped_count}")
        print(f"Stored total: {len(emails)}")

    finally:
        try:
            mail.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
