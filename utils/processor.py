import os
import re
import json
import imaplib

from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from utils.llm_classifier import (
    classify,
    unload_model,
    MODEL_NAME,
)

EMAILS_FILE = Path("emails.json")

GMAIL_USER = os.environ["GMAIL_USER"]
APP_PASSWORD = os.environ["APP_PASSWORD"]

MAILBOX = "INBOX"

# False = реально переносим not_job в корзину.
# True = только показываем, что было бы удалено.
DRY_RUN = False

# Нерусские письма пока не трогаем.
MIN_RUSSIAN_RATIO = 0.40


def load_database():
    with EMAILS_FILE.open(
        "r",
        encoding="utf-8",
    ) as f:
        return json.load(f)


def save_database(db):
    temp = EMAILS_FILE.with_suffix(".tmp")

    with temp.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            db,
            f,
            ensure_ascii=False,
            indent=2,
        )

    temp.replace(EMAILS_FILE)


def russian_ratio(text):
    ru = len(
        re.findall(
            r"[А-Яа-яЁё]",
            text,
        )
    )

    en = len(
        re.findall(
            r"[A-Za-z]",
            text,
        )
    )

    total = ru + en

    if total == 0:
        return 0.0

    return ru / total


def find_trash_folder(mail):
    status, folders = mail.list()

    if status != "OK":
        raise RuntimeError("Не удалось получить список IMAP-папок")

    for raw in folders:
        line = raw.decode(
            "utf-8",
            errors="replace",
        )

        if "\\Trash" not in line:
            continue

        match = re.search(
            r'"([^"]+)"\s*$',
            line,
        )

        if match:
            return match.group(1)

        # fallback для папки без кавычек
        return line.split()[-1].strip('"')

    raise RuntimeError("Не удалось найти Gmail Trash folder")


def move_to_trash(mail, uid, trash_folder):
    folder = '"' + trash_folder.replace('"', '\\"') + '"'

    status, data = mail.uid(
        "MOVE",
        uid,
        folder,
    )

    if status != "OK":
        raise RuntimeError(f"Не удалось переместить UID={uid} " f"в Trash: {data}")


def permanently_delete(mail, uid, trash_folder, message_id):
    """Move a rejection to Gmail Trash, then permanently expunge it."""
    status, _ = mail.select(trash_folder, readonly=False)
    if status != "OK":
        raise RuntimeError(f"Не удалось открыть Gmail Trash: {trash_folder}")

    # Recover safely if an earlier interrupted run already moved this message.
    status, data = mail.uid("search", None, "HEADER", "Message-ID", message_id)
    trash_uid = data[0].split()[-1] if status == "OK" and data and data[0] else None

    if not trash_uid:
        status, _ = mail.select(MAILBOX, readonly=False)
        if status != "OK":
            raise RuntimeError(f"Не удалось открыть {MAILBOX}")
        move_to_trash(mail, uid, trash_folder)
        _, copyuid_data = mail.response("COPYUID")
        if copyuid_data:
            parts = copyuid_data[0].split()
            if len(parts) >= 3:
                trash_uid = parts[-1].decode("ascii")
        status, _ = mail.select(trash_folder, readonly=False)
        if status != "OK":
            raise RuntimeError(f"Не удалось открыть Gmail Trash: {trash_folder}")

    if not trash_uid:
        status, data = mail.uid("search", None, "HEADER", "Message-ID", message_id)
        if status == "OK" and data and data[0]:
            trash_uid = data[0].split()[-1]
    if not trash_uid:
        raise RuntimeError(f"Не удалось найти письмо UID={uid} в Trash")

    status, result = mail.uid("store", trash_uid, "+FLAGS", r"(\Deleted)")
    if status != "OK":
        raise RuntimeError(f"Не удалось пометить UID={uid} для удаления: {result}")
    status, result = mail.uid("expunge", trash_uid)
    if status != "OK":
        raise RuntimeError(f"Не удалось окончательно удалить UID={uid}: {result}")


def main():
    db = load_database()

    emails = db.get(
        "emails",
        {},
    )

    print(f"Emails in JSON: {len(emails)}")

    mail = imaplib.IMAP4_SSL(
        "imap.gmail.com",
        993,
    )

    classified = 0
    skipped = 0
    trashed = 0
    deleted = 0
    errors = 0

    try:
        mail.login(
            GMAIL_USER,
            APP_PASSWORD,
        )

        status, _ = mail.select(
            MAILBOX,
            readonly=False,
        )

        if status != "OK":
            raise RuntimeError("Не удалось открыть INBOX")

        trash_folder = find_trash_folder(mail)

        print(
            "Trash folder:",
            trash_folder,
        )

        pending = [
            (key, email)
            for key, email in emails.items()
            if not email.get("trashed") and not email.get("deleted")
        ]

        for index, (
            key,
            email,
        ) in enumerate(
            pending,
            start=1,
        ):
            subject = email.get(
                "subject",
                "",
            )

            body = email.get(
                "body",
                "",
            )

            text = subject + "\n" + body

            print()
            print("=" * 80)

            print(f"[{index}/{len(pending)}]")

            print(
                "SUBJECT:",
                subject,
            )

            # --------------------------------
            # Если уже классифицировано,
            # повторно LLM не вызываем.
            # --------------------------------

            ml = email.get("ml")

            if ml and ml.get("category"):
                category = ml["category"]

                print(
                    "CACHED:",
                    category,
                )

            else:
                # ----------------------------
                # Пока работаем только
                # с русскоязычными письмами.
                # ----------------------------

                ratio = russian_ratio(text)

                if ratio < MIN_RUSSIAN_RATIO:
                    print(
                        "SKIP: non-russian",
                        f"({ratio:.0%})",
                    )

                    email["ml"] = {
                        "category": None,
                        "model": MODEL_NAME,
                        "reason": "non_russian",
                    }

                    save_database(db)

                    skipped += 1
                    continue

                # ----------------------------
                # LLM classification
                # ----------------------------

                try:
                    category = classify(
                        subject,
                        body,
                    )

                except Exception as error:
                    print(
                        "LLM ERROR:",
                        error,
                    )

                    errors += 1
                    continue

                email["ml"] = {
                    "category": category,
                    "model": MODEL_NAME,
                }

                save_database(db)

                classified += 1

                print(
                    "CATEGORY:",
                    category,
                )

            # --------------------------------
            # Оставляем job-related письма.
            # --------------------------------

            if category not in {"not_job", "rejection"}:
                print("KEEP")
                continue

            # --------------------------------
            # NOT_JOB -> Gmail Trash
            # --------------------------------

            uid = str(email["uid"])

            if DRY_RUN:
                print(
                    "DRY RUN -> DELETE" if category == "rejection" else "DRY RUN -> TRASH",
                    uid,
                )

                continue

            try:
                if category == "rejection":
                    permanently_delete(mail, uid, trash_folder, email.get("message_id", ""))
                else:
                    move_to_trash(mail, uid, trash_folder)

            except Exception as error:
                print(
                    "TRASH ERROR:",
                    error,
                )

                errors += 1
                continue

            if category == "rejection":
                email["deleted"] = True
                email["deleted_at"] = datetime.now().astimezone().isoformat()
                deleted += 1
            else:
                email["trashed"] = True
                email["trashed_at"] = datetime.now().astimezone().isoformat()
                trashed += 1

            save_database(db)

            print(
                "DELETED:" if category == "rejection" else "TRASHED:",
                uid,
            )

    finally:
        try:
            mail.logout()
        except Exception:
            pass

        try:
            unload_model()
        except Exception:
            pass

    print()
    print("=" * 80)

    print(
        "Classified:",
        classified,
    )

    print(
        "Skipped:",
        skipped,
    )

    print(
        "Trashed:",
        trashed,
    )

    print("Permanently deleted:", deleted)

    print(
        "Errors:",
        errors,
    )


if __name__ == "__main__":
    main()
